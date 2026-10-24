"""Complete native memory checkpoints, then use the unchanged benchmark queue stages."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from memory_benchmark.runtime.native_bridge.common import read, write
from memory_benchmark.pipeline.prepare import native_identity
from memory_benchmark.runtime.artifacts import file_hash, rows
from memory_benchmark.runtime.execution_overlay import verify_frozen_file
from memory_benchmark.runtime.queue import initialize, check_manifest, ordered_jobs, publish, execute_task
from memory_benchmark.runtime.worker_launch import run_memory_worker


def memory(prepared, output, manifest, method, case, policy):
    folder = output / method / case['benchmark'] / case['key']
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = read(folder / 'checkpoint.json')
    for relative, digest in checkpoint.get('files', {}).items():
        assert file_hash(folder / relative) == digest
    if checkpoint.get('memory_complete'):
        return True
    stages = sorted(folder.glob('memory_attempt_*'))
    stage = folder / f'memory_attempt_{len(stages) + 1:03d}'
    stage.mkdir()
    config = manifest['methods'][method]['config']
    frozen_adapter = output / 'frozen_method_bridge' / f'adapter_{method}.py'
    assert file_hash(frozen_adapter) == manifest['execution']['method_adapter_sha256'][method]
    config = {**config, 'adapter_file': str(frozen_adapter)}
    job = {'method': method, 'method_config': config, 'runtime': manifest['runtime'],
        'scope': f'{output.name}:{method}:{case["benchmark"]}:{case["key"]}:{stage.name}',
        'provider_identity': manifest['execution']['provider_identity'],
        'history': str(prepared / case['history']), 'queries': str(prepared / case['queries']),
        'input_hashes': {f: manifest['inputs'][case[f]] for f in ('history', 'queries')},
        'output': str(stage), 'method_embedding_pacing': policy}
    pacing = Path(policy['state_directory']) if policy else None
    if pacing:
        pacing.mkdir(parents=True, exist_ok=True)
    with (stage / 'worker.log').open('w') as log:
        bridge = output / 'frozen_method_bridge'
        child = run_memory_worker(job, executable=config['python'], framework=output / 'frozen_framework',
            queue_output=output, record=manifest['methods'][method],
            timeout=manifest['runtime']['worker_timeout_seconds'], log=log,
            script=bridge / 'worker.py', extra_readonly=[(bridge, bridge)],
            extra_writable=[pacing] if pacing else [])
    report = read(stage / 'report.json')
    if child.returncode or report.get('status') != 'complete':
        return False
    expected = {q['qa_id'] for q in rows(prepared / case['queries'])}
    retrieved = list(rows(stage / 'retrievals.jsonl'))
    assert len(retrieved) == len(expected) and {r['id'] for r in retrieved} == expected
    shutil.copyfile(stage / 'retrievals.jsonl', folder / 'retrievals.jsonl')
    write(folder / 'checkpoint.json', {'memory_complete': True,
        'files': {'retrievals.jsonl': file_hash(folder / 'retrievals.jsonl')}})
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepared', required=True)
    parser.add_argument('--methods-config', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--max-jobs', type=int)
    parser.add_argument('--policy', required=True)
    args = parser.parse_args()
    prepared, output = Path(args.prepared).resolve(), Path(args.output).resolve()
    methods = read(args.methods_config)
    policy = read(args.policy)
    if not methods or set(methods) - {'invmem', 'refind'}:
        raise ValueError('This bridge supports only InvMem and ReFind')
    runtime = read(prepared / 'manifest.json')['runtime']
    if runtime['top_k'] is not None and (type(runtime['top_k']) is not int or runtime['top_k'] <= 0):
        raise ValueError('top_k must be null or a positive integer')
    if not args.resume:
        manifest = initialize(prepared, methods, output)
        bridge = Path(__file__).resolve().parent
        files = {p.name: file_hash(p) for p in bridge.glob('*.py')}
        shutil.copytree(bridge, output / 'frozen_method_bridge', ignore=shutil.ignore_patterns('__pycache__'))
        adapter_hashes = {}
        for method, config in methods.items():
            target = output / 'frozen_method_bridge' / f'adapter_{method}.py'
            shutil.copyfile(config['adapter_file'], target)
            adapter_hashes[method] = file_hash(target)
        manifest['execution']['method_side_bridge_sha256'] = files
        manifest['execution']['method_adapter_sha256'] = adapter_hashes
        manifest['execution']['method_adapter_loading'] = 'frozen_file; registered in memory_benchmark.methods'
        manifest['execution']['method_embedding_pacing'] = policy
        write(output / 'manifest.json', manifest)
        write(output / 'methods.frozen.json', methods)
        env = {**os.environ, 'PYTHONPATH': str(output / 'frozen_framework/src')}
        command = [sys.executable, '-u', str(output / 'frozen_method_bridge/runner.py'),
            '--prepared', str(prepared), '--methods-config', str(output / 'methods.frozen.json'),
            '--output', str(output), '--policy', args.policy, '--resume']
        if args.max_jobs:
            command += ['--max-jobs', str(args.max_jobs)]
        os.execvpe(command[0], command, env)
    manifest = check_manifest(output, prepared, methods)
    assert manifest['execution']['method_embedding_pacing'] == policy
    for relative, digest in manifest['execution']['method_side_bridge_sha256'].items():
        verify_frozen_file(output / 'frozen_method_bridge' / relative, digest)
    lock = (output / 'queue.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    tasks = read(output / 'progress.json').get('tasks', {})
    completed = 0
    for method, case in ordered_jobs(prepared, methods):
        key = ':'.join((method, case['benchmark'], case['key']))
        previous = tasks.get(key, {})
        if previous.get('status') == 'complete':
            continue
        if previous.get('status') == 'failed' and not args.retry_failed:
            continue
        assert native_identity(methods[method]) == manifest['methods'][method]['native_code']
        publish(output, manifest, tasks, {'method': method, 'benchmark': case['benchmark'],
            'episode_id': case['episode_id'], 'job_id': key})
        print({'stage': 'memory', 'method': method, 'benchmark': case['benchmark'], 'case': case['key']}, flush=True)
        if memory(prepared, output, manifest, method, case, policy):
            # Existing execution function sees the completed retrieval checkpoint
            # and runs its own Answer/Judge/resume logic without the factory.
            result = execute_task(prepared, output, manifest, method, case, retry_failed=args.retry_failed)
        else:
            result = {'status': 'failed', 'phase': 'memory', 'error_type': 'NativeMemoryWorkerFailure'}
        tasks[key] = {**result, 'finished_unix_seconds': time.time()}
        publish(output, manifest, tasks)
        print({'method': method, 'case': case['key'], 'status': result['status']}, flush=True)
        completed += 1
        if args.max_jobs and completed >= args.max_jobs:
            break
    state = publish(output, manifest, tasks)
    return 0 if state['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
