"""Safety checks use MockTransport only; zero paid/network calls."""
import json
import unittest

import httpx

from reproduction.real_minimal import Budget, CappedTransport, StopRun

URL='https://synthetic.invalid/v1/chat/completions'
GOOD={'model':'gpt-4o-mini','choices':[{'message':{'content':'synthetic'}}], 'usage':{'prompt_tokens':10,'completion_tokens':1}}


class Guards(unittest.TestCase):
    def setUp(self):
        self.sends=0
        self.budget=Budget()

    def transport(self, response):
        def handle(request):
            self.sends+=1
            if isinstance(response,Exception):raise response
            return response
        client=httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        return CappedTransport(self.budget,'llm',URL,'gpt-4o-mini',client=client)

    def request(self,model='gpt-4o-mini'):
        return httpx.Request('POST',URL,json={'model':model})

    def test_five_requests_then_block_without_sixth_io(self):
        t=self.transport(httpx.Response(200,json=GOOD))
        for _ in range(5):t.handle_request(self.request())
        with self.assertRaises(StopRun):t.handle_request(self.request())
        self.assertEqual(self.sends,5)
        self.assertEqual(self.budget.report['attempts']['llm'],5)

    def test_historical_requests_reduce_remaining_budget(self):
        self.budget=Budget(prior={'llm':2,'embedding':1})
        t=self.transport(httpx.Response(200,json=GOOD))
        for _ in range(3):t.handle_request(self.request())
        with self.assertRaises(StopRun):t.handle_request(self.request())
        self.assertEqual(self.sends,3)
        self.assertEqual(self.budget.report['cumulative_attempts']['llm'],5)
        self.assertEqual(self.budget.report['limits']['embedding'],4)

    def test_first_http_error_latches_all_channels(self):
        t=self.transport(httpx.Response(401,text='sensitive-data'))
        with self.assertRaises(StopRun):t.handle_request(self.request())
        with self.assertRaises(StopRun):t.handle_request(self.request())
        self.assertEqual(self.sends,1)
        self.assertNotIn('sensitive-data',json.dumps(self.budget.report))
        other=CappedTransport(self.budget,'embedding',URL,'text-embedding-v4',client=t.client)
        with self.assertRaises(StopRun):other.handle_request(self.request('text-embedding-v4'))
        self.assertEqual(self.sends,1)

    def test_model_mismatch_stops(self):
        t=self.transport(httpx.Response(200,json=dict(GOOD,model='another-model')))
        with self.assertRaises(StopRun):t.handle_request(self.request())
        self.assertFalse(self.budget.report['calls'][0]['model_matches'])

    def test_documented_snapshot_accepted_and_recorded(self):
        t=self.transport(httpx.Response(200,json=dict(GOOD,model='gpt-4o-mini-2024-07-18')))
        t.handle_request(self.request())
        self.assertTrue(self.budget.report['calls'][0]['model_matches'])
        self.assertEqual(self.budget.report['calls'][0]['returned_model'],'gpt-4o-mini-2024-07-18')
        self.assertIsNone(self.budget.report['stopped'])

    def test_unknown_snapshot_is_not_accepted_by_prefix(self):
        t=self.transport(httpx.Response(200,json=dict(GOOD,model='gpt-4o-mini-2099-01-01')))
        with self.assertRaises(StopRun):t.handle_request(self.request())
        self.assertFalse(self.budget.report['calls'][0]['model_matches'])

    def test_network_error_stops_and_redacts(self):
        t=self.transport(httpx.ConnectError('sensitive-error'))
        with self.assertRaises(StopRun):t.handle_request(self.request())
        with self.assertRaises(StopRun):t.handle_request(self.request())
        self.assertEqual(self.sends,1)
        self.assertNotIn('sensitive-error',json.dumps(self.budget.report))

    def test_retry_header_blocked_before_io(self):
        t=self.transport(httpx.Response(200,json=GOOD))
        req=self.request();req.headers['x-stainless-retry-count']='1'
        with self.assertRaises(StopRun):t.handle_request(req)
        self.assertEqual(self.sends,0)

    def test_malformed_success_latches(self):
        t=self.transport(httpx.Response(200,json={'model':'gpt-4o-mini'}))
        with self.assertRaises(StopRun):t.handle_request(self.request())
        self.assertEqual(self.budget.report['stopped'],'llm_invalid_response_shape')


if __name__=='__main__':unittest.main()
