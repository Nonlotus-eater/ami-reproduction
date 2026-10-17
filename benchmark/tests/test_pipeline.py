import json
from pathlib import Path
import tempfile
import subprocess
import os
import sys
import unittest
from unittest.mock import patch

from memory_benchmark.benchmarks.locomo_refined.loader import load as locomo
from memory_benchmark.benchmarks.longmemeval.loader import load as longmem
from memory_benchmark.benchmarks.native_contract import ROOT, contract
from memory_benchmark.pipeline.context import FixtureTokenizer, normalize, pack
from memory_benchmark.pipeline.evaluate import evaluate
from memory_benchmark.pipeline.prepare import prepare, verify_prepared
from memory_benchmark.pipeline.reader import answer
from memory_benchmark.pipeline.runner import run, validate_runtime
from memory_benchmark.runtime.artifacts import read_json, rows, write_json, write_rows
from memory_benchmark.runtime.model_client import FixtureClient, Reply
from memory_benchmark.runtime.profile import public_identity, validate_profile
from memory_benchmark.runtime.resources import limits, safe_parallelism
from memory_benchmark.schemas import Evidence, Query, Target


def fixtures(root):
    data = root / "locomo"
    data.mkdir()
    write_rows(data / "conversations.jsonl", [{"sample_id": "c", "speaker_a": "Alice", "speaker_b": "Bob",
        "sessions": [{"session_index": 2, "date_time": "1:56 pm on 9 May, 2023", "messages": [
            {"dia_id": "D2", "role": "assistant", "speaker": "Bob", "text": "second", "images": ["NEVER_PASS_IMAGE"]}]},
            {"session_index": 1, "date_time": "1:56 pm on 8 May, 2023", "messages": [
            {"dia_id": "D1", "role": "user", "speaker": "Alice", "text": "first", "blip_caption": "CAPTION_SENTINEL"}]}]}])
    write_rows(data / "questions.jsonl", [
        {"sample_id": "c", "qa_id": "q1", "question": "When?", "answer": "GOLD_SENTINEL", "category": 2,
         "is_multi_modality": False, "evidence": ["D1"]},
        {"sample_id": "c", "qa_id": "q2", "question": "Image?", "answer": "GOLD_IMAGE", "category": 1,
         "is_multi_modality": True, "evidence": ["D2"]}])
    lme = root / "longmem.json"
    write_json(lme, [{"question_id": "l1", "question_type": "knowledge-update", "question": "Which?",
        "question_date": "2023/05/30 (Tue) 23:40", "answer": "GOLD_LONGMEM", "answer_session_ids": ["s"],
        "haystack_dates": ["2023/06/01 (Thu) 02:21", "2023/05/20 (Sat) 02:21"],
        "haystack_session_ids": ["s", "s"], "haystack_sessions": [
            [{"role": "user", "content": "future distractor", "has_answer": False}],
            [{"role": "user", "content": "earlier message", "has_answer": True}]]}])
    return {"suite_id": "fixture", "scope": "pilot", "purpose": "test", "aggregation": "macro_benchmark",
            "benchmarks": {"locomo_refined": {"data_dir": str(data), "media_policy": "text_only"},
                           "longmemeval_s": {"data_file": str(lme)}}}


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.suite = fixtures(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_accepts_model_maximum_answer_and_rejects_invalid_limits(self):
        runtime = read_json(ROOT / "configs/runtime.json")
        validate_runtime({**runtime, "answer_max_tokens": None})
        validate_runtime(runtime)
        for value in (0, -1, True, "unlimited"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_runtime({**runtime, "answer_max_tokens": value})
        with self.assertRaises(ValueError):
            validate_runtime({k: v for k, v in runtime.items() if k != "answer_max_tokens"})
        with self.assertRaises(ValueError):
            validate_runtime({**runtime, "model": "unknown", "answer_max_tokens": None})
        for name in ("top_k", "reader_token_budget", "answer_input_token_limit"):
            validate_runtime({**runtime, name: None})
            validate_runtime({**runtime, name: 2})
            for value in (0, -1, True, "unlimited"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    validate_runtime({**runtime, name: value})
            with self.assertRaises(ValueError):
                validate_runtime({k: v for k, v in runtime.items() if k != name})

    def test_text_only_filters_questions_and_omits_media(self):
        case, = locomo(self.suite["benchmarks"]["locomo_refined"])
        self.assertEqual([q.qa_id for q in case.queries], ["q1"])
        self.assertEqual([s.messages[0].source_id for s in case.history.sessions], ["D1", "D2"])
        self.assertEqual(case.history.sessions[0].timestamp, "2023-05-08T13:56:00+00:00")
        wire = json.dumps(case.history.to_wire())
        for secret in ("GOLD_SENTINEL", "CAPTION_SENTINEL", "NEVER_PASS_IMAGE", "capability", "evidence"):
            self.assertNotIn(secret, wire)

    def test_caption_option_explicitly_changes_inputs_and_question_set(self):
        config = {**self.suite["benchmarks"]["locomo_refined"], "media_policy": "text_with_captions"}
        case, = locomo(config)
        self.assertEqual(len(case.queries), 2)
        self.assertIn("CAPTION_SENTINEL", json.dumps(case.history.to_wire()))
        self.assertNotIn("NEVER_PASS_IMAGE", json.dumps(case.history.to_wire()))

    def test_supported_selection_uses_evidence_only_at_preparation(self):
        case, = locomo(self.suite["benchmarks"]["locomo_refined"], session_limit=1, require_supported=True)
        self.assertEqual(case.queries[0].qa_id, "q1")
        self.assertEqual(len(case.history.sessions), 1)

    def test_explicit_questions_preserve_full_history(self):
        case, = locomo(self.suite["benchmarks"]["locomo_refined"], question_ids=["q1"])
        self.assertEqual([q.qa_id for q in case.queries], ["q1"])
        self.assertEqual(len(case.history.sessions), 2)
        self.assertEqual(list(longmem(self.suite["benchmarks"]["longmemeval_s"], question_ids=["other"])), [])

    def test_longmem_duplicates_future_history_preserved_and_labels_stripped(self):
        case, = longmem(self.suite["benchmarks"]["longmemeval_s"])
        sessions = case.history.sessions
        self.assertEqual(len({s.session_id for s in sessions}), 2)
        self.assertEqual({s.source_session_id for s in sessions}, {"s"})
        self.assertEqual(sessions[0].messages[0].text, "earlier message")
        self.assertEqual(case.dataset_audit["sessions_after_question_time"], 1)
        self.assertEqual(case.targets[0].capability_leaves, ("D1",))
        wire = json.dumps(case.history.to_wire())
        for label in ("has_answer", "GOLD_LONGMEM", "answer_session_ids"):
            self.assertNotIn(label, wire)

    def test_misaligned_longmem_arrays_rejected(self):
        p = Path(self.suite["benchmarks"]["longmemeval_s"]["data_file"])
        data = read_json(p)
        data[0]["haystack_dates"] = []
        write_json(p, data)
        with self.assertRaises(ValueError):
            list(longmem(self.suite["benchmarks"]["longmemeval_s"]))

    def test_preparation_freezes_targets_separately_and_detects_tampering(self):
        prepared = self.root / "prepared"
        result = prepare(self.suite, read_json(ROOT / "configs/runtime.json"), prepared)
        self.assertEqual(result["questions"], 2)
        verify_prepared(prepared)
        case = next(rows(prepared / "cases.jsonl"))
        p = prepared / case["history"]
        self.assertNotIn("GOLD_SENTINEL", p.read_text())
        self.assertIn("GOLD_SENTINEL", (prepared / case["targets"]).read_text())
        p.write_text(p.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "Prepared input changed"):
            verify_prepared(prepared)

    def test_prepare_discards_retired_request_and_cost_limits(self):
        runtime = {**read_json(ROOT / "configs/runtime.json"), "request_limits": {"llm": 1, "embedding": 1},
                   "estimated_cny_cap": 0.001, "per_memory_worker_cny_cap": 0.001}
        prepared = self.root / "unlimited-prepared"
        prepare(self.suite, runtime, prepared)
        frozen = verify_prepared(prepared)['runtime']
        self.assertFalse({'request_limits', 'estimated_cny_cap', 'per_memory_worker_cny_cap'} & set(frozen))

    def test_offline_two_benchmark_four_method_end_to_end(self):
        prepared = self.root / "prepared"
        prepare(self.suite, read_json(ROOT / "configs/runtime.json"), prepared)
        report = run(prepared, read_json(ROOT / "configs/methods.json"), self.root / "run", offline=True)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["calls"], {})
        for summary in report["summaries"].values():
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["scored"], 2)
            self.assertIsNone(summary["local_overall_macro_benchmark"])
        job = next((self.root / "run").rglob("job.json"))
        payload = read_json(job)
        self.assertNotIn("targets", payload)
        self.assertNotIn("GOLD_SENTINEL", job.read_text())
        worker = read_json(job.parent / "report.json")
        self.assertEqual(set(worker["phase_seconds"]), {"init", "add", "finalize", "search", "close"})
        self.assertGreater(worker["resources"]["peak_rss_bytes"], 0)
        with self.assertRaises(FileExistsError):
            run(prepared, {"mem0": {}}, self.root / "run", offline=True)

    def test_bundled_demo_from_another_working_directory(self):
        from memory_benchmark.runtime.demo import demo
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            report = demo(self.root / "bundled-demo")
        finally:
            os.chdir(previous)
        self.assertTrue(report["checks_passed"])
        self.assertEqual(report["api_calls"], 0)
        native = read_json(self.root / "bundled-demo/run/report.json")
        self.assertEqual(set(native["summaries"]), {"fixture_reference"})
        self.assertIsNone(native["summaries"]["fixture_reference"]["local_overall_macro_benchmark"])

    def test_worker_failure_keeps_all_expected_questions_unscored(self):
        prepared = self.root / "prepared"
        prepare(self.suite, read_json(ROOT / "configs/runtime.json"), prepared)
        with patch("memory_benchmark.pipeline.runner.subprocess.run", side_effect=TimeoutError):
            report = run(prepared, {"mem0": {}}, self.root / "failed", offline=True)
        self.assertEqual(report["status"], "failed")
        summary = report["summaries"]["mem0"]
        self.assertEqual(summary["expected"], 2)
        self.assertEqual(summary["scored"], 0)
        self.assertIsNone(summary["local_overall_macro_benchmark"])

    def test_cli_failed_worker_returns_nonzero_and_preserves_failure(self):
        config = self.suite['benchmarks']['locomo_refined']
        p = Path(config['data_dir']) / 'conversations.jsonl'
        data = list(rows(p))
        data[0]['sessions'][0]['messages'][0]['dia_id'] = 'D1'
        write_rows(p, data)
        prepared = self.root / 'prepared'
        prepare(self.suite, read_json(ROOT / 'configs/runtime.json'), prepared)
        output = self.root / 'cli-failure'
        process = subprocess.run([sys.executable, '-m', 'memory_benchmark', 'run', '--offline',
            '--prepared', str(prepared), '--method', 'mem0', '--output', str(output)],
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, capture_output=True, text=True)
        self.assertEqual(process.returncode, 1, process.stdout)
        report = read_json(output / 'report.json')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['worker_failures'][0]['returncode'], 1)

    def test_cgroup_v1_limits_override_host_capacity(self):
        base = self.root / 'cgroup'
        (base / 'cpu').mkdir(parents=True)
        (base / 'memory').mkdir()
        (base / 'cpu/cpu.cfs_quota_us').write_text('100000')
        (base / 'cpu/cpu.cfs_period_us').write_text('100000')
        (base / 'memory/memory.limit_in_bytes').write_text(str(2 * 1024 ** 3))
        actual = limits(base, affinity_count=64)
        self.assertEqual(actual['effective_cpu_cores'], 1)
        self.assertEqual(actual['memory_limit_bytes'], 2 * 1024 ** 3)
        self.assertEqual(safe_parallelism(actual, 8), 1)

    def test_cgroup_v2_quota_and_unlimited_values(self):
        base = self.root / 'cgroup'
        base.mkdir()
        (base / 'cpu.max').write_text('50000 100000')
        (base / 'memory.max').write_text('max')
        self.assertEqual(limits(base, affinity_count=64)['effective_cpu_cores'], 0.5)
        (base / 'cpu.max').write_text('max 100000')
        actual = limits(base, affinity_count=16)
        self.assertEqual(actual['effective_cpu_cores'], 16)
        self.assertIsNone(actual['memory_limit_bytes'])
        self.assertEqual(safe_parallelism(actual, 8), 8)


