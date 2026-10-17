import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory_benchmark.benchmarks.native_contract import contract, ROOT
from memory_benchmark.benchmarks.public_datasets import load_clbench, load_v1, load_v2, blocks, safe_link
from memory_benchmark.benchmarks.public_contract import render, evaluate
from memory_benchmark.pipeline.reliability import interval
from memory_benchmark.runtime.artifacts import write_json, write_rows
from memory_benchmark.runtime.model_client import Reply
from memory_benchmark.schemas import Query, Target


def csv_file(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


class PublicDatasetTests(unittest.TestCase):
    def test_unknown_times_and_source_order_survive_wire(self):
        history = blocks("beam", "b", [[{"role": "user", "content": "first", "time_anchor": "T2"}],
                                          [{"role": "assistant", "content": "second"}]])
        self.assertIsNone(history.sessions[0].timestamp)
        self.assertIsNone(history.sessions[0].messages[0].timestamp)
        self.assertIn("T2", history.sessions[0].messages[0].text)
        self.assertEqual(history.sessions[1].messages[0].text, "second")

    def test_clbench_reference_stays_in_task_and_rubric_stays_out(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cl.jsonl"
            write_rows(path, [{"metadata": {"task_id": "task", "context_id": "shared", "context_category": "Rule System Application"},
                "messages": [{"role": "system", "content": "FORMAT"}, {"role": "user", "content": "FULL_REFERENCE_AND_QUERY"}],
                "rubrics": ["SECRET_RUBRIC"]}])
            case, = load_clbench({"data_file": str(path)})
            wire = json.dumps({"history": case.history.to_wire(), "query": case.queries[0].to_wire()})
            self.assertNotIn("SECRET_RUBRIC", wire)
            self.assertEqual(case.queries[0].text, "FULL_REFERENCE_AND_QUERY")
            self.assertEqual(case.dataset_audit["cluster_id"], "shared")
            self.assertEqual(case.targets[0].capability_leaves, ("G2",))

    def test_v1_prefix_excludes_future_and_cluster_is_persona(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            csv_file(root / "questions_32k.csv", [{"persona_id": "p", "shared_context_id": "c", "end_index_in_shared_context": "1",
                "question_id": "q", "question_type": "recall_user_shared_facts", "user_question_or_message": "Q",
                "all_options": "['(a) one', '(b) two']", "correct_answer": "GOLD_SENTINEL"}])
            write_rows(root / "shared_contexts_32k.jsonl", [{"c": [{"role": "user", "content": "past"},
                                                                  {"role": "user", "content": "FUTURE_SENTINEL"}]}])
            case, = load_v1({"data_dir": str(root)})
            wire = json.dumps({"history": case.history.to_wire(), "query": case.queries[0].to_wire()})
            self.assertNotIn("FUTURE_SENTINEL", wire)
            self.assertNotIn("GOLD_SENTINEL", wire)
            self.assertEqual(case.dataset_audit["cluster_id"], "p")

    def test_v2_never_passes_persona_or_preference_to_memory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            csv_file(root / "benchmark/text/benchmark.csv", [{"persona_id": "p", "chat_history_32k_link": "data/h.json",
                "pref_type": "ask_to_forget", "user_query": "{'role': 'user', 'content': 'query'}",
                "preference": "do not use SECRET_PREFERENCE", "expanded_persona": "SECRET_PERSONA"}])
            write_json(root / "data/h.json", {"chat_history": [{"role": "user", "content": "visible history"}]})
            case, = load_v2({"data_dir": str(root)})
            wire = json.dumps({"history": case.history.to_wire(), "query": case.queries[0].to_wire()})
            self.assertNotIn("SECRET_", wire)
            self.assertEqual(case.queries[0].text, "query")
            self.assertTrue(case.targets[0].scoring_data["response_adherence_proxy_only"])
            with self.assertRaises(ValueError):
                safe_link(root, "../secrets")

    def test_native_prompt_parity_and_braces_in_input(self):
        for benchmark in ("beam", "clbench"):
            query = Query(benchmark, "e", "q", "QUESTION {x}", system_prompt="SYS {y}")
            actual = render(query, "CONTEXT {z}", "gpt-4o-mini")
            native = contract(benchmark)
            expected = native.render_answer_prompt({"question": query.text, "context": "CONTEXT {z}"}) if benchmark == "beam" else native.render_answer_prompt_clbench(system_prompt=query.system_prompt, memories="CONTEXT {z}", question=query.text)
            self.assertEqual(actual, expected)
        query = Query("personamem_v1", "e", "q", "Q", all_options="(a) A (b) B")
        self.assertEqual(render(query, "MEMORY", "gpt-4o-mini"), contract("personamem_v1").official_messages(
            {"question": "Q", "all_options": query.all_options, "context_messages": [{"role": "user", "content": "MEMORY"}]}, "gpt-4o-mini"))

    def test_v1_no_judge_and_v2_negative_native_judge(self):
        class Client:
            def complete(self, prompt, **kwargs):
                self.prompt = prompt
                return Reply(r"\boxed{0.75}", "gpt-4o-mini")
        client = Client()
        runtime = {"judge_max_tokens": 1000, "judge_temperature": 0}
        result = evaluate(Query("personamem_v1", "e", "q", "Q"), Target("q", "(b)", ()),
                          "<final_answer>(b)</final_answer>", client, runtime)
        self.assertTrue(result["is_correct"])
        self.assertFalse(hasattr(client, "prompt"))
        query = Query("personamem_v2", "e", "q", "Q")
        target = Target("q", "do not use P", (), scoring_data={"preference": "do not use P"})
        result = evaluate(query, target, "ANSWER", client, runtime)
        self.assertEqual(result["score"], .75)
        self.assertEqual(client.prompt, contract("personamem_v2").JUDGE_PROMPT_NARROW_NEGATIVE.format(
            user_query="Q", preference="do not use P", model_response="ANSWER"))

    def test_broken_judge_is_unscored_not_wrong(self):
        class BrokenClient:
            def complete(self, prompt, **kwargs):
                return Reply("unparseable", "gpt-4o-mini")
        with self.assertRaises(ValueError):
            evaluate(Query("personamem_v2", "e", "q", "Q"), Target("q", "P", (), scoring_data={"preference": "P"}),
                     "A", BrokenClient(), {"judge_max_tokens": 100, "judge_temperature": 0})


class ReliabilityTests(unittest.TestCase):
    def test_independent_hundred_binary_questions_meet_target(self):
        result = interval([(str(i), i % 2) for i in range(100)])
        self.assertEqual(result["precision_status"], "meets_target")
        self.assertLess(result["max_margin_pp"], 10)

    def test_correlated_history_questions_do_not_count_as_independent(self):
        result = interval([(str(i // 10), (i // 10) % 2) for i in range(100)])
        self.assertEqual(result["source_clusters"], 10)
        self.assertGreater(result["max_margin_pp"], 10)
        self.assertEqual(result["intraclass_correlation"], 1)
        self.assertEqual(result["precision_status"], "few_independent_histories")

    def test_constant_clustered_scores_have_nonzero_interval(self):
        result = interval([("history", 1) for _ in range(100)])
        self.assertEqual(result["ci95"], [0., 100.])
        self.assertNotEqual(result["precision_status"], "meets_target")

    def test_missing_scores_never_meet_precision(self):
        result = interval([(str(i), i % 2) for i in range(100)], expected=101)
        self.assertEqual(result["precision_status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
