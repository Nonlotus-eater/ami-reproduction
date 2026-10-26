"""Reproduce proportional-cluster-200-v1 directly from the full public sources."""
import argparse
from collections import Counter
from pathlib import Path
import socket
from unittest.mock import patch

from memory_benchmark.pipeline.proportional_sampling import design, POLICY, SEEDS, BENCHMARKS
from memory_benchmark.pipeline.source_sampling import question_index
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows

ROOT = Path(__file__).resolve().parents[1]


def sample(suite_file, config_file, output):
    suite_file, config_file, output = (Path(p).resolve() for p in (suite_file, config_file, output))
    policy = read_json(config_file)
    if (policy['strategy'] != POLICY or policy['seeds'] != SEEDS or policy['questions_per_seed'] != 200
            or policy['beam_eligible_length'] != '100K' or policy['beam_histories_per_seed'] != 2
            or policy['locomo_histories_per_seed'] != 2 or not policy['preserve_full_histories']):
        raise ValueError('This version implements the published 200-question policy exactly')
    if any('question_ids' in b for b in read_json(suite_file)['benchmarks'].values()):
        raise ValueError('Sampling requires the full question bank')
    if output.exists():
        raise ValueError('Sampling output already exists')
    pool, sources = question_index(suite_file)
    if dict(Counter(q['benchmark'] for q in pool)) != policy['dataset_counts']:
        raise ValueError('Full source population differs from the published policy')
    hashes = {str(p): file_hash(p) for p in sorted(sources)}
    draws, datasets, types = design(pool)
    output.mkdir(parents=True)
    rounds = {}
    for seed in SEEDS:
        path = output / f'seed-{seed}/selected_questions.jsonl'
        write_rows(path, draws[seed])
        rounds[str(seed)] = {'questions': len(draws[seed]),
            'histories': len({(q['benchmark'], q['source_unit_id']) for q in draws[seed]}),
            'selected_questions_sha256': file_hash(path)}
    audit = []
    for field in ['benchmark', 'native_type', 'family', 'leaf']:
        key = (lambda q: q['benchmark'] + '::' + q['native_type']) if field == 'native_type' else (lambda q: q[field])
        population = Counter(key(q) for q in pool)
        for label, count in sorted(population.items()):
            selected = {str(seed): sum(key(q) == label for q in draws[seed]) for seed in SEEDS}
            audit.append({'kind': field, 'label': label, 'population_count': count,
                'population_percent': 100 * count / len(pool), 'seed_counts': selected,
                'five_seed_count': sum(selected.values()), 'five_seed_percent': sum(selected.values()) / 10})
    if hashes != {str(p): file_hash(p) for p in sorted(sources)}:
        raise ValueError('Source changed during sampling')
    write_json(output / 'inventory.json', {'bank_suite_sha256': file_hash(suite_file), 'source_sha256': hashes})
    write_json(output / 'distribution_audit.json', audit)
    summary = {'status': 'complete', 'strategy': POLICY, 'seeds': SEEDS, 'questions_per_seed': 200,
               'rounds': rounds, 'total_question_occurrences': 1000,
               'distinct_selected_questions': len({(q['benchmark'], q['qa_id']) for v in draws.values() for q in v}),
               'dataset_quotas': datasets, 'native_type_quotas': types, 'api_calls': 0}
    write_json(output / 'summary.json', summary)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--suite', type=Path, default=ROOT / 'configs/local/suite.json')
    p.add_argument('--config', type=Path, default=ROOT / 'configs/proportional-200-v1/sampling.json')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    def denied(*a, **kw):
        raise RuntimeError('Network forbidden during sampling')
    with patch.object(socket.socket, 'connect', denied), patch.object(socket.socket, 'connect_ex', denied), patch.object(socket, 'create_connection', denied):
        result = sample(args.suite, args.config, args.output)
    print({'status': result['status'], 'questions': result['total_question_occurrences'], 'api_calls': 0})


if __name__ == '__main__':
    main()
