import json
import unittest
from unittest.mock import Mock

from memory_benchmark.benchmarks.clbench_judge import JUDGE_PROTOCOL, STATUS_FIELD, parse_judgment
from memory_benchmark.benchmarks.native_contract import contract
from memory_benchmark.benchmarks.public_contract import evaluate
from memory_benchmark.pipeline.evaluate import evaluate as pipeline_evaluate
from memory_benchmark.runtime.model_client import Reply
from memory_benchmark.schemas import Query, Target


def judgment(statuses, score=0):
    return {STATUS_FIELD: statuses, "Overall Score": score, "Grading Rationale": "Requirement failed."}


class CLBenchJudgeTests(unittest.TestCase):
    def test_indexed_statuses_are_ordered_by_rubric_not_json_order(self):
        payload = parse_judgment(json.dumps(judgment({"2": "no", "1": "yes"})), 2, indexed=True)
        self.assertEqual(payload[STATUS_FIELD], ["yes", "no"])
        self.assertEqual(payload["Overall Score"], 0)

    def test_missing_extra_duplicate_invalid_and_truncated_statuses_fail(self):
        cases = [
            json.dumps(judgment({"1": "no"})),
            json.dumps(judgment({"1": "no", "2": "yes", "3": "yes"})),
            json.dumps(judgment({"01": "no", "2": "yes"})),
            json.dumps(judgment({"1": "no", "2": True})),
            json.dumps(judgment({"1": "no", "2": "maybe"})),
            '{"List of Requirement Satisfaction Status":{"1":"no","1":"yes","2":"no"},'
            '"Overall Score":0,"Grading Rationale":"failed"}',
            json.dumps(judgment({"1": "no", "2": "yes"}))[:-1],
            json.dumps(judgment({"1": "no", "2": "yes"})) + '{}',
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_judgment(text, 2, indexed=True)

    def test_strict_score_must_agree_with_statuses(self):
        for statuses, score in ((["yes", "no"], 1), (["yes", "yes"], 0),
                                (["yes", "N/A"], 1), (["yes", "N/A"], 0),
                                (["yes", "no"], False), (["yes", "no"], 0.0)):
            with self.subTest(statuses=statuses, score=score), self.assertRaises(ValueError):
                parse_judgment(json.dumps(judgment(statuses, score)), 2)
        self.assertEqual(parse_judgment(json.dumps(judgment(["yes", "yes"], "1")), 2)["Overall Score"], "1")

    def test_native_na_diagnostic_is_preserved_without_fabrication(self):
        payload = judgment(["no", "N/A"])
        self.assertEqual(parse_judgment('```json\n' + json.dumps(payload) + '\n```', 2), payload)

    def test_public_scorer_rejects_wrong_status_count_before_scoring(self):
        class Client:
            def complete(self, prompt, **kwargs):
                return Reply(json.dumps(judgment({str(i): "no" for i in range(1, 5001)})), "gpt-4o-mini")
        with self.assertRaisesRegex(ValueError, "numbered status"):
            evaluate(Query("clbench", "e", "q", "Q"),
                     Target("q", "", (), scoring_data={"rubrics": ["a", "b"]}),
                     "answer", Client(), {"judge_max_tokens": 16384, "judge_temperature": 0})

    def test_regular_pipeline_uses_numbered_judge_and_preserves_raw_response(self):
        rubrics = ["first requirement", "second requirement", "third requirement"]
        raw = json.dumps(judgment({"3": "no", "1": "yes", "2": "N/A"}))
        client = Mock()
        client.complete.return_value = Reply(raw, "gpt-4o-mini")
        result = pipeline_evaluate(Query("clbench", "e", "q", "Q"),
            Target("q", "", (), scoring_data={"rubrics": rubrics}),
            {"id": "q", "generated_answer": "saved answer {x}"}, client,
            {"judge_max_tokens": 16384, "judge_temperature": 0})
        prompt = client.complete.call_args.args[0]
        native = contract("clbench")
        prefix = native.rubric_judge_prompt(rubrics_text=native._build_rubrics_text(rubrics),
            model_output=native._normalize_model_output("saved answer {x}"))
        self.assertTrue(prompt.startswith(prefix))
        self.assertEqual(prompt.count("OUTPUT FORMAT ADAPTER"), 1)
        self.assertIn('keys "1" through "3"', prompt)
        self.assertEqual(client.complete.call_args.kwargs,
            {"stage": "judge", "max_tokens": 16384, "temperature": 0})
        self.assertEqual(result["rubric_clbench_requirement_status"], ["yes", "N/A", "no"])
        self.assertEqual(result["rubric_clbench_score"], 0)
        self.assertEqual(result["judge_protocol"], JUDGE_PROTOCOL)
        self.assertEqual(result["judge_response_raw"], raw)
        self.assertEqual(json.loads(result["judge_response"])[STATUS_FIELD], ["yes", "N/A", "no"])

    def test_regular_judge_passes_only_when_all_numbered_requirements_are_yes(self):
        client = Mock()
        client.complete.return_value = Reply(json.dumps(judgment({"2": "yes", "1": "yes"}, 1)), "gpt-4o-mini")
        result = evaluate(Query("clbench", "e", "q", "Q"),
            Target("q", "", (), scoring_data={"rubrics": ["a", "b"]}),
            "answer", client, {"judge_max_tokens": 16384, "judge_temperature": 0})
        self.assertEqual(result["rubric_clbench_score"], 1)
        self.assertEqual(result["rubric_clbench_requirement_status"], ["yes", "yes"])

    def test_regular_judge_rejects_legacy_arrays_and_inconsistent_numbered_scores(self):
        for payload in (judgment(["yes", "no"]), judgment({"1": "yes", "2": "no"}, 1)):
            with self.subTest(payload=payload):
                client = Mock()
                client.complete.return_value = Reply(json.dumps(payload), "gpt-4o-mini")
                with self.assertRaises(ValueError):
                    evaluate(Query("clbench", "e", "q", "Q"),
                        Target("q", "", (), scoring_data={"rubrics": ["a", "b"]}),
                        "answer", client, {"judge_max_tokens": 16384, "judge_temperature": 0})


if __name__ == "__main__":
    unittest.main()
