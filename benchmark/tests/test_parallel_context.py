import unittest
from types import SimpleNamespace as Obj
from scripts.parallel_runner.planner_context import PlannerContext


def hit(i, chunks):
    return Obj(anchor=chunks[i], context=tuple(chunks), score=1.0)


class ContextTests(unittest.TestCase):
    def test_dedup_preserves_numbers_and_anchor_references_and_source(self):
        chunks = [Obj(id=str(i), session_id='session', content=('原文 content ' * 400) + str(i)) for i in range(3)]
        original = [c.content for c in chunks]
        p = PlannerContext()
        text = p.format_observation([hit(i, chunks) for i in range(3)])
        for i in range(1, 4):
            self.assertIn(f'Result {i} |', text)
        self.assertIn('[MATCH] [Exact text already shown', text)
        self.assertEqual(p.events[-1]['duplicate_chunk_occurrences_removed'], 6)
        self.assertLess(p.events[-1]['deduplicated_tokens'], p.events[-1]['original_tokens'])
        self.assertEqual(original, [c.content for c in chunks])

    def test_multiple_observations_fit_preserves_question_actions_and_headers(self):
        p = PlannerContext(limit=800)
        chunks = [Obj(id=str(i), session_id='s', content=('中文 / detail ' * 2500) + str(i)) for i in range(5)]
        obs = p.format_observation([hit(i, chunks) for i in range(5)])
        messages = [{'role': 'system', 'content': 'Preserve system instructions'},
                    {'role': 'user', 'content': 'Exact question and options'},
                    {'role': 'assistant', 'content': 'Action: search_chatrecord'},
                    {'role': 'user', 'content': 'Observation: ' + obs + '\nRespond with the next action only.'}]
        messages += [dict(messages[2]), dict(messages[3])]
        before = [dict(m) for m in messages]
        actual = p.fit(messages)
        self.assertLessEqual(p.count(actual), 800)
        self.assertEqual(messages, before)
        for i in [0, 1, 2, 4]:
            self.assertEqual(actual[i], messages[i])
        for i in [3, 5]:
            for rank in range(1, 6):
                self.assertIn(f'Result {rank} |', actual[i]['content'])
            self.assertNotIn('\ufffd', actual[i]['content'])
        self.assertTrue(p.events[-1]['truncated'])

    def test_short_prompt_unchanged(self):
        p = PlannerContext()
        messages = [{'role': 'user', 'content': 'Short question'}]
        self.assertEqual(p.fit(messages), messages)

    def test_oversize_question_is_never_silently_cut(self):
        with self.assertRaises(ValueError):
            PlannerContext(limit=20).fit([{'role': 'user', 'content': 'question ' * 1000}])


if __name__ == '__main__':
    unittest.main()
