"""Regression checks for clbench_judge."""
import json
import unittest
from unittest.mock import Mock

from memory_benchmark.benchmarks.clbench_judge import (
    JUDGE_PROTOCOL, STATUS_FIELD, STRICT_JUDGE_PROTOCOL, judgment_response_format, parse_judgment,
)
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


class StrictJudgeTests(unittest.TestCase):
    def test_protocol_failures_retry_only_judge_and_accept_the_first_valid_zero(self):
        invalid = [judgment({"1": "yes", "2": "N/A"}, 1),
                   judgment({"1": "yes"}, 1), judgment({"1": "yes", "2": "no"}, 1)]
        valid = judgment({"1": "yes", "2": "no"}, 0)
        for bad in invalid:
            with self.subTest(bad=bad):
                client = Mock()
                client.complete.side_effect = [Reply(json.dumps(bad), "gpt-4o-mini"),
                                               Reply(json.dumps(valid), "gpt-4o-mini")]
                result = evaluate(Query("clbench", "e", "q", "Q"),
                    Target("q", "", (), scoring_data={"rubrics": ["a", "b"]}), "saved answer", client,
                    {"judge_max_tokens": 16384, "judge_temperature": 0,
                     "clbench_judge_protocol": STRICT_JUDGE_PROTOCOL, "judge_protocol_max_attempts": 3})
                self.assertEqual(result["rubric_clbench_score"], 0)
                self.assertEqual(result["judge_protocol_attempts"], 2)
                self.assertEqual(client.complete.call_count, 2)
                self.assertTrue(all(c.kwargs["stage"] == "judge" for c in client.complete.call_args_list))

    def test_repeated_invalid_judge_stops_without_fabricating_a_score(self):
        client = Mock()
        client.complete.return_value = Reply(json.dumps(judgment({"1": "N/A"}, 1)), "gpt-4o-mini")
        with self.assertRaises(ValueError):
            evaluate(Query("clbench", "e", "q", "Q"),
                Target("q", "", (), scoring_data={"rubrics": ["a"]}), "saved answer", client,
                {"judge_max_tokens": 16384, "judge_temperature": 0,
                 "clbench_judge_protocol": STRICT_JUDGE_PROTOCOL, "judge_protocol_max_attempts": 3})
        self.assertEqual(client.complete.call_count, 3)

    def test_strict_judge_requests_schema_and_keeps_original_rubrics(self):
        client = Mock()
        response = {STATUS_FIELD: {"1": "yes", "2": "no"}, "Overall Score": 0,
                    "Grading Rationale": "Second requirement is unmet."}
        client.complete.return_value = Reply(json.dumps(response), "gpt-4o-mini")
        result = evaluate(Query("clbench", "e", "q", "Q"),
            Target("q", "", (), scoring_data={"rubrics": ["Unique first rubric", "Unique second rubric"]}),
            "saved answer", client, {"judge_max_tokens": 16384, "judge_temperature": 0,
                                      "clbench_judge_protocol": STRICT_JUDGE_PROTOCOL})
        prompt = client.complete.call_args.args[0]
        self.assertIn("Unique first rubric", prompt)
        self.assertIn('"N/A"', prompt)
        self.assertEqual(client.complete.call_args.kwargs["response_format"], judgment_response_format(2))
        self.assertEqual(result["judge_protocol"], STRICT_JUDGE_PROTOCOL)
        self.assertEqual(result["rubric_clbench_score"], 0)

    def test_strict_judge_rejects_na_and_a_contradictory_total(self):
        for statuses, score in (({"1": "no", "2": "N/A"}, 0),
                                ({"1": "yes", "2": "no"}, 1)):
            with self.subTest(statuses=statuses), self.assertRaises(ValueError):
                parse_judgment(json.dumps({STATUS_FIELD: statuses, "Overall Score": score,
                    "Grading Rationale": "reason"}), 2, indexed=True, strict_statuses=True)

    def test_schema_requires_every_number_and_forbids_extra_fields(self):
        schema = judgment_response_format(37)["json_schema"]["schema"]
        statuses = schema["properties"][STATUS_FIELD]
        self.assertEqual(statuses["required"], [str(i) for i in range(1, 38)])
        self.assertFalse(statuses["additionalProperties"])
        self.assertEqual(statuses["properties"]["37"]["enum"], ["yes", "no"])


if __name__ == "__main__":
    unittest.main()
