"""Run the original supervisor, selecting audited queue execution overlays.

Launch/config/prepared hashes are still verified by that exact supervisor.
Only new queue subprocesses use their own frozen framework instead of the
original release package. Live queues are adopted with the original PID/token.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys


def overlay_environment(command, environment):
    if '--output' not in command:
        return environment
    queue = Path(command[command.index('--output') + 1])
    path = queue / 'execution_overlay.json'
    if not path.exists():
        return environment
    receipt = json.loads(path.read_text())
    if receipt.get('activation') != 'new_memory_workers' or receipt.get('version') != 1:
        raise ValueError('Invalid queue execution overlay')
    for relative, change in receipt['changes'].items():
        file = queue / relative
        if not file.resolve().is_relative_to(queue.resolve()):
            raise ValueError('Execution overlay path escaped queue')
        if hashlib.sha256(file.read_bytes()).hexdigest() != change['after_sha256']:
            raise ValueError('Execution overlay changed before queue start')
    return {**environment, 'PYTHONPATH': str(queue / 'frozen_framework/src')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    args, extra = parser.parse_known_args()
    experiment = args.experiment.resolve()
    results = experiment / 'results'
    plan = json.loads((results / 'launch_plan.json').read_text())
    limits = json.loads((results / 'progress.json').read_text()).get('execution_limits', plan)
    release = experiment / 'release/benchmark'
    original = release / 'scripts/run_five_seed_experiments.py'
    sys.path.insert(0, str(release / 'src'))
    os.chdir(release)
    original_popen = subprocess.Popen
    def launch(command, *positional, **kwargs):
        kwargs['env'] = overlay_environment(command, kwargs.get('env', os.environ))
        return original_popen(command, *positional, **kwargs)
    subprocess.Popen = launch
    sys.argv = [str(original), '--prepared', plan['prepared'], '--config', plan['config'],
        '--output', str(results), '--parallel', str(limits['parallel']),
        '--parallel-per-method', str(limits['parallel_per_method']),
        '--passes', str(limits.get('max_queue_passes', plan['max_queue_passes'])),
        '--resume', '--adopt-running', '--resize-concurrency']
    if set(plan['methods']) != {'activememoryindex', 'mem0', 'invmem', 'refind'}:
        for method in plan['methods']:
            sys.argv += ['--method', method]
    sys.argv += extra
    runpy.run_path(str(original), run_name='__main__')


if __name__ == '__main__':
    main()
