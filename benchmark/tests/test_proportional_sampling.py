"""Published cohort quotas and long-history exclusion regressions."""
from collections import Counter
import csv
import json
from pathlib import Path
import unittest

from memory_benchmark.pipeline.proportional_sampling import balanced, SEEDS, sample_questions

ROOT = Path(__file__).resolve().parents[1]


class ProportionalSamplingTests(unittest.TestCase):
    def test_joint_rounding_reproduces_each_published_dataset_quota(self):
        with (ROOT / 'configs/proportional-200-v1/dataset_distribution.csv').open(encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        population = {r['benchmark']: int(r['population_questions']) for r in rows}
        actual = balanced(population, {s: 200 for s in SEEDS}, 'dataset-quota')
        for seed in SEEDS:
            self.assertEqual(actual[seed], {r['benchmark']: int(r[f'seed_{seed}_questions']) for r in rows})
            self.assertEqual(sum(actual[seed].values()), 200)
        self.assertEqual(actual, balanced(dict(reversed(list(population.items()))), {s:200 for s in SEEDS}, 'dataset-quota'))

    def test_beam_long_histories_are_not_selected_even_when_unused(self):
        pool = []
        for history, length in [('a','100K'), ('b','100K'), ('c','500K'), ('d','1M')]:
            for i in range(3):
                pool.append({'benchmark':'beam', 'qa_id':history+str(i), 'source_unit_id':history,
                    'cluster_id':history, 'native_type':'fact', 'length_split':length})
        used = Counter({('beam','a'):100, ('beam','b'):100})
        selected = sample_questions(pool, {'fact':4}, 17, Counter(), used, used)
        self.assertEqual({q['source_unit_id'] for q in selected}, {'a','b'})
        self.assertEqual(len({q['qa_id'] for q in selected}), 4)

    def test_published_ids_are_unique_and_match_prepared_inputs(self):
        seen = set()
        for seed in SEEDS:
            ids = json.loads((ROOT / f'configs/proportional-200-v1/seed-{seed}.json').read_text())
            manifest = json.loads((ROOT / f'samples/proportional-200-v1/prepared/seed-{seed}/manifest.json').read_text())
            selected = {(b,q) for b,values in ids.items() for q in values}
            prepared = {(b,q) for b,values in manifest['benchmarks'].items() for q in values['items']}
            self.assertEqual(selected, prepared)
            self.assertEqual(len(selected), 200)
            self.assertFalse(seen & selected)
            seen.update(selected)
        self.assertEqual(len(seen), 1000)
