"""Extend an existing audited memory overlay without stopping live workers."""
import argparse
from datetime import datetime
import fcntl
import os
from pathlib import Path
import shutil
import signal
import time
from zoneinfo import ZoneInfo

from memory_benchmark.pipeline.prepare import code_identity
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json
from memory_benchmark.runtime.execution_overlay import checked_changes, verify_framework_overlay, verify_frozen_file
from memory_benchmark.runtime.memory_checkpoint import durable_json
from memory_benchmark.runtime.profile import public_identity, read_profile, validate_profile
from memory_benchmark.runtime.retry import RetryPolicy
from scripts.enable_memory_checkpoints import prefix, process

ROOT = Path(__file__).resolve().parents[1]
FILES = ('runtime/budget.py', 'runtime/model_client.py', 'runtime/profile.py', 'runtime/retry.py',
         'runtime/memory_retry.py', 'runtime/worker.py', 'runtime/native_bridge/worker.py',
         'runtime/native_bridge/common.py')


def plan(experiment, *, upgrade=False):
    results = experiment / 'results'
    launch = read_json(results / 'launch_plan.json')
    for path, digest in launch['file_sha256'].items():
        if file_hash(path) != digest:
            raise ValueError('Frozen launch input changed')
    queues = []
    for path in sorted(results.glob('*/seed-*/manifest.json')):
        queue, manifest = path.parent, read_json(path)
        receipt = read_json(queue / 'execution_overlay.json')
        if receipt.get('version') != 1 or receipt.get('activation') != 'new_memory_workers':
            raise ValueError('An existing audited memory overlay is required')
        if 'request_retry_policy' in receipt and not upgrade:
            raise ValueError('Queue request retry overlay is already enabled')
        checked_changes(queue, receipt)
        verify_framework_overlay(queue / 'frozen_framework', manifest['framework'], code_identity(queue / 'frozen_framework'))
        runtime = manifest['runtime']
        profile = validate_profile(read_profile(runtime['env_file']), runtime['model'])
        if not RetryPolicy.from_profile(profile, 10).enabled:
            raise ValueError('AMI_ALLOW_RETRIES=1 is required before activation')
        if public_identity(profile, runtime) != manifest['execution']['provider_identity']:
            raise ValueError('Frozen provider identity changed')
        targets = {'frozen_framework/src/memory_benchmark/' + rel: ROOT / 'src/memory_benchmark' / rel for rel in FILES}
        if (queue / 'frozen_method_bridge').is_dir():
            targets['frozen_method_bridge/worker.py'] = ROOT / 'src/memory_benchmark/runtime/native_bridge/worker.py'
        queues.append((queue, manifest, receipt, targets))
    if len(queues) != len(launch['methods']) * len(launch['seeds']):
        raise ValueError('Expected every queue to have a frozen framework')
    return launch, queues


def coordinators(experiment, supervisor):
    result = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        item = process(int(entry.name))
        if not item or item['state'] == 'Z' or str(experiment) not in item['command']:
            continue
        command = item['command']
        if item['pid'] == supervisor or any(text in command for text in (
                '-m memory_benchmark.runtime.queue ', '-m memory_benchmark.runtime.native_bridge.runner ',
                'frozen_method_bridge/runner.py ')):
            result.append(item)
    return sorted(result, key=lambda item: item['pid'] != supervisor)


def workers(experiment):
    result = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        item = process(int(entry.name))
        if item and item['state'] != 'Z' and str(experiment) in item['command'] and any(
                text in item['command'] for text in ('-m memory_benchmark.runtime.worker ', 'frozen_method_bridge/worker.py ')):
            result.append(item)
    return result


def wait_for_imports(items):
    deadline = time.monotonic() + 5
    while True:
        pending = []
        for item in items:
            current = process(item['pid'])
            if not current or current['state'] == 'Z' or current['token'] != item['token']:
                continue
            arguments = Path(f'/proc/{item["pid"]}/cmdline').read_bytes().split(b'\0')
            job = Path(next(value.decode() for value in reversed(arguments) if value))
            if not (job.parent / 'report.json').exists():
                pending.append(item)
        if not pending:
            return
        if time.monotonic() > deadline:
            raise TimeoutError('A pre-existing worker is still importing its framework')
        time.sleep(.02)


