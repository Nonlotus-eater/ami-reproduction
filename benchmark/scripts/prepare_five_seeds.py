"""Freeze the published five seed lists from verified native sources, offline."""
import argparse
import copy
from pathlib import Path

from memory_benchmark.pipeline.prepare import prepare, verify_prepared
from memory_benchmark.pipeline.runner import validate_runtime
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def freeze(suite, runtime, output, profile=None):
    validate_runtime(runtime)
    profile = Path(profile) if profile else ROOT / 'configs/proportional-200-v1'
    frozen = read_json(profile / 'frozen_samples.json')
    # Check all source hashes and ID lists before creating any output.
    for source in frozen['source_files']:
        path = Path(suite['benchmarks'][source['benchmark']][source['config_key']])
        if source['relative_path'] is not None:
            path = path / source['relative_path']
        if file_hash(path) != source['sha256']:
            raise ValueError(f"Pinned dataset differs: {source['benchmark']} {source['relative_path']}")
    for seed in frozen['seeds']:
        if file_hash(profile / f'seed-{seed}.json') != frozen['rounds'][str(seed)]['question_ids_sha256']:
            raise ValueError('Frozen question ID list changed')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rounds = {}
    for seed in frozen['seeds']:
        ids = read_json(profile / f'seed-{seed}.json')
        ids_flat = [q for values in ids.values() for q in values]
        folder = output / f'seed-{seed}'
        result = prepare(copy.deepcopy(suite), {**runtime, 'seed': seed}, folder,
                         selection={'question_ids': ids_flat})
        expected = frozen['rounds'][str(seed)]
        manifest = verify_prepared(folder)
        actual_ids = {b: sorted(c['items']) for b, c in manifest['benchmarks'].items()}
        if (actual_ids != {b: sorted(v) for b, v in ids.items()} or
                result['questions'] != expected['questions'] or result['cases'] != expected['history_instances']):
            raise ValueError(f'Seed {seed} does not match the frozen question/history set')
        rounds[str(seed)] = {'prepared': str(folder), **result}
    summary = {'status': 'prepared', 'api_calls': 0, 'rounds': rounds,
               'questions': sum(v['questions'] for v in rounds.values()),
               'history_jobs_per_method': sum(v['cases'] for v in rounds.values()),
               'note': 'New framework/runtime contracts; historical run manifests are not rewritten.'}
    write_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path('configs/local/suite.json'))
    parser.add_argument('--runtime', type=Path, default=Path('configs/local/runtime.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile', type=Path, default=ROOT / 'configs/proportional-200-v1')
    args = parser.parse_args()
    # This command is an offline data operation even in an environment with keys.
    import socket
    def denied(*args, **kwargs):
        raise RuntimeError('Network forbidden while preparing frozen seed lists')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
    result = freeze(read_json(args.suite), read_json(args.runtime), args.output, args.profile)
    print({k: v for k, v in result.items() if k != 'rounds'})


if __name__ == '__main__':
    main()
