import copy
import unittest
import tempfile
from pathlib import Path

from memory_benchmark.pipeline.aggregate import import_native, summarize, validate_manifest
from memory_benchmark.runtime.artifacts import contract_hash, json_array_items


class EvaluationContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"suite_id": "synthetic-test", "scope": "pilot", "aggregation": "macro_benchmark",
                         "benchmarks": {
                             "large": {"primary_metric": "judged_accuracy", "items": {
                                 "q1": {"capability_leaves": ["A1", "A2"]},
                                 "q2": {"capability_leaves": ["B1"]},
                                 "q3": {"capability_leaves": ["A1"]}}},
                             "small": {"primary_metric": "strict_accuracy", "items": {
                                 "q4": {"capability_leaves": ["G2"]}}}}}
        self.scores = import_native(self.manifest, [{"id": f"q{i}", "is_correct": True} for i in (1, 2, 3)], "large", "demo", "r1")
        self.scores += import_native(self.manifest, [{"id": "q4", "rubric_clbench_score": 0}], "small", "demo", "r1")

    def test_macro_does_not_weight_by_question_count(self):
        result = summarize(self.manifest, self.scores)
        self.assertEqual(result["local_overall_macro_benchmark"], 50)
        self.assertEqual(result["capabilities"]["A"]["scored"], 2)
        self.assertIsNone(result["capabilities"]["H"]["score"])
        self.assertFalse(result["official_aml_comparable"])

    def test_missing_benchmark_blocks_overall(self):
        result = summarize(self.manifest, self.scores[:-1])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["local_overall_macro_benchmark"])
        self.assertEqual(result["missing_or_unscored"], [{"benchmark": "small", "qa_id": "q4"}])

    def test_duplicate_or_foreign_question_rejected(self):
        with self.assertRaises(ValueError):
            summarize(self.manifest, self.scores + [self.scores[0]])
        changed = copy.deepcopy(self.scores)
        changed[0]["qa_id"] = "not-in-frozen-suite"
        with self.assertRaises(ValueError):
            summarize(self.manifest, changed)

    def test_contract_metric_and_run_must_match(self):
        for field, value in [("contract_sha256", "wrong"), ("metric", "mcq_accuracy"), ("run_id", "r2")]:
            changed = copy.deepcopy(self.scores)
            changed[0][field] = value
            with self.assertRaises(ValueError):
                summarize(self.manifest, changed)

    def test_lexical_score_cannot_become_judged_accuracy(self):
        with self.assertRaises(ValueError):
            import_native(self.manifest, [{"id": "q1", "f1": 1.0}], "large", "demo", "r1")

    def test_native_clbench_idx_is_imported(self):
        native = [{"idx": "q4", "rubric_clbench_score": 1.0,
                   "rubric_clbench_requirement_ratio": 1.0}]
        scores = self.scores[:-1] + import_native(self.manifest, native, "small", "demo", "r1")
        result = summarize(self.manifest, scores)
        self.assertEqual(result["benchmarks"]["small"]["score"], 100)
        self.assertEqual(result["local_overall_macro_benchmark"], 100)

    def test_conflicting_native_ids_do_not_assign_score_to_wrong_question(self):
        native = [{"id": "q1", "qa_id": "q2", "is_correct": True}]
        with self.assertRaises(ValueError):
            import_native(self.manifest, native, "large", "demo", "r1")

    def test_invalid_score_or_judge_label_rejected(self):
        for value in [-1, 1.1, float("nan"), "1", 0.5]:
            with self.assertRaises(ValueError):
                import_native(self.manifest, [{"id": "q1", "is_correct": value}], "large", "demo", "r1")
        with self.assertRaises(ValueError):
            import_native(self.manifest, [{"id": "q1", "is_correct": True, "label": "WRONG"}], "large", "demo", "r1")

    def test_unscored_is_not_silently_dropped(self):
        changed = copy.deepcopy(self.scores)
        changed[0].update(status="unscored", score=None)
        result = summarize(self.manifest, changed)
        self.assertEqual(result["scored"], 3)
        self.assertIsNone(result["local_overall_macro_benchmark"])
        self.assertIsNone(result["capabilities"]["A"]["score"])

    def test_empty_suite_template_is_not_a_valid_evaluation(self):
        changed = copy.deepcopy(self.manifest)
        changed["benchmarks"]["small"]["items"] = {}
        with self.assertRaises(ValueError):
            validate_manifest(changed)

    def test_contract_hash_changes_when_question_mapping_changes(self):
        changed = copy.deepcopy(self.manifest)
        changed["benchmarks"]["small"]["items"]["q4"]["capability_leaves"] = ["G3"]
        self.assertNotEqual(contract_hash(self.manifest), contract_hash(changed))

    def test_streaming_dataset_parser_handles_boundaries_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.json"
            path.write_text('[ {"text": "跨会话", "x": [1, 2]}, {"x": 3}] \n')
            expected = [{"text": "跨会话", "x": [1, 2]}, {"x": 3}]
            self.assertEqual(list(json_array_items(path, block_size=2)), expected)
            for text in ('[{"x":1},]', '[{"x":1}', '[{"x":1}]garbage', '[{"x":1}]x', '{}'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    list(json_array_items(path, block_size=2))


if __name__ == "__main__":
    unittest.main()
