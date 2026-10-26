"""Generate portable four-method configs without reading credentials."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path, default=Path('baselines'))
    parser.add_argument('--ami-root', type=Path, help='Existing ami-reproduction source (e.g. .. in the fork)')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--output', type=Path, default=Path('configs/local'))
    parser.add_argument('--history-workers', type=int, help='Optional total history concurrency; default unlimited')
    parser.add_argument('--per-method-workers', type=int, help='Optional per-method history concurrency')
    parser.add_argument('--query-workers', type=int, help='Optional per-history Answer/Judge concurrency')
    parser.add_argument('--budget-cny', type=float, help='Optional estimated spend cap; default unlimited')
    args = parser.parse_args()
    base = args.baseline_root.resolve()
    methods = json.loads((ROOT / 'configs/methods.json').read_text())
    methods = {k: methods[k] for k in ('mem0', 'activememoryindex')}
    for name, folder in [('mem0', 'mem0'), ('activememoryindex', 'ami-reproduction')]:
        source = args.ami_root.resolve() if name == 'activememoryindex' and args.ami_root else base / folder
        executable = source / '.venv/bin/python'
        if name == 'activememoryindex':
            import sys
            sys.path.insert(0, str(ROOT / 'src'))
            from memory_benchmark.methods.compatibility import validate_ami_roles
            try:
                validate_ami_roles({'source_root': str(source)}, {'user', 'assistant', 'system'})
            except ValueError:
                compatible = source.with_name(source.name + '-system-role')
                if not compatible.is_dir():
                    raise ValueError('AMI requires system-role support; apply patches/ami-system-role.patch or select a compatible --ami-root') from None
                validate_ami_roles({'source_root': str(compatible)}, {'user', 'assistant', 'system'})
                source = compatible
            if not executable.exists() and source.name.endswith('-system-role'):
                original_env = source.with_name(source.name.removesuffix('-system-role')) / '.venv/bin/python'
                if original_env.exists():
                    executable = original_env
        methods[name].update(source_root=str(source), python=str(executable))
        if name == 'mem0':
            methods[name]['fastembed_cache'] = str((ROOT / 'models/fastembed').resolve())
    for name, folder in [('invmem', 'InvMem'), ('refind', 'ReFind')]:
        source = base / folder
        methods[name] = {'source_root': str(source), 'python': str(source / '.benchmark-env/bin/python'),
                         'adapter_file': str(ROOT / f'src/memory_benchmark/methods/{name}.py'),
                         'embedding_backend': 'shared_api' if name == 'invmem' else 'not_used_by_native_bm25_agent'}
    runtime = json.loads((ROOT / 'configs/runtime.json').read_text())
    runtime['env_file'] = str(args.env_file.resolve())
    suite = json.loads((ROOT / 'configs/datasets/text_suite.json').read_text())
    for config in suite['benchmarks'].values():
        for key in ('data_dir', 'data_file', 'source_manifest', 'source_snapshot'):
            if key in config:
                config[key] = str((ROOT / config[key]).resolve())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    values = {'methods.json': methods, 'runtime.json': runtime, 'suite.json': suite,
              'embedding_policy.json': {}}
    for method, config in methods.items():
        values[f'methods.{method}.json'] = {method: config}
    parallel = json.loads((ROOT / 'configs/parallel/four-methods.json').read_text())
    for method in parallel['methods']:
        method['config'] = methods[method['adapter']]
    parallel.update(env_file=str(args.env_file.resolve()), runtime_file='runtime.json',
                    prepared_root=str(ROOT / 'samples/proportional-200-v1/prepared'),
                    output=str(ROOT / 'runs/proportional-five-seeds'),
                    tokenizer_cache=str(ROOT / 'samples/tokenizer-cache'))
    parallel.update(total_history_workers=args.history_workers,
                    per_method_history_workers=args.per_method_workers,
                    query_workers=args.query_workers, budget_cny=args.budget_cny)
    from parallel_runner.common import validate_config
    validate_config(parallel)
    values['parallel.json'] = parallel

    for name, value in values.items():
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(f'Wrote four-method configuration to {output}; no credentials read; no API calls.')


if __name__ == '__main__':
    main()
