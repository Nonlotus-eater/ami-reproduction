"""Install an audited Add-checkpoint overlay at the next worker boundary.

No worker is stopped and no prepared/config/answer/score file is replaced.
Only coordinators pause briefly while validated files are installed. Original
framework contracts remain intact; explicit before/after receipts allow their
exact execution changes to be checked on later queue resumes.
"""
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from memory_benchmark.pipeline.prepare import code_identity
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json
from memory_benchmark.runtime.execution_overlay import verify_framework_overlay, verify_frozen_file
from memory_benchmark.runtime.memory_checkpoint import POLICY, durable_json

ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_FILES = (
    'runtime/memory_checkpoint.py', 'runtime/execution_overlay.py',
    'runtime/budget.py', 'runtime/model_client.py', 'runtime/profile.py',
    'runtime/retry.py', 'runtime/memory_retry.py',
    'runtime/worker.py', 'pipeline/prepare.py',
    'runtime/native_bridge/worker.py', 'runtime/native_bridge/runner.py', 'runtime/native_bridge/common.py',
)


def process(pid):
    try:
        parts = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'state': parts[0], 'parent': int(parts[1]), 'token': parts[19],
                'command': Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode()}
    except (OSError, IndexError):
        return None


def prefix(path, count=None):
    digest = hashlib.sha256()
    consumed = 0
    with path.open('rb') as handle:
        while count is None or consumed < count:
            block = handle.read(1024 * 1024 if count is None else min(1024 * 1024, count - consumed))
            if not block:
                break
            digest.update(block)
            consumed += len(block)
    if count is not None and consumed != count:
        raise ValueError('Saved answer/score was truncated')
    return {'bytes': consumed, 'sha256': digest.hexdigest()}


def plan(experiment):
    results = experiment / 'results'
    launch = read_json(results / 'launch_plan.json')
    for name, digest in launch['file_sha256'].items():
        if file_hash(name) != digest:
            raise ValueError('Frozen launch input changed')
    queues = []
    for path in sorted(results.glob('*/seed-*/manifest.json')):
        queue = path.parent
        manifest = read_json(path)
        if (queue / 'execution_overlay.json').exists():
            raise ValueError('Queue already has an execution overlay')
        if code_identity(queue / 'frozen_framework') != manifest['framework']:
            raise ValueError('Queue framework differs before rollout')
        for relative, digest in manifest['execution'].get('method_side_bridge_sha256', {}).items():
            if file_hash(queue / 'frozen_method_bridge' / relative) != digest:
                raise ValueError('Queue bridge differs before rollout')
        changes = {}
        for relative in FRAMEWORK_FILES:
            target = 'frozen_framework/src/memory_benchmark/' + relative
            changes[target] = ROOT / 'src/memory_benchmark' / relative
        if (queue / 'frozen_method_bridge').is_dir():
            for relative in ('worker.py', 'runner.py'):
                changes['frozen_method_bridge/' + relative] = ROOT / 'src/memory_benchmark/runtime/native_bridge' / relative
        queues.append((queue, manifest, changes))
    if len(queues) != len(launch['methods']) * len(launch['seeds']):
        raise ValueError('Expected every queue to have its frozen framework')
    return launch, queues


