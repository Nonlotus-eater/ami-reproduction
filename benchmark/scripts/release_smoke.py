"""Run three synthetic questions per method through real memory, Answer and Judge.

This command makes paid API calls. It validates configuration, not performance.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.pipeline.prepare import prepare
from memory_benchmark.runtime.artifacts import read_json, rows, write_json

METHODS = ('activememoryindex', 'mem0', 'invmem', 'refind')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--methods-config', type=Path, default=Path('configs/local/methods.json'))
    parser.add_argument('--runtime', type=Path, default=Path('configs/local/runtime.json'))
    parser.add_argument('--method', choices=METHODS, action='append')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    methods = read_json(args.methods_config)
    selected = args.method or list(METHODS)
    if len(set(selected)) != len(selected) or any(m not in methods for m in selected):
        parser.error('Select available methods without duplicates')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    suite = read_json(ROOT / 'configs/suites/smoke.json')
    for config in suite['benchmarks'].values():
        for key in ('data_dir', 'data_file'):
            if key in config:
                config[key] = str(ROOT / config[key])
    prepared = output / 'prepared'
    prepare(suite, read_json(args.runtime), prepared)
    policy = output / 'embedding_policy.json'
    write_json(policy, {})
    result = {'status': 'running', 'purpose': 'real_api_configuration_check_not_performance',
              'questions_per_method': 3, 'methods': {}}
    write_json(output / 'summary.json', result)
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
    for method in selected:
        config = output / f'methods.{method}.json'
        write_json(config, {method: methods[method]})
        folder = output / method
        if method in ('invmem', 'refind'):
            command = [sys.executable, '-m', 'memory_benchmark.runtime.native_bridge.runner',
                       '--policy', str(policy)]
        else:
            command = [sys.executable, '-m', 'memory_benchmark.runtime.queue']
        command += ['--prepared', str(prepared), '--methods-config', str(config), '--output', str(folder)]
        print(f'Starting real API configuration check: {method}', flush=True)
        with (output / f'{method}.log').open('w') as log:
            process = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        summary_path = folder / f'{method}.summary.json'
        summary = read_json(summary_path) if summary_path.exists() else {}
        retrievals = [r for path in (folder / method).rglob('retrievals.jsonl')
                      if not any(p.startswith('memory_attempt_') for p in path.parts)
                      for r in rows(path)]
        evidence_ok = len(retrievals) == 3 and all(r['evidence'] for r in retrievals)
        usage = [read_json(p) for p in folder.rglob('*.json') if p.name == 'usage.json' or p.name.startswith('reader_usage_')]
        calls = [call for record in usage for call in record.get('calls', [])]
        passed = (process.returncode == 0 and summary.get('complete') is True and
                  summary.get('expected') == summary.get('scored') == 3 and evidence_ok)
        record = {'status': 'passed' if passed else 'failed', 'exit_code': process.returncode,
                  'expected': summary.get('expected'), 'scored': summary.get('scored'),
                  'nonempty_evidence': evidence_ok, 'requests': len(calls),
                  'estimated_cny': sum(c.get('estimated_cny', 0) for c in calls)}
        result['methods'][method] = record
        write_json(output / 'summary.json', result)
        print({method: record}, flush=True)
    result['status'] = 'passed' if all(r['status'] == 'passed' for r in result['methods'].values()) else 'failed'
    write_json(output / 'summary.json', result)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
