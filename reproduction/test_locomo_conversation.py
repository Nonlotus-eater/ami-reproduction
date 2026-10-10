"""Offline checks of full-conversation alignment and judge price accounting."""
import json
from pathlib import Path
import unittest
import tempfile
from types import SimpleNamespace

import httpx

from .locomo_conversation import DATASET, lightmem_reader, load_conversation, make_add, message_text, reader_context, score_official
from .locomo_session import CallBudget, QwenCompletions, make_transport


@unittest.skipUnless(DATASET.exists(), 'Local LoCoMo/LightMem workspace missing')
class ConversationAlignment(unittest.TestCase):
    def test_all_sessions_and_questions_match_original_conv(self):
        conversation, questions = load_conversation(DATASET, 'conv-26')
        raw = next(c for c in json.loads((DATASET / 'data/raw/locomo_refined.json').read_text()) if c['sample_id'] == 'conv-26')
        self.assertEqual(len(conversation['sessions']), 19)
        self.assertEqual(sum(len(s['messages']) for s in conversation['sessions']), 419)
        self.assertEqual(len(questions), 138)
        self.assertEqual([q['question'] for q in questions], [q['question'] for q in raw['qa']])
        self.assertEqual([q['qa_id'] for q in questions], [f'conv-26#q{i:04d}' for i in range(138)])

    def test_reader_reuses_actual_lightmem_prompt(self):
        formatter, hashes = lightmem_reader(DATASET.parents[1])
        prompt = formatter(speaker_1_name='Alice', speaker_1_memories='fixture-a',
                           speaker_2_name='Bob', speaker_2_memories='fixture-b', question='fixture-question')
        self.assertIn('fixture-a', prompt)
        self.assertIn('fixture-question', prompt)
        self.assertNotIn('# APPROACH (Think step by step):', prompt)
        self.assertEqual(len(hashes), 2)

    def test_ingestion_preserves_speakers_date_and_caption(self):
        conversation, _ = load_conversation(DATASET, 'conv-26')
        session = conversation['sessions'][0]
        payload = make_add(conversation, session, 'test-user')
        self.assertEqual(len(payload['messages']), 18)
        self.assertTrue(all(type(m['timestamp']) is int for m in payload['messages']))
        self.assertIn('Caroline', payload['messages'][0]['content'])
        self.assertIn('D1:1', payload['messages'][0]['content'])
        self.assertIn('(image description: fixture)', message_text(
            {'dia_id': 'D1:1', 'speaker': 'Alice', 'text': 'hello', 'blip_caption': 'fixture'}))

    def test_reader_does_not_invent_speaker_for_shared_fact(self):
        fields = reader_context([{'id': 'fact', 'content': 'A shared event.'}],
            {'speaker_a': 'Alice', 'speaker_b': 'Bob'}, {})
        for key in ['speaker_1_memories', 'speaker_2_memories']:
            self.assertIn('Shared or unattributed evidence', fields[key])

    def test_official_scorer_and_judge_parser_with_mock_api(self):
        _, questions = load_conversation(DATASET, 'conv-26')
        selected = questions[:2]
        calls = []

        def create(**kwargs):
            self.assertEqual(kwargs['model'], 'qwen3-14b')
            self.assertEqual(kwargs['max_tokens'], 128)
            self.assertFalse(kwargs['extra_body']['enable_thinking'])
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"label":"CORRECT"}'))])

        budget = CallBudget({'AMI_TEST_INPUT_CNY_PER_MILLION': '0.5',
            'AMI_TEST_OUTPUT_CNY_PER_MILLION': '2', 'AMI_TEST_EMBED_CNY_PER_MILLION': '0.5'}, 1)
        report = {}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / 'questions.jsonl').write_text(''.join(json.dumps(q) + '\n' for q in selected))
            (output / 'predictions.jsonl').write_text(''.join(json.dumps(
                {'qa_id': q['qa_id'], 'predicted_answer': 'fixture prediction'}) + '\n' for q in selected))
            summary = score_official(DATASET, output, QwenCompletions(SimpleNamespace(create=create)),
                                     report, lambda: None, budget)
            records = [json.loads(line) for line in (output / 'predictions_scored.jsonl').read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertTrue(all(row['llm_score'] == 1 for row in records))
            self.assertEqual(summary['record_count'], 2)
            self.assertEqual(summary['primary_metric'], 'llm_score')
            self.assertEqual(len(calls), 2)


class JudgeGuard(unittest.TestCase):
    def test_judge_uses_14b_prices_and_nonthinking_guard(self):
        budget = CallBudget({'AMI_TEST_INPUT_CNY_PER_MILLION': '0.5',
            'AMI_TEST_OUTPUT_CNY_PER_MILLION': '2', 'AMI_TEST_EMBED_CNY_PER_MILLION': '0.5',
            'AMI_TEST_JUDGE_INPUT_CNY_PER_MILLION': '1', 'AMI_TEST_JUDGE_OUTPUT_CNY_PER_MILLION': '4'},
            1, limits={'judge': 2})
        transport = make_transport(budget, 'judge', 'https://test.invalid/v1/chat/completions', 'qwen3-14b')
        transport.client.close()
        transport.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={'model': 'qwen3-14b', 'choices': [{'message': {'content': '{"label":"CORRECT"}'}}],
                  'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}, request=request)))
        try:
            transport.handle_request(httpx.Request('POST', 'https://test.invalid/v1/chat/completions',
                json={'model': 'qwen3-14b', 'messages': [{'role': 'user', 'content': 'fixture'}],
                      'max_tokens': 128, 'enable_thinking': False}))
            self.assertAlmostEqual(budget.rows[0]['estimated_cny'], 0.000018)
            self.assertAlmostEqual(budget.reserved, 0.000018)
        finally:
            transport.close()


if __name__ == '__main__':
    unittest.main()
