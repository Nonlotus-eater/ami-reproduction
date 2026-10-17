"""Quota and history-cap checks for the offline five-round sampler."""
from collections import Counter
import unittest

from scripts.sample_five_seeds import expand_histories, leaf_requirements, select, select_histories


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


if __name__ == "__main__":
    unittest.main()
