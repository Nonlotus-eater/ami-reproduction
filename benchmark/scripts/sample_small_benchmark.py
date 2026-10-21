"""Sample five seeds directly from public sources, without historical run inputs."""
import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
import socket
from unittest.mock import patch
from zoneinfo import ZoneInfo

from memory_benchmark.pipeline import source_sampling
from memory_benchmark.pipeline.source_sampling import (
    expand_histories, leaf_requirements, question_index, select, select_histories,
)
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows

ROOT = Path(__file__).resolve().parents[1]


def validate_policy(policy, benchmarks):
    if policy['seeds'] != [17, 18, 19, 20, 21]:
        raise ValueError('The five-seed pipeline requires seeds 17 through 21')
    for key in ('max_questions_per_family', 'min_questions_per_available_leaf',
                'min_questions_per_round',
                'max_history_instances_per_round'):
        if type(policy[key]) is not int or policy[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    floor = policy['min_history_instances_per_round']
    if type(floor) is not int or floor < 0:
        raise ValueError('min_history_instances_per_round must be a nonnegative integer')
    if policy['min_history_instances_per_round'] > policy['max_history_instances_per_round']:
        raise ValueError('History minimum exceeds the history cap')
    counts = policy['initial_history_counts']
    if set(counts) != set(benchmarks) or any(type(n) is not int or n <= 0 for n in counts.values()):
        raise ValueError('Initial history counts must cover every configured dataset')
    if policy['history_priority'] not in {'seeded', 'coverage'}:
        raise ValueError('Unknown history priority')
    if type(policy['retain_all_selected_histories']) is not bool:
        raise ValueError('retain_all_selected_histories must be a boolean')
    if policy['beam_length_filter'] not in {None, '100K', '500K', '1M'}:
        raise ValueError('Unknown BEAM length split')


def sample(suite_file, config_file, output):
    suite_file, config_file, output = map(lambda p: Path(p).resolve(), (suite_file, config_file, output))
    suite, policy = read_json(suite_file), read_json(config_file)
    validate_policy(policy, suite['benchmarks'])
    if any('question_ids' in c for c in suite['benchmarks'].values()):
        raise ValueError('Public-source sampling requires a suite without preselected question IDs')
    if output.exists():
        raise ValueError('Choose a new sampling output directory')
    suite_hash, config_hash = file_hash(suite_file), file_hash(config_file)
    index, source_files = question_index(suite_file)
    source_hashes = {str(p): file_hash(p) for p in sorted(source_files)}
    pool = [q for q in index if q['benchmark'] != 'beam' or
            policy['beam_length_filter'] is None or q['length_split'] == policy['beam_length_filter']]
    if {q['benchmark'] for q in pool} != set(suite['benchmarks']):
        raise ValueError('A configured dataset has no eligible questions')
    minimum = policy['min_questions_per_available_leaf']
    requirements = leaf_requirements(pool, minimum)
    if any(n < minimum for n in requirements.values()):
        raise ValueError('Public sources cannot meet the configured available-leaf minimum')
    rounds, draws, histories, additions = {}, {}, {}, {}
    for seed in policy['seeds']:
        initial = select_histories(pool, policy['initial_history_counts'], seed)
        planned, supplements = expand_histories(
            pool, initial, seed, policy['max_questions_per_family'], requirements,
            policy['min_questions_per_round'], policy['max_history_instances_per_round'],
            policy['min_history_instances_per_round'], policy['history_priority'])
        allowed = {(h['benchmark'], h['source_unit_id']) for h in planned}
        eligible = [q for q in pool if (q['benchmark'], q['source_unit_id']) in allowed]
        actual_floor = (len(planned) if policy['retain_all_selected_histories']
                        else policy['min_history_instances_per_round'])
        selected = select(eligible, policy['max_questions_per_family'], seed, requirements, actual_floor)
        actual = {(q['benchmark'], q['source_unit_id']) for q in selected}
        leaf_counts, family_counts = Counter(q['leaf'] for q in selected), Counter(q['family'] for q in selected)
        if (not actual <= allowed or
                (policy['retain_all_selected_histories'] and actual != allowed) or
                not policy['min_history_instances_per_round'] <= len(actual) <= policy['max_history_instances_per_round']):
            raise ValueError('Selected or actual history count violates the policy')
        if (len(selected) < policy['min_questions_per_round'] or
                any(leaf_counts[leaf] < n for leaf, n in requirements.items()) or
                max(family_counts.values()) > policy['max_questions_per_family']):
            raise ValueError('Question quotas were violated')
        if len({(q['benchmark'], q['qa_id']) for q in selected}) != len(selected):
            raise ValueError('Duplicate selected question')
        if selected != select(list(reversed(eligible)), policy['max_questions_per_family'], seed, requirements, actual_floor):
            raise ValueError('Seed replay or input order invariance failed')
        draws[seed], histories[seed], additions[seed] = selected, planned, supplements
        rounds[str(seed)] = {'questions': len(selected), 'history_instances': len(actual),
            'histories_selected_first': len(planned), 'candidate_questions': len(eligible),
            'by_benchmark': dict(Counter(q['benchmark'] for q in selected)),
            'leaf_counts': dict(leaf_counts), 'family_counts': dict(family_counts)}
    # Refuse to publish a sampling plan if its sources/configuration changed.
    if file_hash(suite_file) != suite_hash or file_hash(config_file) != config_hash:
        raise ValueError('Sampling suite or policy changed during indexing')
    if any(file_hash(p) != digest for p, digest in source_hashes.items()):
        raise ValueError('Public source changed during sampling')
    output.mkdir(parents=True, exist_ok=False)
    write_rows(output / 'candidate_questions.jsonl', index)
    write_json(output / 'sampling_config.json', policy)
    for seed, selected in draws.items():
        folder = output / f'seed-{seed}'
        folder.mkdir()
        write_json(folder / 'selected_histories.json', histories[seed])
        write_json(folder / 'history_additions.json', additions[seed])
        write_rows(folder / 'selected_questions.jsonl', selected)
        write_json(folder / 'question_ids.json', {b: sorted(q['qa_id'] for q in selected if q['benchmark'] == b)
                   for b in suite['benchmarks']})
        rounds[str(seed)].update(selected_questions_sha256=file_hash(folder / 'selected_questions.jsonl'),
                                 selected_histories_sha256=file_hash(folder / 'selected_histories.json'))
    write_json(output / 'inventory.json', {'bank_suite': str(suite_file), 'bank_suite_sha256': suite_hash,
        'sampling_config': str(config_file), 'sampling_config_sha256': config_hash,
        'source_sha256': source_hashes, 'index_sha256': file_hash(output / 'candidate_questions.jsonl'),
        'question_bank_questions': len(index), 'by_benchmark': dict(Counter(q['benchmark'] for q in index)),
        'sampling_code_sha256': {str(p.relative_to(ROOT)): file_hash(p)
                                for p in (Path(__file__), Path(source_sampling.__file__))}, 'api_calls': 0})
    summary = {'status': 'complete', 'generated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'sampling_only': True, 'api_calls': 0, 'history_scope': 'sampled',
        'seeds': policy['seeds'], 'parent_bank_questions': len(index), 'eligible_source_questions': len(pool),
        'max_questions_per_family': policy['max_questions_per_family'], 'min_questions_per_leaf': minimum,
        'per_leaf_minimums': requirements, 'min_questions_per_round': policy['min_questions_per_round'],
        'min_history_instances_per_round': policy['min_history_instances_per_round'],
        'max_history_instances_per_round': policy['max_history_instances_per_round'],
        'initial_history_counts': policy['initial_history_counts'], 'history_supplement_priority': policy['history_priority'],
        'retain_all_selected_histories': policy['retain_all_selected_histories'],
        'beam_length_filter': policy['beam_length_filter'],
        'question_preselection': False, 'native_question_history_associations_preserved': True,
        'total_question_occurrences': sum(len(q) for q in draws.values()),
        'distinct_selected_questions': len({(q['benchmark'], q['qa_id']) for draw in draws.values() for q in draw}),
        'rounds': rounds}
    write_json(output / 'validation.json', {'status': 'passed', 'api_calls': 0,
        'seed_replay_and_input_order_invariance_verified': True,
        'all_selected_histories_retain_questions': all(
            {(q['benchmark'], q['source_unit_id']) for q in draws[seed]} ==
            {(h['benchmark'], h['source_unit_id']) for h in histories[seed]}
            for seed in policy['seeds']),
        'history_and_question_quotas_verified': True})
    write_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=ROOT / 'configs/local/suite.json')
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/small_benchmark_v1.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for path in (args.suite, args.config):
        if not path.is_file():
            parser.error(f'Missing configuration: {path}; configure public source paths before sampling')
    def denied(*args, **kwargs):
        raise RuntimeError('Network disabled during public-source sampling')
    with patch.object(socket.socket, 'connect', denied), patch.object(socket.socket, 'connect_ex', denied), patch.object(socket, 'create_connection', denied):
        summary = sample(args.suite, args.config, args.output)
    print({'output': str(args.output.resolve()), 'api_calls': 0, 'rounds': summary['rounds']}, flush=True)


if __name__ == '__main__':
    main()