def apply(experiment, *, upgrade=False):
    with (experiment / 'memory_checkpoint_rollout.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return apply_locked(experiment, upgrade=upgrade)


def apply_locked(experiment, *, upgrade=False):
    launch, queues = plan(experiment, upgrade=upgrade)
    results = experiment / 'results'
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%dT%H%M%S%z')
    audit = experiment / ('request-retry-rollout-' + stamp)
    audit.mkdir()
    saved = {str(p): prefix(p) for name in ('answers.jsonl', 'native_scores.jsonl') for p in results.rglob(name)}
    durable_json(audit / 'preserved_answers_and_scores.json', saved)
    pending = []
    for queue, manifest, receipt, targets in queues:
        previous = queue / 'execution_overlay.json'
        archive_receipt = audit / queue.relative_to(results) / 'execution_overlay.original.json'
        archive_receipt.parent.mkdir(parents=True)
        shutil.copyfile(previous, archive_receipt)
        for relative, source in targets.items():
            target = queue / relative
            before = file_hash(target) if target.exists() else None
            original = audit / queue.relative_to(results) / 'previous' / relative
            if target.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, original)
            staged = audit / queue.relative_to(results) / 'staged' / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, staged)
            if relative not in receipt['changes']:
                baseline_original = None
                if before is not None:
                    baseline_original = queue / 'execution_overlays' / stamp / 'originals' / relative
                    baseline_original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(target, baseline_original)
                receipt['changes'][relative] = {'before_sha256': before,
                    'original': str(baseline_original.relative_to(queue)) if baseline_original else None}
            # Preserve the ORIGINAL baseline hash/archive even when extending an
            # older checkpoint overlay. The immediate previous file is rollback-only.
            receipt['changes'][relative]['after_sha256'] = file_hash(staged)
            pending.append((target, staged, original if before is not None else None, before))
        receipt['request_retry_policy'] = {'version': 1, 'owner': 'transport', 'max_retries': 10,
            'classification_version': 2,
            'activated_at': stamp, 'scope': 'new_memory_workers', 'audit': str(audit)}
        receipt.setdefault('extensions', []).append({'kind': 'request_retry_policy', 'activated_at': stamp, 'audit': str(audit)})
        staged_receipt = audit / queue.relative_to(results) / 'execution_overlay.new.json'
        write_json(staged_receipt, receipt)
        pending.append((previous, staged_receipt, archive_receipt, file_hash(previous)))
    supervisor = read_json(results / 'supervisor.json')
    paused, installed, active = [], [], []
    started = time.monotonic()
    try:
        # Pause only coordinators; active memory workers continue their old code.
        for item in coordinators(experiment, supervisor['pid']):
            current = process(item['pid'])
            if not current or current['token'] != item['token']:
                continue
            if current['state'] in ('T', 't'):
                raise ValueError('A coordinator was already paused')
            os.kill(current['pid'], signal.SIGSTOP)
            paused.append(current)
            deadline = time.monotonic() + 2
            while (latest := process(current['pid'])) and latest['state'] not in ('T', 't', 'Z'):
                if time.monotonic() > deadline:
                    raise TimeoutError('Coordinator did not pause')
                time.sleep(.01)
        if supervisor['pid'] not in {item['pid'] for item in paused}:
            raise ValueError('The recorded supervisor was not found')
        active = workers(experiment)
        wait_for_imports(active)
        durable_json(audit / 'before_activation.json', {'coordinators': paused, 'workers': active})
        for target, staged, previous, digest in pending:
            if (file_hash(target) if target.exists() else None) != digest:
                raise ValueError('Execution file changed during staging')
            target.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(target)
            installed.append((target, previous))
        for queue, manifest, _, _ in queues:
            verify_framework_overlay(queue / 'frozen_framework', manifest['framework'], code_identity(queue / 'frozen_framework'))
            for relative, digest in manifest['execution'].get('method_side_bridge_sha256', {}).items():
                verify_frozen_file(queue / 'frozen_method_bridge' / relative, digest)
    except BaseException:
        for target, previous in reversed(installed):
            if previous:
                temporary = target.with_name(target.name + '.retry-rollback')
                shutil.copyfile(previous, temporary)
                temporary.replace(target)
            else:
                target.unlink(missing_ok=True)
        raise
    finally:
        for item in reversed(paused):
            current = process(item['pid'])
            if current and current['token'] == item['token']:
                os.kill(item['pid'], signal.SIGCONT)
    for path, fingerprint in saved.items():
        if prefix(Path(path), fingerprint['bytes']) != fingerprint:
            raise ValueError('Saved answer/score prefix changed')
    for path, digest in launch['file_sha256'].items():
        if file_hash(path) != digest:
            raise ValueError('Frozen launch input changed')
    receipt = {'status': 'enabled', 'activation': 'new_memory_workers', 'max_retries': 10,
        'sdk_max_retries': 0, 'queues': len(queues), 'activated_at': stamp,
        'coordinator_pause_seconds': time.monotonic() - started, 'worker_processes_signaled': 0,
        'supervisor_pid_preserved': supervisor['pid'], 'preserved_answer_score_files': len(saved),
        'saved_prefixes_unchanged': True, 'frozen_launch_inputs_unchanged': True,
        'active_workers': [{'pid': item['pid'], 'token': item['token'], 'same_process_now': bool(
            (latest := process(item['pid'])) and latest['token'] == item['token'])} for item in active],
        'audit': str(audit), 'installer_sha256': file_hash(Path(__file__))}
    durable_json(audit / 'activation_receipt.json', receipt)
    with (experiment / 'operations.jsonl').open('a') as handle:
        import json
        handle.write(json.dumps(receipt, ensure_ascii=False) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--upgrade', action='store_true')
    args = parser.parse_args()
    experiment = args.experiment.resolve()
    if args.apply:
        result = apply(experiment, upgrade=args.upgrade)
    else:
        _, queues = plan(experiment, upgrade=args.upgrade)
        result = {'status': 'validated; not applied', 'queues': len(queues), 'max_retries': 10}
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
