import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import httpx
    import openai
except ImportError:
    httpx = openai = None

from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.runtime.artifacts import read_json
from memory_benchmark.runtime.budget import make_transport
from memory_benchmark.runtime.model_client import Gateway


@unittest.skipUnless(httpx and openai, "Gateway tests require API extra environment")
class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = read_json(ROOT / "configs/runtime.json")
        self.profile = {"OPENAI_BASE_URL": "https://provider.example/v1", "AMI_EMBED_BASE_URL": "https://embedding.example/v1",
            "OPENAI_API_KEY": "SECRET_LLM", "AMI_EMBED_API_KEY": "SECRET_EMBED", "AMI_LLM_MODEL": "gpt-4o-mini"}
        self.requests = []
        self.embedding_error = None
        self.model, self.status = "gpt-4o-mini-2024-07-18", 200
        self.finish_reason = "stop"
        self.original = openai.OpenAI

    def tearDown(self):
        openai.OpenAI = self.original
        self.temp.cleanup()

    def mocked_transport(self, *args, **kwargs):
        transport = make_transport(*args, **kwargs)
        transport.client.close()
        def respond(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            if str(request.url).endswith('/embeddings'):
                dimensions = payload['dimensions']
                data = [{'object': 'embedding', 'index': i, 'embedding': [float(i+1)] + [0.0]*(dimensions-1)}
                        for i in range(len(payload['input']))][::-1]
                body = {'object': 'list', 'model': 'text-embedding-v4', 'data': data,
                        'usage': {'prompt_tokens': 5, 'total_tokens': 5}}
                if self.embedding_error == 'dimensions':data[0]['embedding'].pop()
                if self.embedding_error == 'indices':data[0]['index'] = -1
                if self.embedding_error == 'model':body['model'] = 'other-embedding'
                if self.embedding_error == 'missing':body['data'] = []
                if self.embedding_error == 'zero':data[0]['embedding'] = [0.0] * dimensions
                return httpx.Response(200, request=request, json=body)
            return httpx.Response(self.status, request=request, json={"id": "test", "object": "chat.completion", "created": 1,
                "model": self.model, "choices": [{"index": 0, "finish_reason": self.finish_reason,
                "message": {"role": "assistant", "content": "answer"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
        transport.client = httpx.Client(transport=httpx.MockTransport(respond))
        return transport

    def gateway(self, runtime=None):
        with patch("memory_benchmark.runtime.model_client.read_profile", return_value=self.profile), \
             patch("memory_benchmark.runtime.model_client.make_transport", side_effect=self.mocked_transport):
            return Gateway(runtime or self.runtime, Path(self.temp.name) / "usage.json")

    def test_shared_transport_survives_native_client_close_and_pins_gpt(self):
        gateway = self.gateway()
        try:
            gateway.install_native()
            native = openai.OpenAI(api_key="SECRET_LLM", base_url=self.profile["OPENAI_BASE_URL"])
            native.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hello"}],
                extra_body={"enable_thinking": False, "top_k": 20})
            native.close()
            reply = gateway.complete("reader", stage="answer", max_tokens=12, temperature=0)
            self.assertEqual(reply.model, self.model)
            self.assertEqual(len(gateway.budget.rows), 2)
            self.assertTrue(all(r['requested_model'] == 'gpt-4o-mini' for r in gateway.budget.rows))
            for request in self.requests:
                self.assertEqual(request["model"], "gpt-4o-mini")
                self.assertEqual(request["seed"], 17)
                self.assertNotIn("enable_thinking", request)
                self.assertNotIn("top_k", request)
        finally:
            gateway.close()
        self.assertIs(openai.OpenAI, self.original)
        self.assertNotIn("SECRET", (Path(self.temp.name) / "usage.json").read_text())

    def test_wrong_returned_model_stops_further_calls_and_preserves_usage(self):
        self.model = "qwen-plus"
        gateway = self.gateway()
        try:
            for i in range(2):
                with self.assertRaises(Exception):
                    gateway.complete("hello", stage="answer", max_tokens=12, temperature=0)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(gateway.budget.rows[0]["input_tokens"], 5)
            self.assertEqual(gateway.budget.rows[0]["validation_error"], "response_model_not_allowed")
            self.assertEqual(gateway.budget.rows[0]["returned_model"], "qwen-plus")
            self.assertIsNotNone(gateway.budget.failed)
        finally:
            gateway.close()

    def test_unrestricted_answer_uses_model_maximum_without_changing_memory_or_judge(self):
        gateway = self.gateway({**self.runtime, "answer_max_tokens": None})
        try:
            gateway.install_native()
            gateway.budget.phase = "add"
            native = openai.OpenAI(api_key="SECRET_LLM", base_url=self.profile["OPENAI_BASE_URL"])
            native.chat.completions.create(model="gpt-4o-mini",
                messages=[{"role": "user", "content": "remember this"}])
            native.close()
            gateway.complete("answer fully", stage="answer", max_tokens=None, temperature=0)
            gateway.complete("judge", stage="judge", max_tokens=8192, temperature=0)
            self.assertEqual([r["max_tokens"] for r in self.requests],
                             [self.runtime["memory_default_max_tokens"], 16384, 8192])
            self.assertEqual(gateway.budget.rows[1]["output_token_bound"], 16384)
        finally:
            gateway.close()

    def test_unset_judge_cap_is_rejected_before_request(self):
        gateway = self.gateway()
        try:
            with self.assertRaisesRegex(ValueError, "Only Answer"):
                gateway.complete("judge", stage="judge", max_tokens=None, temperature=0)
            self.assertEqual(self.requests, [])
        finally:
            gateway.close()

    def test_raw_judge_responses_survive_truncation_and_multiple_calls(self):
        gateway = self.gateway()
        try:
            gateway.complete("judge", stage="judge", max_tokens=12, temperature=0)
            self.finish_reason = "length"
            with self.assertRaisesRegex(ValueError, "truncated"):
                gateway.complete("judge again", stage="judge", max_tokens=12, temperature=0)
            responses = sorted(Path(self.temp.name).glob("usage.judge-*.response.json"))
            self.assertEqual(len(responses), 2)
            self.assertEqual([read_json(p)["choices"][0]["finish_reason"] for p in responses], ["stop", "length"])
            self.assertEqual(read_json(responses[1])["choices"][0]["message"]["content"], "answer")
            self.assertEqual(read_json(responses[1])["usage"]["completion_tokens"], 2)
            self.assertEqual(len(read_json(Path(self.temp.name) / "usage.json")["calls"]), 2)
        finally:
            gateway.close()

    def test_provider_error_has_no_retry(self):
        self.status = 429
        gateway = self.gateway()
        try:
            with self.assertRaises(Exception):
                gateway.complete("hello", stage="answer", max_tokens=12, temperature=0)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(gateway.budget.rows[0]["http_status"], 429)
        finally:
            gateway.close()

    def test_unlimited_requests_and_costs_ignore_retired_config_limits(self):
        runtime = {**self.runtime, "request_limits": {"llm": 1, "embedding": 1},
                   "estimated_cny_cap": 0.000001, "per_memory_worker_cny_cap": 0.000001,
                   "rates_cny_per_million": {"input": 1000000, "output": 1000000, "embedding": 1000000}}
        gateway = self.gateway(runtime)
        try:
            for i in range(101):
                gateway.complete("hello", stage="answer" if i % 2 else "judge", max_tokens=12, temperature=0)
                gateway.embed(["hello"])
            self.assertEqual(len(self.requests), 202)
            self.assertIsNone(gateway.budget.failed)
        finally:
            gateway.close()
        usage = read_json(Path(self.temp.name) / "usage.json")
        self.assertEqual(usage['request_and_cost_limits'], 'none')
        self.assertGreater(usage['estimated_cny'], 1)
        self.assertNotIn('estimated_cny_cap', usage)

    def test_embedding_uses_shared_endpoint_dimensions_order_and_batch_size(self):
        gateway = self.gateway()
        try:
            gateway.budget.phase = 'add'
            texts = ['hello\nworld'] * 12
            vectors = gateway.embed(texts)
            self.assertEqual(len(vectors), 12)
            self.assertTrue(all(len(v) == 1024 and v[0] == 1 for v in vectors))
            self.assertEqual([len(r['input']) for r in self.requests], [10, 2])
            self.assertEqual(self.requests[0]['input'][0], 'hello\nworld')
            self.assertTrue(all(r['dimensions'] == 1024 for r in self.requests))
            self.assertTrue(all(r['phase'] == 'add' and r['kind'] == 'embedding' for r in gateway.budget.rows))
        finally:
            gateway.close()
        self.assertNotIn('SECRET', (Path(self.temp.name) / 'usage.json').read_text())

    def test_invalid_embedding_response_stops_later_requests(self):
        for problem in ('dimensions', 'indices', 'model', 'missing', 'zero'):
            with self.subTest(problem=problem):
                self.requests.clear()
                self.embedding_error = problem
                gateway = self.gateway()
                try:
                    for attempt in range(2):
                        with self.assertRaises(Exception):
                            gateway.embed(['hello'])
                    self.assertEqual(len(self.requests), 1)
                    self.assertIsNotNone(gateway.budget.failed)
                finally:
                    gateway.close()


if __name__ == "__main__":
    unittest.main()