def apply(experiment, *, handoff=False):
    with (experiment / 'memory_checkpoint_rollout.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return apply_locked(experiment, handoff=handoff)


def apply_locked(experiment, *, handoff=False):
    launch, queues = plan(experiment)
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%dT%H%M%S%z')
    audit = experiment / f'memory-checkpoint-rollout-{stamp}'
    audit.mkdir()
    results = experiment / 'results'
    saved = {str(p): prefix(p) for name in ('answers.jsonl', 'native_scores.jsonl') for p in results.rglob(name)}
    durable_json(audit / 'preserved_answers_and_scores.json', saved)
    for name in ('progress.json', 'supervisor.json', 'launch_plan.json'):
        shutil.copyfile(results / name, audit / name)
    pending = []
    for queue, manifest, changes in queues:
        receipt = {'version': 1, 'activation': 'new_memory_workers', 'activated_at': stamp,
            'memory_checkpoint_policy': POLICY, 'baseline_framework_sha256': manifest['framework']['sha256'],
            'authorization': 'User requested chunk recovery from the next memory, preserving active workers',
            'rollout_audit': str(audit), 'changes': {}}
        for relative, source in changes.items():
            target = queue / relative
            original = audit / queue.relative_to(results) / 'originals' / relative
            before = file_hash(target) if target.exists() else None
            if before:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, original)
            staged = audit / queue.relative_to(results) / 'staged' / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, staged)
            with staged.open('rb') as handle:
                os.fsync(handle.fileno())
            receipt['changes'][relative] = {'before_sha256': before, 'after_sha256': file_hash(staged),
                'original': str(original.relative_to(experiment)) if before else None}
            # Original files must be under the queue for receipt validation.
            if before:
                local_original = queue / 'execution_overlays' / stamp / 'originals' / relative
                local_original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, local_original)
                receipt['changes'][relative]['original'] = str(local_original.relative_to(queue))
            pending.append((target, staged, original if before else None, before))
        staged_receipt = audit / queue.relative_to(results) / 'execution_overlay.json'
        write_json(staged_receipt, receipt)
        pending.append((queue / 'execution_overlay.json', staged_receipt, None, None))
    supervisor = read_json(results / 'supervisor.json')
    paused = []
    installed = []
    active_workers = []
    replacement = None
    pause_started = time.monotonic()
    def pause(pid, expected):
        current = process(pid)
        if not current or current['state'] == 'Z':
            return
        if current['token'] != expected or str(experiment) not in current['command']:
            raise ValueError('Coordinator PID identity changed')
        if current['state'] in ('T', 't'):
            raise ValueError('Coordinator was already paused')
        os.kill(pid, signal.SIGSTOP)
        paused.append(current)
        deadline = time.monotonic() + 2
        while True:
            stopped = process(pid)
            if not stopped or stopped['state'] in ('T', 't', 'Z'):
                return
            if time.monotonic() > deadline:
                raise TimeoutError('Coordinator did not pause promptly')
            time.sleep(0.01)
    try:
        pause(supervisor['pid'], supervisor['process_token'])
        children = [p for entry in Path('/proc').iterdir() if entry.name.isdecimal()
                    and (p := process(int(entry.name))) and p['parent'] == supervisor['pid']]
        for child in children:
            pause(child['pid'], child['token'])
        queue_pids = {p['pid'] for p in children}
        active_workers = [p for entry in Path('/proc').iterdir() if entry.name.isdecimal()
                          and (p := process(int(entry.name))) and p['parent'] in queue_pids]
        durable_json(audit / 'before_activation.json', {'coordinators': paused, 'active_workers': active_workers})
        for target, staged, original, before in pending:
            if (file_hash(target) if target.exists() else None) != before:
                raise ValueError('Execution target changed during staging')
            target.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(target)
            installed.append((target, original))
        for queue, manifest, changes in queues:
            verify_framework_overlay(queue / 'frozen_framework', manifest['framework'], code_identity(queue / 'frozen_framework'))
            for relative, digest in manifest['execution'].get('method_side_bridge_sha256', {}).items():
                verify_frozen_file(queue / 'frozen_method_bridge' / relative, digest)
        if handoff:
            # Stop only the old supervisor. Its independently launched queues
            # and their workers retain their PIDs, counters and checkpoints.
            current = process(supervisor['pid'])
            if not current or current['token'] != supervisor['process_token']:
                raise ValueError('Supervisor identity changed before handoff')
            os.kill(supervisor['pid'], signal.SIGTERM)
            os.kill(supervisor['pid'], signal.SIGCONT)
            deadline = time.monotonic() + 5
            while (current := process(supervisor['pid'])) and current['state'] != 'Z':
                if time.monotonic() > deadline:
                    raise TimeoutError('Original supervisor did not exit')
                time.sleep(0.01)
            command = [sys.executable, '-u', str(ROOT / 'scripts/resume_with_memory_checkpoints.py'),
                       '--experiment', str(experiment)]
            env = {**os.environ, 'PYTHONPATH': str(experiment / 'release/benchmark/src'),
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
                   'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                   'TOKENIZERS_PARALLELISM': 'false'}
            with (audit / 'supervisor.log').open('a') as log:
                replacement = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True)
    except BaseException:
        for target, original in reversed(installed):
            if original:
                temporary = target.with_name(target.name + '.rollback')
                shutil.copyfile(original, temporary)
                temporary.replace(target)
            else:
                target.unlink(missing_ok=True)
        raise
    finally:
        for current in reversed(paused):
            latest = process(current['pid'])
            if latest and latest['token'] == current['token']:
                os.kill(current['pid'], signal.SIGCONT)
    pause_seconds = time.monotonic() - pause_started
    if replacement:
        deadline = time.monotonic() + 30
        while read_json(results / 'supervisor.json').get('pid') != replacement.pid:
            if replacement.poll() is not None:
                raise RuntimeError('Overlay supervisor exited; see rollout supervisor.log')
            if time.monotonic() > deadline:
                raise TimeoutError('Overlay supervisor startup has not completed')
            time.sleep(0.1)
        original_resume = experiment / 'resume.sh'
        shutil.copyfile(original_resume, audit / 'resume.sh.original')
        # Literal paths are shell-quoted; never interpolate JSON as shell code.
        import shlex
        original_resume.write_text('#!/usr/bin/env bash\nset -euo pipefail\nexec ' +
            ' '.join(shlex.quote(v) for v in command) + ' "$@"\n')
    for name, fingerprint in saved.items():
        if prefix(Path(name), fingerprint['bytes']) != fingerprint:
            raise ValueError('Existing answer/score prefix changed')
    for name, digest in launch['file_sha256'].items():
        if file_hash(name) != digest:
            raise ValueError('Frozen launch input changed during rollout')
    receipt = {'status': 'enabled', 'policy': POLICY, 'activated_at': stamp,
        'queues': [str(q) for q, _, _ in queues], 'coordinator_pause_seconds': pause_seconds,
        'worker_processes_signaled': 0, 'active_workers_preserved': [
            {'pid': p['pid'], 'token': p['token'], 'still_same_process': bool(
                (latest := process(p['pid'])) and latest['token'] == p['token'])} for p in active_workers],
        'preserved_answer_score_files': len(saved), 'frozen_launch_inputs_unchanged': True,
        'replacement_supervisor_pid': replacement.pid if replacement else None,
        'audit': str(audit), 'script_sha256': file_hash(Path(__file__))}
    durable_json(audit / 'activation_receipt.json', receipt)
    with (experiment / 'operations.jsonl').open('a') as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False) + '\n')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--handoff-supervisor', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.handoff_supervisor:
        parser.error('--apply requires --handoff-supervisor so future queues load the audited framework')
    experiment = args.experiment.resolve()
    if args.apply:
        apply(experiment, handoff=args.handoff_supervisor)
    else:
        _, queues = plan(experiment)
        print(json.dumps({'queues': len(queues), 'policy': POLICY, 'status': 'validated; not applied'}))
