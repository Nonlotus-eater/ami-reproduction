"""Regression checks for sampling."""
from collections import Counter
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.pipeline.source_sampling import (
    expand_histories, leaf_requirements, select, select_histories,
)
from memory_benchmark.runtime.artifacts import read_json, rows, write_json, write_rows
from scripts.sample_small_benchmark import sample


def question(ident, leaf, history=None, benchmark="fixture"):
    return {"benchmark": benchmark, "qa_id": ident, "leaf": leaf, "family": leaf[0],
            "source_unit_id": history or ident, "cluster_id": history or ident,
            "length_split": None, "history_locator": {"id": history or ident}}


class SamplingQuotaTests(unittest.TestCase):
    def test_rare_leaf_reserved_and_family_cap_enforced(self):
        pool = [question(str(i), "A1") for i in range(100)] + [question("rare1", "A3"), question("rare2", "A3")]
        result = select(pool, 40, 17, leaf_requirements(pool, 2))
        self.assertEqual(len(result), 40)
        self.assertEqual(Counter(x["leaf"] for x in result)["A3"], 2)
        self.assertEqual(len({x["qa_id"] for x in result}), 40)
        self.assertEqual(result, select(list(reversed(pool)), 40, 17, leaf_requirements(pool, 2)))

    def test_insufficient_bank_leaf_retains_its_only_question(self):
        pool = [question("one", "A3"), question("other", "A1")]
        self.assertEqual(leaf_requirements(pool, 2), {"A3": 1, "A1": 1})
        self.assertEqual(len(select(pool, 40, 17, leaf_requirements(pool, 2))), 2)

    def test_family_cap_below_leaf_minimums_rejected(self):
        pool = [question(f"{leaf}{i}", leaf) for leaf in ("A1", "A3") for i in range(2)]
        with self.assertRaisesRegex(ValueError, "Family cap"):
            select(pool, 3, 17, leaf_requirements(pool, 2))

    def test_supplemental_histories_stop_at_hard_cap(self):
        pool = [question(f"{leaf}{i}", leaf) for leaf in ("A1", "A3") for i in range(2)]
        with self.assertRaisesRegex(ValueError, "3-history cap"):
            expand_histories(pool, [], 17, 40, leaf_requirements(pool, 2), 4, 3)
        histories, additions = expand_histories(pool, [], 17, 40, leaf_requirements(pool, 2), 4, 4)
        self.assertEqual(len(histories), 4)
        self.assertEqual(len(additions), 4)

    def test_history_keys_include_dataset(self):
        pool = [question("one", "A1", "shared", "dataset1"), question("two", "A3", "shared", "dataset2")]
        histories, _ = expand_histories(pool, [], 17, 40, leaf_requirements(pool, 2), 2, 2)
        self.assertEqual({(h["benchmark"], h["source_unit_id"]) for h in histories},
                         {("dataset1", "shared"), ("dataset2", "shared")})

    def test_total_minimum_infeasible_under_family_cap_rejected(self):
        pool = [question(str(i), "A1") for i in range(100)]
        initial = select_histories(pool, {"fixture": 1}, 17)
        with self.assertRaisesRegex(ValueError, "only 40"):
            expand_histories(pool, initial, 17, 40, leaf_requirements(pool, 2), 91, 30)

    def test_coverage_priority_fits_joint_leaf_quotas_within_history_cap(self):
        pool = [question("a", "A1", "shared"), question("b", "B1", "shared"),
                question("a-only", "A1", "other")]
        histories, _ = expand_histories(pool, [], 17, 40, {"A1": 1, "B1": 1}, 2, 1,
                                       priority="coverage")
        self.assertEqual([h["source_unit_id"] for h in histories], ["shared"])
        reversed_histories, _ = expand_histories(list(reversed(pool)), [], 17, 40,
                                                {"A1": 1, "B1": 1}, 2, 1, priority="coverage")
        self.assertEqual(histories, reversed_histories)

    def test_history_floor_applies_after_question_selection_without_relaxing_quotas(self):
        pool = [question(f"common-{i}", "A1", "common") for i in range(100)]
        pool += [question(f"rare-{i}", "A3", "rare") for i in range(3)]
        pool += [question(f"extra-{i}", "A1", f"extra-history-{i}") for i in range(6)]
        result = select(pool, 8, 17, {"A1": 3, "A3": 3}, min_histories=5)
        self.assertEqual(len(result), 8)
        self.assertGreaterEqual(len({q["source_unit_id"] for q in result}), 5)
        self.assertGreaterEqual(Counter(q["leaf"] for q in result)["A3"], 3)
        self.assertEqual(result, select(list(reversed(pool)), 8, 17,
                                        {"A1": 3, "A3": 3}, min_histories=5))

    def test_infeasible_actual_history_floor_is_not_silently_relaxed(self):
        pool = [question(str(i), "A1", "same") for i in range(10)]
        with self.assertRaisesRegex(ValueError, "2 actual histories"):
            select(pool, 8, 17, {"A1": 8}, min_histories=2)
        with self.assertRaisesRegex(ValueError, "between zero and"):
            expand_histories(pool, [], 17, 40, {"A1": 8}, 0, 1, min_histories=2)

    def test_selected_history_floor_is_supplemented(self):
        pool = [question(str(i), "A1") for i in range(10)]
        histories, additions = expand_histories(pool, [], 17, 40, {"A1": 1}, 1, 5,
                                               min_histories=4, priority="coverage")
        self.assertEqual(len(histories), 4)
        self.assertEqual(sum(a["reason"] == "total-history-minimum" for a in additions), 3)


