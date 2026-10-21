"""Regression checks for runtime."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.runtime.artifacts import read_json
from memory_benchmark.runtime.budget import make_transport
from memory_benchmark.runtime.embedding import chunks, encode, normalized, settings
from memory_benchmark.runtime.model_client import Gateway
from memory_benchmark.runtime.pacing import paced_transport
from memory_benchmark.runtime.profile import public_identity


try:
    import openai
except ImportError:
    openai = None


@unittest.skipUnless(openai, 'Gateway tests require API extra environment')
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

    def test_recovery_preserves_actual_http_status_without_sdk_connection_wrapping(self):
        self.status = 429
        gateway = self.gateway({**self.runtime, "provider_error_diagnostics": True})
        try:
            with self.assertRaises(openai.RateLimitError):
                gateway.complete("reader", stage="answer", max_tokens=12, temperature=0)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(gateway.budget.rows[0]["http_status"], 429)
            self.assertEqual(gateway.budget.rows[0]["error_type"], "HTTPStatusError")
        finally:
            gateway.close()

    def test_diagnostics_redact_credentials_bound_messages_and_do_not_retry(self):
        for status in (400, 429, 500):
            with self.subTest(status=status):
                gateway = self.gateway({**self.runtime, 'provider_error_diagnostics': True})
                calls = []
                def error(request):
                    calls.append(request)
                    return httpx.Response(status, request=request, json={'error': {
                        'code': 'invalid_parameter',
                        'message': 'SECRET_LLM SECRET_EMBED ' + 'diagnostic ' * 300}})
                transport = gateway.transports['llm']
                transport.client.close()
                transport.client = httpx.Client(transport=httpx.MockTransport(error))
                try:
                    with self.assertRaises(Exception):
                        gateway.complete('fixture', stage='answer', max_tokens=12, temperature=0)
                    self.assertEqual(len(calls), 1)
                    row = gateway.budget.rows[-1]
                    self.assertEqual(row['http_status'], status)
                    self.assertEqual(row['provider_error_code'], 'invalid_parameter')
                    self.assertEqual(row['error_type'], 'HTTPStatusError')
                    self.assertIn('[REDACTED]', row['provider_error_message'])
                    self.assertLessEqual(len(row['provider_error_message']), 1600)
                    self.assertNotIn('SECRET_', gateway.output.read_text())
                    with self.assertRaises(Exception):
                        gateway.complete('again', stage='answer', max_tokens=12, temperature=0)
                    self.assertEqual(len(calls), 1)
                finally:
                    gateway.close()

    def test_recovery_saves_distinct_intermediate_and_final_answer_responses(self):
        gateway = self.gateway({**self.runtime, "persist_answer_responses": True})
        try:
            gateway.complete("part", stage="answer_map", max_tokens=12, temperature=0)
            gateway.complete("native final prompt", stage="answer", max_tokens=12, temperature=0)
            folder = Path(self.temp.name)
            self.assertEqual(read_json(folder / "usage.answer-001.request.json")["stage"], "answer_map")
            self.assertEqual(read_json(folder / "usage.answer-002.request.json")["stage"], "answer")
            self.assertTrue((folder / "usage.answer-001.response.json").exists())
            self.assertTrue((folder / "usage.answer-002.response.json").exists())
        finally:
            gateway.close()

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


class EmbeddingTests(unittest.TestCase):
    def test_multilingual_chunks_preserve_every_byte_and_whitespace(self):
        text = ("中文🙂\n  abc\t" * 600)
        parts = chunks(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part.encode("utf-8")) <= 2048 for part in parts))

    def test_batching_crosses_text_boundaries_and_preserves_order(self):
        texts = ["a" * 4096, "b", "c"]
        calls = []
        def request(payload):
            calls.append(list(payload))
            return [[1, 0] if part[0] == "a" else [0, 1] for part in payload]
        vectors = encode(texts, request, 2, 3)
        self.assertEqual([len(c) for c in calls], [3, 1])
        self.assertEqual(vectors, [[1, 0], [0, 1], [0, 1]])

    def test_long_text_pooling_uses_byte_weights_and_normalizes_chunks(self):
        def request(payload):
            return [[100, 0] if part[0] == "a" else [0, 2] for part in payload]
        vector, = encode(["a" * 2048 + "b" * 1024], request, 2, 10)
        self.assertAlmostEqual(vector[0], 2 / math.sqrt(5))
        self.assertAlmostEqual(vector[1], 1 / math.sqrt(5))

    def test_invalid_texts_are_rejected_before_any_request(self):
        calls = []
        with self.assertRaises(ValueError):
            encode(["valid", ""], lambda p: calls.append(p), 2, 10)
        self.assertEqual(calls, [])
        self.assertEqual(encode([], lambda p: calls.append(p), 2, 10), [])

    def test_bad_vectors_cannot_enter_index(self):
        for vector in ([0, 0], [1], [float("nan"), 1], [float("inf"), 1], [True, 1]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                normalized(vector, 2)
        self.assertEqual(normalized([1e300, 0], 2), [1, 0])

    def test_profile_dimensions_batch_and_preprocessing_are_frozen_identity(self):
        profile = {"OPENAI_BASE_URL": "https://llm.example/v1", "AMI_EMBED_BASE_URL": "https://embed.example/v1"}
        first = public_identity(profile, {"model": "gpt-4o-mini"})
        second = public_identity({**profile, "AMI_EMBED_DIMENSIONS": "512"}, {"model": "gpt-4o-mini"})
        self.assertNotEqual(first, second)
        self.assertEqual(first["embedding_dimensions"], 1024)
        self.assertIn("embedding_preprocess", first)
        for changes in ({"AMI_EMBED_DIMENSIONS": "384"}, {"AMI_EMBED_BATCH": "11"},
                        {"AMI_EMBED_MODEL": "other"}, {"AMI_EMBED_BACKEND": "bge"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings({**profile, **changes})


class ProviderPacingTests(unittest.TestCase):
    def test_overload_cooldown_is_shared_and_increases_until_a_success(self):
        with tempfile.TemporaryDirectory() as folder:
            from pathlib import Path
            current = [100.0]
            starts, statuses = [], iter([503, 503, 200])
            def respond(request):
                starts.append(current[0])
                return httpx.Response(next(statuses), request=request)
            policy = {"state_directory": folder, "minimum_interval_seconds": 2,
                      "overload_cooldown_seconds": 180, "maximum_cooldown_seconds": 900}
            def sleep(seconds):
                current[0] += seconds
            transport = paced_transport(httpx.MockTransport(respond), policy,
                clock=lambda: current[0], sleeper=sleep)
            for _ in range(3):
                transport.handle_request(httpx.Request("POST", "https://provider.example/chat/completions"))
            self.assertEqual(starts, [100, 280, 640])
            state = read_json(Path(folder) / "state.json")
            self.assertEqual(state["overload_events"], 2)
            self.assertEqual(state["consecutive_overload"], 0)
            self.assertEqual(state["in_flight"], 0)
            transport.close()

    def test_independent_gateway_transports_never_overlap_provider_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            active, peak = [0], [0]
            lock = threading.Lock()
            def respond(request):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(.01)
                with lock:
                    active[0] -= 1
                return httpx.Response(200, request=request)
            policy = {"state_directory": folder, "minimum_interval_seconds": .01,
                      "overload_cooldown_seconds": 180, "maximum_cooldown_seconds": 900}
            transports = [paced_transport(httpx.MockTransport(respond), policy) for _ in range(3)]
            def send(transport):
                return transport.handle_request(httpx.Request("POST", "https://provider.example/chat/completions")).status_code
            with ThreadPoolExecutor(max_workers=3) as pool:
                self.assertEqual(list(pool.map(send, transports)), [200, 200, 200])
            self.assertEqual(peak[0], 1)
            for transport in transports:
                transport.close()


if __name__ == "__main__":
    unittest.main()
