"""Regression checks for queue."""
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows
from memory_benchmark.runtime.model_client import Reply
from memory_benchmark.runtime.queue import execute_task, publish
from memory_benchmark.schemas import History, Message, Query, Session, Target
from scripts.run_five_seed_experiments import (
    METHODS, SEEDS, export, recover_queues, round_summary, validate_resume_plan,
)


class QueueTests(unittest.TestCase):
    def test_resume_reuses_memory_answer_and_score_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepared, output = root / "prepared", root / "run"
            prepared.mkdir()
            output.mkdir()
            h = History("personamem_v1", "e", ("User", "Assistant"), (Session("s", None, "", (Message("m", "user", "User", "history", None),)),))
            q = Query("personamem_v1", "e", "q", "Question", all_options="(a) A (b) B")
            write_json(prepared / "history.json", h.to_wire())
            write_rows(prepared / "queries.jsonl", [q.to_wire()])
            write_rows(prepared / "targets.jsonl", [Target("q", "(b)", ("E1",)).to_wire()])
            case = {"benchmark": "personamem_v1", "episode_id": "e", "key": "k", "history": "history.json",
                    "queries": "queries.jsonl", "targets": "targets.jsonl"}
            runtime = {"model": "gpt-4o-mini", "worker_timeout_seconds": 60, "reader_tokenizer": "fixture",
                "top_k": 100, "reader_token_budget": 4096, "answer_input_token_limit": 12000,
                "answer_max_tokens": 256, "answer_temperature": 0}
            manifest = {"suite_id": "s", "scope": "expanded", "aggregation": "macro_benchmark", "runtime": runtime,
                "execution": {"provider_identity": {}}, "methods": {"mem0": {"config": {"python": "unused"}}},
                "benchmarks": {"personamem_v1": {"primary_metric": "mcq_accuracy", "items": {"q": {"capability_leaves": ["E1"], "cluster_id": "p"}}}},
                "inputs": {field: file_hash(prepared / field) for field in ["history.json", "queries.jsonl"]}}
            calls = []
            def worker(command, **kwargs):
                job = read_json(command[-1])
                self.assertNotIn("targets", job)
                calls.append("worker")
                stage = Path(job["output"])
                write_json(stage / "report.json", {"status": "complete"})
                write_rows(stage / "retrievals.jsonl", [{"id": "q", "evidence": []}])
                return SimpleNamespace(returncode=0)
            class Client:
                def __init__(self, *args, **kwargs):
                    pass
                def complete(self, prompt, **kwargs):
                    calls.append("answer")
                    self_test.assertFalse((output / "mem0/personamem_v1/k/native_scores.jsonl").exists())
                    return Reply("<final_answer>(b)</final_answer>", "gpt-4o-mini")
                def close(self):
                    pass
            from memory_benchmark.pipeline.context import FixtureTokenizer
            class Tokenizer(FixtureTokenizer):
                name = "fixture"
                def __init__(self, *args):
                    pass
            self_test = self
            with patch("memory_benchmark.runtime.queue.subprocess.run", side_effect=worker), patch("memory_benchmark.runtime.queue.Gateway", Client), patch("memory_benchmark.runtime.queue.GPTTokenizer", Tokenizer):
                first = execute_task(prepared, output, manifest, "mem0", case)
                self.assertEqual(first["status"], "complete", first)
                second = execute_task(prepared, output, manifest, "mem0", case)
                self.assertEqual(second["status"], "complete", second)
                self.assertEqual(calls, ["worker", "answer"])
                state = publish(output, manifest, {"job": first})
                self.assertEqual(state["status"], "complete")
                path = output / "mem0/personamem_v1/k/answers.jsonl"
                path.write_text(path.read_text() + " ")
                damaged = execute_task(prepared, output, manifest, "mem0", case)
                self.assertEqual(damaged["status"], "failed")
                self.assertEqual(calls, ["worker", "answer"])


