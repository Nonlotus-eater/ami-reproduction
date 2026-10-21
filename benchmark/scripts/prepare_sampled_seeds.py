"""Freeze a newly sampled five-seed plan offline, retaining native associations."""
import argparse
from collections import Counter
from pathlib import Path
import socket

from memory_benchmark.pipeline.prepare import prepare, verify_prepared
from memory_benchmark.pipeline.runner import validate_runtime
from memory_benchmark.runtime.artifacts import file_hash, read_json, rows, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sampling', type=Path, required=True)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    def denied(*a, **kw):
        raise RuntimeError('Network disabled during input freezing')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
    sampling = args.sampling.resolve()
    plan = read_json(sampling / 'summary.json')
    inventory = read_json(sampling / 'inventory.json')
    if plan['status'] != 'complete' or plan['seeds'] != [17, 18, 19, 20, 21]:
        raise ValueError('Require a complete five-seed sampling plan')
    if file_hash(args.suite) != inventory['bank_suite_sha256']:
        raise ValueError('Sampling source suite changed')
    for source, expected in inventory['source_sha256'].items():
        if file_hash(source) != expected:
            raise ValueError('Native sampling source changed')
    runtime = read_json(args.runtime)
    validate_runtime(runtime)
    suite = read_json(args.suite)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    for seed in plan['seeds']:
        selected_file = sampling / f'seed-{seed}/selected_questions.jsonl'
        if file_hash(selected_file) != plan['rounds'][str(seed)]['selected_questions_sha256']:
            raise ValueError('Sampled question list changed')
        selected = list(rows(selected_file))
        result = prepare(suite, {**runtime, 'seed': seed}, output / f'seed-{seed}',
                         selection={'question_ids': [q['qa_id'] for q in selected]})
        manifest = verify_prepared(output / f'seed-{seed}')
        expected = {(q['benchmark'], q['qa_id']): q for q in selected}
        actual = {(b, q): v for b, c in manifest['benchmarks'].items() for q, v in c['items'].items()}
        if set(expected) != set(actual):
            raise ValueError('Native prepared question IDs differ from sampling')
        for key, value in actual.items():
            sample = expected[key]
            if value['source_unit_id'] != sample['source_unit_id'] or value['capability_leaves'] != [sample['leaf']]:
                raise ValueError('Native history or capability association changed')
        histories = {(b, v['source_unit_id']) for (b, q), v in actual.items()}
        leaf_counts = Counter(q['leaf'] for q in selected)
        family_counts = Counter(q['family'] for q in selected)
        if not plan['min_history_instances_per_round'] <= len(histories) <= plan['max_history_instances_per_round']:
            raise ValueError('Prepared history count violates plan')
        if any(n < plan['min_questions_per_leaf'] for n in leaf_counts.values()) or max(family_counts.values()) > plan['max_questions_per_family']:
            raise ValueError('Prepared leaf/family quota mismatch')
        results[str(seed)] = {**result, 'histories': len(histories), 'prepared': str(output / f'seed-{seed}'),
                              'manifest_sha256': file_hash(output / f'seed-{seed}/manifest.json')}
        write_json(output / 'progress.json', {'status': 'preparing', 'api_calls': 0, 'rounds': results})
        print({'seed': seed, 'questions': result['questions'], 'histories': len(histories)}, flush=True)
    write_json(output / 'summary.json', {'status': 'prepared', 'api_calls': 0,
        'sampling_summary_sha256': file_hash(sampling / 'summary.json'),
        'questions_per_method': sum(r['questions'] for r in results.values()),
        'history_jobs_per_method': sum(r['histories'] for r in results.values()), 'rounds': results})


if __name__ == '__main__':
    main()
