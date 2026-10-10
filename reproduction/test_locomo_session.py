"""Network-free checks of the paid smoke test's stopping conditions."""
import json
import unittest

import httpx

from .locomo_session import CallBudget, make_transport, select_session, DATASET


class SmokeGuards(unittest.TestCase):
    def make(self, maximum=1, status=200, usage=True, returned_model='qwen3-8b'):
        budget = CallBudget({'AMI_TEST_INPUT_CNY_PER_MILLION': '0.5',
            'AMI_TEST_OUTPUT_CNY_PER_MILLION': '2', 'AMI_TEST_EMBED_CNY_PER_MILLION': '0.5'}, maximum)
        transport = make_transport(budget, 'llm', 'https://test.invalid/v1/chat/completions', 'qwen3-8b')
        transport.client.close()
        self.calls = 0

        def handler(request):
            self.calls += 1
            body = {'model': returned_model, 'choices': [{'message': {'content': 'fixture'}}]}
            if usage:
                body['usage'] = {'prompt_tokens': 10, 'completion_tokens': 2}
            return httpx.Response(status, json=body, request=request)

        transport.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(transport.close)
        return budget, transport

    def request(self, **changes):
        body = {'model': 'qwen3-8b', 'messages': [{'role': 'user', 'content': 'test'}],
                'max_tokens': 20, 'enable_thinking': False, **changes}
        return httpx.Request('POST', 'https://test.invalid/v1/chat/completions', content=json.dumps(body))

    def test_budget_stops_before_network(self):
        budget, transport = self.make(maximum=0.000001)
        with self.assertRaises(RuntimeError):
            transport.handle_request(self.request())
        self.assertEqual(self.calls, 0)
        self.assertEqual(budget.rows, [])

    def test_failure_latches(self):
        budget, transport = self.make(status=429)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                transport.handle_request(self.request())
        self.assertEqual(self.calls, 1)

    def test_provider_model_error_is_preserved(self):
        budget, transport = self.make()
        transport.client.close()
        transport.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404,
            json={'error': {'code': 'model_not_found', 'message': 'private body not retained'}}, request=request)))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 404.*model_not_found'):
            transport.handle_request(self.request())
        self.assertEqual(budget.rows[0]['provider_error_code'], 'model_not_found')
        self.assertNotIn('private body', str(budget.rows))

    def test_missing_usage_stops(self):
        budget, transport = self.make(usage=False)
        with self.assertRaises(RuntimeError):
            transport.handle_request(self.request())
        self.assertIsNotNone(budget.failed)

    def test_wrong_response_model_stops(self):
        budget, transport = self.make(returned_model='other-model')
        with self.assertRaises(RuntimeError):
            transport.handle_request(self.request())
        self.assertIsNotNone(budget.failed)

    def test_thinking_stops_before_network(self):
        _, transport = self.make()
        with self.assertRaises(RuntimeError):
            transport.handle_request(self.request(enable_thinking=True))
        self.assertEqual(self.calls, 0)

    def test_usage_cost_recorded(self):
        budget, transport = self.make()
        response = transport.handle_request(self.request())
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(budget.rows[0]['estimated_cny'], 0.000009)

    def test_completed_reservation_reconciles_to_actual_usage(self):
        budget, transport = self.make(maximum=0.0004)
        for _ in range(2):
            transport.handle_request(self.request())
        self.assertEqual(self.calls, 2)
        self.assertAlmostEqual(budget.reserved, 0.000018)
        self.assertLessEqual(budget.peak_reserved, budget.maximum)

    @unittest.skipUnless(DATASET.exists(), 'Local dataset not installed')
    def test_question_evidence_is_session_local(self):
        conversation, session, messages, questions = select_session(DATASET)
        self.assertEqual(conversation['sample_id'], 'conv-26')
        self.assertEqual(session['session_index'], 1)
        self.assertEqual(len(messages), 18)
        self.assertEqual(len(questions), 2)
        ids = {m['dia_id'] for m in messages}
        for q in questions:
            self.assertTrue(set(q['evidence']) <= ids)
            self.assertFalse(q['is_multi_modality'])


if __name__ == '__main__':
    unittest.main()