class PublicSourceSamplingTests(unittest.TestCase):
    def inputs(self, root):
        source = root / 'public.jsonl'
        write_rows(source, [{'metadata': {'task_id': str(i), 'context_id': str(i),
            'context_category': 'Rule System Application' if i % 2 else 'Domain Knowledge Reasoning'},
            'messages': [{'role': 'user', 'content': 'History'},
                         {'role': 'assistant', 'content': 'Acknowledged'},
                         {'role': 'user', 'content': 'Question'}], 'rubrics': ['GOLD_SENTINEL']}
            for i in range(4)])
        suite = root / 'suite.json'
        write_json(suite, {'suite_id': 'fresh-public-fixture', 'scope': 'expanded',
            'purpose': 'offline-migration-check', 'aggregation': 'macro_benchmark',
            'question_bank_expected_counts': {'clbench': 4},
            'benchmarks': {'clbench': {'data_file': str(source)}}})
        policy = root / 'policy.json'
        write_json(policy, {'seeds': [17, 18, 19, 20, 21], 'max_questions_per_family': 3,
            'min_questions_per_available_leaf': 1, 'min_questions_per_round': 2,
            'min_history_instances_per_round': 2, 'max_history_instances_per_round': 3,
            'initial_history_counts': {'clbench': 1}, 'history_priority': 'coverage',
            'retain_all_selected_histories': True, 'beam_length_filter': '100K'})
        return suite, policy

    def run_cli(self, script, args, cwd):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *map(str, args)],
            cwd=cwd, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sample_and_freeze_in_fresh_directory_without_historical_inputs(self):
        for retain_all in (False, True):
            with self.subTest(retain_all=retain_all), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                suite, policy = self.inputs(root)
                if not retain_all:
                    value = read_json(policy)
                    value.update(min_history_instances_per_round=0,
                                 max_history_instances_per_round=4,
                                 initial_history_counts={'clbench': 4},
                                 history_priority='seeded', retain_all_selected_histories=False)
                    write_json(policy, value)
                sampling, prepared = root / 'sampling', root / 'prepared'
                self.run_cli('sample_small_benchmark.py',
                             ['--suite', suite, '--config', policy, '--output', sampling], root)
                summary = read_json(sampling / 'summary.json')
                self.assertEqual(summary['status'], 'complete')
                self.assertEqual(summary['api_calls'], 0)
                self.assertEqual(summary['retain_all_selected_histories'], retain_all)
                self.assertNotIn('GOLD_SENTINEL', (sampling / 'candidate_questions.jsonl').read_text())
                for seed in summary['seeds']:
                    selected = list(rows(sampling / f'seed-{seed}/selected_questions.jsonl'))
                    self.assertEqual({q['leaf'] for q in selected}, {'G1', 'G2'})
                    self.assertEqual(len({q['source_unit_id'] for q in selected}), len(selected))
                    if not retain_all:
                        self.assertEqual(summary['rounds'][str(seed)]['histories_selected_first'], 4)
                        self.assertEqual(summary['rounds'][str(seed)]['history_instances'], 3)
                self.assertEqual(read_json(sampling / 'validation.json')[
                    'all_selected_histories_retain_questions'], retain_all)
                self.run_cli('prepare_sampled_seeds.py', ['--sampling', sampling, '--suite', suite,
                    '--runtime', ROOT / 'configs/runtime.json', '--output', prepared], root)
                frozen = read_json(prepared / 'summary.json')
                self.assertEqual(frozen['status'], 'prepared')
                self.assertEqual(frozen['api_calls'], 0)
                for seed in summary['seeds']:
                    manifest = read_json(prepared / f'seed-{seed}/manifest.json')
                    ids = {q['qa_id'] for q in rows(sampling / f'seed-{seed}/selected_questions.jsonl')}
                    self.assertEqual(set(manifest['benchmarks']['clbench']['items']), ids)

    def test_preselected_source_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite, policy = self.inputs(root)
            value = read_json(suite)
            value['benchmarks']['clbench']['question_ids'] = ['clbench:0']
            write_json(suite, value)
            with self.assertRaisesRegex(ValueError, 'preselected question IDs'):
                sample(suite, policy, root / 'sampling')
            self.assertFalse((root / 'sampling').exists())

    def test_insufficient_leaf_quota_does_not_publish_a_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite, policy = self.inputs(root)
            value = read_json(policy)
            value['min_questions_per_available_leaf'] = 3
            write_json(policy, value)
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'available-leaf minimum'):
                sample(suite, policy, root / 'sampling')
            self.assertFalse((root / 'sampling').exists())


if __name__ == "__main__":
    unittest.main()