class ReaderTests(unittest.TestCase):
    def test_budget_keeps_rank_and_skips_overflowing_whole_evidence(self):
        evidence = [Evidence("long", "x" * 30), Evidence("a", "12"), Evidence("b", "34")]
        text, stats = pack(evidence, FixtureTokenizer(), 5)
        self.assertEqual(text, "12\n34")
        self.assertEqual(stats["included_ids"], ["a", "b"])
        self.assertEqual(stats["omitted_ids"], ["long"])

    def test_duplicate_evidence_and_topk_violation_are_errors(self):
        with self.assertRaises(ValueError):
            normalize([Evidence("a", "one"), Evidence("a", "two")], 2)
        with self.assertRaises(ValueError):
            normalize([Evidence("a", "one")], 0)

    def test_native_prompt_parsers_and_judge_garbage_rejection(self):
        for benchmark in ("locomo_refined", "longmemeval_s"):
            self.assertEqual(contract(benchmark).parse_judge_label('{"label":"CORRECT"}'), "CORRECT")
            with self.assertRaises(ValueError):
                contract(benchmark).parse_judge_label("not a verdict")

    def test_answer_has_no_gold_and_longmem_query_time_is_visible(self):
        runtime = read_json(ROOT / "configs/runtime.json")
        query = Query("longmemeval_s", "l", "l", "Which?", "2023-05-30T23:40:00+00:00")
        record = answer(query, [], ("User", "Assistant"), FixtureClient(), FixtureTokenizer(), runtime)
        self.assertIn(query.timestamp, record["reader_prompt"])
        self.assertNotIn("GOLD_LONGMEM", record["reader_prompt"])
        score = evaluate(query, Target("l", "GOLD_LONGMEM", ("D1",)), record, FixtureClient(), runtime)
        self.assertEqual(score["label"], "WRONG")

    def test_exact_model_tokenizer(self):
        from memory_benchmark.pipeline.context import GPTTokenizer
        try:
            import tiktoken
        except ImportError:
            self.skipTest("Run in API extra environment for exact tokenizer")
        tokenizer = GPTTokenizer("gpt-4o-mini")
        self.assertEqual(tokenizer.name, "o200k_base")
        self.assertEqual(tokenizer.count("hello world"), 2)

    def test_public_profile_excludes_credentials_and_rejects_wrong_model(self):
        profile = {"OPENAI_BASE_URL": "https://provider.example/v1", "AMI_EMBED_BASE_URL": "https://embedding.example/v1",
                   "OPENAI_API_KEY": "SECRET_LLM", "AMI_EMBED_API_KEY": "SECRET_EMBED", "AMI_LLM_MODEL": "gpt-4o-mini"}
        identity = public_identity(validate_profile(profile, "gpt-4o-mini"), {"model": "gpt-4o-mini"})
        self.assertNotIn("SECRET", json.dumps(identity))
        with self.assertRaises(ValueError):
            validate_profile({**profile, "AMI_LLM_MODEL": "qwen-plus"}, "gpt-4o-mini")


if __name__ == "__main__":
    unittest.main()