class FiveSeedExperimentTests(unittest.TestCase):
    def fixtures(self, output, partial=False):
        states = {}
        for method in METHODS:
            for seed in SEEDS:
                folder = output / method / f'seed-{seed}'
                folder.mkdir(parents=True)
                complete = not (partial and method == 'mem0' and seed == 21)
                value = 10 * (seed - 16)
                summary = {'complete': complete, 'scored': 10 if complete else 9, 'expected': 10,
                           'local_overall_macro_benchmark': value + 5 if complete else None,
                           'capabilities': {f: {'score': value if complete else None} for f in 'ABCDEGH'}}
                (folder / f'{method}.summary.json').write_text(json.dumps(summary))
                states[f'{method}:seed-{seed}'] = {'status': 'complete' if complete else 'incomplete'}
        return states

    def test_complete_export_keeps_two_schemes_and_sample_standard_deviation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            states = self.fixtures(output)
            progress = export(output, states, {s: 10 for s in SEEDS})
            self.assertEqual(progress['status'], 'complete')
            self.assertEqual(progress['scored'], 200)
            summary = json.loads((output / 'summary.json').read_text())['overall']['mem0']
            self.assertEqual(summary['overall_macro_capability']['mean'], 30)
            self.assertEqual(summary['overall_macro_benchmark']['mean'], 35)
            self.assertAlmostEqual(summary['overall_macro_capability']['sample_variance'], 250)

    def test_one_partial_seed_does_not_publish_subset_overall(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            states = self.fixtures(output, partial=True)
            progress = export(output, states, {s: 10 for s in SEEDS})
            self.assertEqual(progress['status'], 'incomplete')
            summary = json.loads((output / 'summary.json').read_text())['overall']
            self.assertIsNone(summary['mem0']['overall_macro_capability'])
            self.assertIsNone(summary['mem0']['overall_macro_benchmark'])
            self.assertIsNotNone(summary['refind']['overall_macro_capability'])

    def test_single_method_export_counts_only_selected_method(self):
        for partial in (False, True):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                states = {key: value for key, value in self.fixtures(output, partial=partial).items()
                          if key.startswith('mem0:')}
                progress = export(output, states, {s: 10 for s in SEEDS}, methods=('mem0',))
                self.assertEqual(progress['total_rounds'], 5)
                self.assertEqual(progress['expected_scores'], 50)
                self.assertEqual(progress['scored'], 49 if partial else 50)
                self.assertEqual(progress['status'], 'incomplete' if partial else 'complete')
                self.assertEqual(set(progress['rounds']), {'mem0'})
                summary = read_json(output / 'summary.json')['overall']
                self.assertEqual(set(summary), {'mem0'})
                if partial:
                    self.assertIsNone(summary['mem0']['overall_macro_capability'])
                else:
                    self.assertEqual(summary['mem0']['overall_macro_capability']['mean'], 30)
                with (output / 'overall_comparison.csv').open(encoding='utf-8-sig') as handle:
                    self.assertEqual([row['method'] for row in csv.DictReader(handle)], ['Mem0'])
                report = (output / 'report.md').read_text()
                self.assertIn('运行方法：Mem0。', report)
                self.assertNotIn('ReFind', report)

    def test_missing_summary_is_unscored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(round_summary(Path(tmp), 'mem0')['local_overall_macro_capability'])

    def test_resizing_cannot_change_inputs_or_retry_policy(self):
        original = {'parallel': 8, 'parallel_per_method': 2, 'max_queue_passes': 3,
                    'file_sha256': {'input': 'original'}}
        resized = {**original, 'parallel': 20, 'parallel_per_method': 5}
        validate_resume_plan(original, resized, resize=True)
        with self.assertRaisesRegex(ValueError, 'Frozen launch plan'):
            validate_resume_plan(original, resized)
        for changed in [{**resized, 'max_queue_passes': 4}, {**resized, 'file_sha256': {'input': 'changed'}}]:
            with self.assertRaisesRegex(ValueError, 'Frozen launch plan'):
                validate_resume_plan(original, changed, resize=True)

        original = {**original, 'methods': {'mem0': {'source_root': 'original'}}}
        with self.assertRaisesRegex(ValueError, 'Frozen launch plan'):
            validate_resume_plan(original, {**original, 'methods': {'invmem': {'source_root': 'original'}}},
                                 resize=True)

    def test_adoption_preserves_live_pid_and_retry_count(self):
        state = {'mem0:seed-17': {'method': 'mem0', 'seed': 17, 'pid': 123,
                 'process_token': 'original-process', 'status': 'running', 'attempts': 1}}
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.run_five_seed_experiments.process_token', return_value='original-process'):
            with self.assertRaisesRegex(ValueError, 'still running'):
                recover_queues(state, Path(tmp))
            jobs = recover_queues(state, Path(tmp), adopt=True)
            child, log = jobs['mem0:seed-17']
            self.assertEqual(child.pid, 123)
            self.assertIsNone(child.poll())
            self.assertIsNone(log)
            self.assertEqual(state['mem0:seed-17']['attempts'], 1)
        with patch('scripts.run_five_seed_experiments.process_token', return_value=None):
            self.assertEqual(child.poll(), 'unknown-adopted-process')

    def test_reused_pid_does_not_adopt_an_unrelated_process(self):
        state = {'mem0:seed-17': {'method': 'mem0', 'seed': 17, 'pid': 123,
                 'process_token': 'old-process', 'status': 'running', 'attempts': 1}}
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.run_five_seed_experiments.process_token', return_value='unrelated-process'):
            self.assertEqual(recover_queues(state, Path(tmp), adopt=True), {})
            self.assertEqual(state['mem0:seed-17']['status'], 'pending')


if __name__ == "__main__":
    unittest.main()
