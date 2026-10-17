"""Download complete pinned author repositories and verify every published file.

Uses Git for ordinary files and HTTPS for LFS objects. No model calls or keys.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'configs/datasets/public_sources.json'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def hashes(path):
    sha, blob = hashlib.sha256(), hashlib.sha1()
    blob.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            sha.update(chunk)
            blob.update(chunk)
    return sha.hexdigest(), blob.hexdigest()


def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'memory-benchmark-data'}), timeout=60) as stream:
        return json.load(stream)


def download(name, spec, root, update):
    folder = root / name
    snapshot = folder / 'snapshot'
    folder.mkdir(parents=True, exist_ok=True)
    status = {'status': 'running', 'repo_id': spec['repo_id'], 'revision': spec['revision'],
              'expected_files': spec['file_count'], 'expected_bytes': spec['bytes'], 'phase': 'metadata'}
    update(name, status)
    start = time.monotonic()
    try:
        catalog_path = folder / 'repository.json'
        catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else {}
        if catalog.get('sha') != spec['revision']:
            catalog = fetch_json('https://huggingface.co/api/datasets/' + spec['repo_id'] + '/revision/' + spec['revision'] + '?blobs=true')
            write_json(catalog_path, catalog)
        if catalog['sha'] != spec['revision'] or len(catalog['siblings']) != spec['file_count']:
            raise ValueError('Published repository differs from pinned source manifest')
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'}
        git = ['git', '-c', 'credential.helper=']
        with (folder / 'download.log').open('a') as log:
            status['phase'] = 'git_snapshot'; update(name, status)
            if not (snapshot / '.git').exists():
                subprocess.run(git + ['clone', '--depth', '1', '--progress',
                    'https://huggingface.co/datasets/' + spec['repo_id'], str(snapshot)],
                    env=env, stdout=log, stderr=log, check=True, timeout=7200)
            head = subprocess.check_output(git + ['-C', str(snapshot), 'rev-parse', 'HEAD'], env=env, text=True).strip()
            if head != spec['revision']:
                subprocess.run(git + ['-C', str(snapshot), 'fetch', '--depth', '1', 'origin', spec['revision']],
                               env=env, stdout=log, stderr=log, check=True, timeout=7200)
                subprocess.run(git + ['-C', str(snapshot), 'checkout', '--detach', spec['revision']],
                               env=env, stdout=log, stderr=log, check=True)
        status['phase'] = 'large_files'; update(name, status)
        large = [f for f in catalog['siblings'] if 'lfs' in f]
        for index, entry in enumerate(large):
            rel = entry['rfilename']
            path = snapshot / rel
            expected = entry['lfs']['sha256']
            if path.exists() and path.stat().st_size == entry['size'] and hashes(path)[0] == expected:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            url = 'https://huggingface.co/datasets/' + spec['repo_id'] + '/resolve/' + spec['revision'] + '/' + rel
            temp = path.with_name(path.name + '.download.partial')
            for attempt in range(3):
                try:
                    status.update(current_file=rel, current_file_bytes=0, large_file_index=index+1, large_file_count=len(large))
                    update(name, status)
                    request = urllib.request.Request(url, headers={'User-Agent': 'memory-benchmark-data'})
                    last_update = time.monotonic()
                    with urllib.request.urlopen(request, timeout=120) as response, temp.open('wb') as stream:
                        while chunk := response.read(1024 * 1024):
                            stream.write(chunk)
                            status['current_file_bytes'] += len(chunk)
                            if time.monotonic() - last_update >= 3:
                                update(name, status); last_update = time.monotonic()
                    if temp.stat().st_size != entry['size'] or hashes(temp)[0] != expected:
                        raise ValueError('Large file failed size or SHA-256 verification')
                    temp.replace(path)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(2)
        status['phase'] = 'verify_all_files'; update(name, status)
        verified = []
        for entry in catalog['siblings']:
            path = snapshot / entry['rfilename']
            if not path.is_file() or path.stat().st_size != entry['size']:
                raise ValueError('Published file missing or has wrong size: ' + entry['rfilename'])
            digest, blob = hashes(path)
            if 'lfs' in entry:
                if digest != entry['lfs']['sha256']:
                    raise ValueError('LFS digest mismatch')
            elif blob != entry['blobId']:
                raise ValueError('Git blob digest mismatch: ' + entry['rfilename'])
            verified.append({'path': entry['rfilename'], 'bytes': entry['size'], 'sha256': digest,
                             'upstream_blob_id': entry['blobId'], 'upstream_lfs_sha256': entry.get('lfs', {}).get('sha256')})
        tracked = set(subprocess.check_output(git + ['-C', str(snapshot), 'ls-tree', '-r', '--name-only', spec['revision']], text=True).splitlines())
        if tracked != {f['rfilename'] for f in catalog['siblings']}:
            raise ValueError('Git and published file catalogs disagree')
        result = {'status': 'complete', 'repo_id': spec['repo_id'], 'revision': spec['revision'],
                  'full_repository': True, 'verified_files': len(verified), 'verified_bytes': sum(f['bytes'] for f in verified),
                  'seconds': time.monotonic()-start, 'files': verified, 'snapshot': str(snapshot)}
        write_json(folder / 'download_manifest.json', result)
        update(name, {k: v for k, v in result.items() if k != 'files'})
        return result
    except Exception as error:
        status.update(status='failed', error_type=type(error).__name__, seconds=time.monotonic()-start)
        update(name, status)
        print(name, type(error).__name__, str(error)[:250], flush=True)
        return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(ROOT / 'datasets/raw'))
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCK.read_text())
    states = {}
    mutex = threading.Lock()
    def update(name, value):
        with mutex:
            states[name] = dict(value)
            write_json(root / 'download_status.json', {'status': 'running', 'repositories': states})
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(download, n, s, root, update) for n, s in lock['repositories'].items()]
        results = [future.result() for future in futures]
    success = all(r['status'] == 'complete' for r in results)
    result = {'status': 'complete' if success else 'failed', 'repositories': states,
              'source_lock': str(LOCK), 'full_repositories': success}
    write_json(root / 'download_status.json', result)
    print(json.dumps(result), flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
