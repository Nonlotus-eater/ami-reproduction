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
    runtime = json.loads((ROOT / 'configs/reproduction/runtime.json').read_text())
    defaults = json.loads((ROOT / 'configs/runtime.json').read_text())
    for key in ('answer_context_policy', 'answer_output_policy', 'answer_min_output_tokens',
                'answer_part_truncation_policy', 'clbench_judge_protocol', 'judge_protocol_max_attempts',
                'persist_answer_responses', 'provider_error_diagnostics', 'add_chunk_policy'):
        runtime[key] = defaults[key]
    runtime['env_file'] = str(args.env_file.resolve())
    suite = json.loads((ROOT / 'configs/reproduction/suite.example.json').read_text())
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
    for name, value in values.items():
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(f'Wrote four-method configuration to {output}; no credentials read; no API calls.')


if __name__ == '__main__':
    main()
