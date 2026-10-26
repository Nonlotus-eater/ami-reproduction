"""Fault injection and real loopback HTTP verification for the retry candidate."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import httpx
import openai
from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.runtime import model_client, retry
from memory_benchmark.runtime.artifacts import read_json
from memory_benchmark.runtime.budget import make_transport
from memory_benchmark.runtime.pacing import paced_transport
from memory_benchmark.runtime.profile import public_identity
from memory_benchmark.runtime.memory_retry import gateway_for_reader


def success(payload):
    if payload['model'] == 'text-embedding-v4':
        return {'object': 'list', 'model': payload['model'], 'data': [
            {'object': 'embedding', 'index': i, 'embedding': [1.0] + [0.0] * (payload['dimensions'] - 1)}
            for i in range(len(payload['input']))], 'usage': {'prompt_tokens': 1, 'total_tokens': 1}}
    return {'id': 'local', 'object': 'chat.completion', 'created': 1, 'model': 'gpt-4o-mini-2024-07-18',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'recovered'}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


def complete(gateway, stage='answer'):
    return gateway.complete('same prompt', stage=stage, max_tokens=16, temperature=0)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = {**read_json(ROOT / 'configs/runtime.json'), 'max_retries': 2,
                        'provider_error_diagnostics': True, 'persist_answer_responses': False}
        self.profile = {'OPENAI_BASE_URL': 'https://provider.invalid/v1', 'AMI_EMBED_BASE_URL': 'https://embedding.invalid/v1',
                        'OPENAI_API_KEY': 'FAKE_LLM_SECRET', 'AMI_EMBED_API_KEY': 'FAKE_EMBED_SECRET', 'AMI_ALLOW_RETRIES': '1'}
        self.requests, self.sleeps, self.flags = [], [], []
        self.steps, self.current, self.number = [503, 200], None, 0

    def respond(self, request):
        self.requests.append(request)
        self.flags.append(self.current.budget.failed)
        payload = json.loads(request.content)
        step = self.steps[min(len(self.requests) - 1, len(self.steps) - 1)]
        if isinstance(step, str):
            error = {'timeout': httpx.ReadTimeout, 'connect': httpx.ConnectError,
                     'protocol': httpx.RemoteProtocolError, 'unsupported': httpx.UnsupportedProtocol}[step]
            raise error('simulated transport failure', request=request)
        options = step if isinstance(step, dict) else {'status': step}
        status = options.get('status', 200)
        if options.get('invalid_json'):
            return httpx.Response(status, request=request, content=b'not json')
        body = success(payload) if status == 200 else {'error': {'code': options.get('code', 'rate_limit_exceeded'),
                                                                 'message': options.get('message', 'temporary')}}
        if 'usage' in options: body['usage'] = options['usage']
        if options.get('wrong_model'): body['model'] = 'wrong-model'
        if options.get('empty'): body['choices'][0]['message']['content'] = ''
        if options.get('bad_embedding'): body['data'][0]['embedding'].pop()
        return httpx.Response(status, request=request, headers=options.get('headers'), json=body)

    def factory(self, *args, **kwargs):
        transport = make_transport(*args, **kwargs)
        transport.client.close()
        transport.client = httpx.Client(transport=httpx.MockTransport(self.respond))
        return transport

    @contextmanager
    def gateway(self, **changes):
        self.number += 1
        with patch.object(model_client, 'read_profile', return_value=dict(self.profile)), \
                patch.object(model_client, 'make_transport', side_effect=self.factory):
            gateway = model_client.Gateway({**self.runtime, **changes}, Path(self.temp.name) / f'usage-{self.number}.json')
        self.current = gateway
        with patch.object(retry, 'sleep', side_effect=self.sleeps.append):
            try: yield gateway
            finally: gateway.close()

    def assert_accounting(self, gateway):
        rows = gateway.budget.rows
        expected = sum(row.get('estimated_cny', row['reserved_cny']) for row in rows)
        self.assertGreaterEqual(gateway.budget.reserved, 0)
        self.assertAlmostEqual(gateway.budget.reserved, expected, places=12)
        self.assertTrue(all(row['settled'] for row in rows))
        usage = read_json(gateway.output)
        self.assertAlmostEqual(usage['reserved_cny'], expected, places=12)
        self.assertEqual(usage['unknown_usage_attempts'], sum(r['usage_status'] == 'unknown' for r in rows))

    def test_transient_http_recovers_without_latching_or_changing_request(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.requests.clear(); self.flags.clear()
                self.steps = [status, 200]
                with self.gateway() as g:
                    self.assertEqual(complete(g).text, 'recovered')
                    self.assertEqual(len(self.requests), 2)
                    self.assertEqual(self.requests[0].content, self.requests[1].content)
                    self.assertEqual(self.flags, [None, None])
                    self.assertEqual([r.headers.get('x-stainless-retry-count', '0') for r in self.requests], ['0', '0'])
                    self.assertEqual([r['retry_count'] for r in g.budget.rows], [0, 1])
                    self.assertEqual(g.client.max_retries, 0)
                    self.assertIsNone(g.budget.failed)
                    self.assert_accounting(g)

    def test_ten_retries_means_eleven_sends_then_latches(self):
        self.steps = [503]
        with self.gateway(max_retries=10) as g:
            with self.assertRaises(openai.InternalServerError): complete(g)
            self.assertEqual(len(self.requests), 11)
            self.assertEqual(len(self.sleeps), 10)
            self.assertTrue(all(0 <= delay <= 60 for delay in self.sleeps))
            self.assertEqual(g.client.max_retries, 0)
            with self.assertRaises(RuntimeError): g.budget.check()
            with self.assertRaises(Exception): complete(g)
            self.assertEqual(len(self.requests), 11)
            self.assertTrue(g.budget.rows[-1]['retry_exhausted'])
            self.assert_accounting(g)

    def test_hundred_retries_means_101_sends_without_sdk_multiplication(self):
        self.steps = [503]
        with self.gateway(max_retries=100) as g:
            with self.assertRaises(openai.InternalServerError): complete(g)
            self.assertEqual(len(self.requests), 101)
            self.assertEqual(len(self.sleeps), 100)
            self.assertTrue(all(0 <= delay <= 60 for delay in self.sleeps))
            self.assertTrue(g.budget.rows[-1]['retry_exhausted'])
            self.assertEqual(g.client.max_retries, 0)
            self.assert_accounting(g)

    def test_reader_overlay_recovers_answer_and_judge_after_100_transient_errors(self):
        receipt = {'version': 1, 'activation': 'new_memory_workers',
                   'baseline_framework_sha256': 'fixture', 'changes': {},
                   'reader_request_retry_policy': {'version': 1, 'owner': 'transport',
                       'scope': 'new_reader_clients', 'max_retries': 100, 'activated_at': 'fixture'}}
        (Path(self.temp.name) / 'execution_overlay.json').write_text(json.dumps(receipt))
        for stage in ('answer', 'judge'):
            with self.subTest(stage=stage):
                self.requests.clear(); self.sleeps.clear(); self.steps = [503] * 100 + [200]
                with self.gateway(max_retries=0) as g:
                    identity = json.loads(json.dumps(g.identity))
                    runtime = json.loads(json.dumps(g.runtime))
                    current = gateway_for_reader(g.runtime, g.output, lambda *a, **kw: g,
                                                 expected_identity=g.identity)
                    self.assertIs(current, g)
                    self.assertEqual(complete(g, stage).text, 'recovered')
                    self.assertEqual(len(self.requests), 101)
                    self.assertIsNone(g.budget.failed)
                    self.assertEqual(g.identity, identity)
                    self.assertEqual(g.runtime, runtime)
                    usage = read_json(g.output)
                    self.assertEqual(usage['request_retry_policy']['max_attempts'], 101)
                    self.assertEqual(usage['reader_retry_activation']['activation'], 'new_reader_clients')
                    self.assert_accounting(g)

    def test_network_errors_recover(self):
        for error in ('timeout', 'connect', 'protocol'):
            with self.subTest(error=error):
                self.requests.clear(); self.steps = [error, 200]
                with self.gateway() as g:
                    self.assertEqual(complete(g).text, 'recovered')
                    self.assertEqual(len(self.requests), 2)
                    self.assertIsNone(g.budget.failed)
                    self.assertEqual(g.budget.rows[0]['usage_status'], 'unknown')
                    self.assert_accounting(g)

    def test_timeout_exhaustion_preserves_original_error_and_latches(self):
        self.steps = ['timeout']
        with self.gateway() as g:
            with self.assertRaises(openai.APITimeoutError) as caught: complete(g)
            self.assertIsInstance(caught.exception.__cause__, httpx.ReadTimeout)
            self.assertEqual(len(self.requests), 3)
            with self.assertRaises(RuntimeError): g.budget.check()
            self.assert_accounting(g)

    def test_unsupported_protocol_is_not_retried(self):
        self.steps = ['unsupported']
        with self.gateway() as g:
            with self.assertRaises(openai.APIConnectionError) as caught: complete(g)
            self.assertIsInstance(caught.exception.__cause__, httpx.UnsupportedProtocol)
            self.assertEqual(len(self.requests), 1)
            self.assertIsNotNone(g.budget.failed)

    def test_fatal_http_and_quota_errors_send_once(self):
        for status, code in ((400, 'invalid_parameter'), (401, 'invalid_api_key'), (403, 'invalid_api_key'),
                             (404, 'model_not_found'), (429, 'insufficient_quota'), (503, 'invalid_api_key')):
            with self.subTest(status=status, code=code):
                self.requests.clear(); self.sleeps.clear(); self.steps = [{'status': status, 'code': code}]
                with self.gateway() as g:
                    with self.assertRaises(openai.APIStatusError): complete(g)
                    self.assertEqual(len(self.requests), 1)
                    self.assertEqual(self.sleeps, [])
                    self.assertIsNotNone(g.budget.failed)
                    self.assert_accounting(g)

    def test_provider_channel_unavailable_503_recovers_but_real_unknown_model_stops(self):
        cases = [(503, 'No available channel for model gpt-4o-mini under group test', 2),
                 (404, 'No available channel for model gpt-4o-mini under group test', 1),
                 (503, 'The requested model does not exist', 1)]
        for status, message, sends in cases:
            with self.subTest(status=status, message=message):
                self.requests.clear()
                self.steps = [{'status': status, 'code': 'model_not_found', 'message': message}, 200]
                with self.gateway() as g:
                    if sends == 2:
                        self.assertEqual(complete(g).text, 'recovered')
                        self.assertIsNone(g.budget.failed)
                        self.assertEqual(g.budget.rows[0]['retry_reason'], 'provider_channel_unavailable')
                    else:
                        with self.assertRaises(openai.APIStatusError): complete(g)
                        self.assertIsNotNone(g.budget.failed)
                    self.assertEqual(len(self.requests), sends)
                    self.assert_accounting(g)

    def test_invalid_success_response_is_fatal_with_usage_preserved(self):
        for options in ({'wrong_model': True}, {'empty': True}, {'usage': {'prompt_tokens': -1}}, {'invalid_json': True}):
            with self.subTest(options=options):
                self.requests.clear(); self.steps = [options, 200]
                with self.gateway() as g:
                    with self.assertRaises(Exception): complete(g)
                    with self.assertRaises(Exception): complete(g)
                    self.assertEqual(len(self.requests), 1)
                    self.assertIsNotNone(g.budget.failed)
                    if options.get('wrong_model') or options.get('empty'): self.assertIn('estimated_cny', g.budget.rows[0])
                    self.assert_accounting(g)

    def test_both_retry_configuration_controls_take_effect(self):
        for flag, count, sends in (('0', 2, 1), ('1', 0, 1), ('1', 2, 2)):
            with self.subTest(flag=flag, count=count):
                self.requests.clear(); self.profile['AMI_ALLOW_RETRIES'] = flag; self.steps = [503, 200]
                with self.gateway(max_retries=count) as g:
                    if sends == 1:
                        with self.assertRaises(openai.InternalServerError): complete(g)
                    else: complete(g)
                    self.assertEqual(len(self.requests), sends)
                    self.assertEqual(g.client.max_retries, 0)

    def test_missing_flag_defaults_to_no_retries(self):
        self.profile.pop('AMI_ALLOW_RETRIES')
        with self.gateway() as g:
            with self.assertRaises(openai.InternalServerError): complete(g)
            self.assertEqual(len(self.requests), 1)

    def test_invalid_configuration_rejected_before_any_send(self):
        for value in (-1, 101, True, 1.5, '10', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with self.gateway(max_retries=value): self.fail('invalid configuration accepted')
        self.profile['AMI_ALLOW_RETRIES'] = 'true'
        with self.assertRaises(ValueError):
            with self.gateway(): self.fail('invalid flag accepted')
        self.assertEqual(self.requests, [])

    def test_native_client_and_clone_cannot_multiply_sdk_retries(self):
        with self.gateway() as g:
            g.install_native()
            with openai.OpenAI(api_key='FAKE_LLM_SECRET', base_url=self.profile['OPENAI_BASE_URL'], max_retries=10) as native:
                self.assertEqual(native.max_retries, 0)
                with native.with_options(max_retries=10) as cloned:
                    self.assertEqual(cloned.max_retries, 0)
                    cloned.chat.completions.create(model='gpt-4o-mini', max_tokens=16,
                        messages=[{'role': 'user', 'content': 'native request'}])
            self.assertEqual(len(self.requests), 2)
            self.assertIsNone(g.budget.failed)
            self.assert_accounting(g)

    def test_embedding_uses_the_same_retry_policy(self):
        with self.gateway() as g:
            self.assertEqual(g.embed(['remember'])[0], [1.0] + [0.0] * 1023)
            self.assertEqual(len(self.requests), 2)
            self.assertEqual(g.embedding_client.max_retries, 0)
            self.assert_accounting(g)

    def test_judge_saves_only_the_final_success(self):
        with self.gateway() as g:
            complete(g, 'judge')
            self.assertEqual(len(self.requests), 2)
            self.assertEqual(len(list(Path(self.temp.name).glob('*.judge-*.response.json'))), 1)
            self.assertTrue(all(r['phase'] == 'judge' for r in g.budget.rows))

    def test_invalid_embedding_response_remains_fatal(self):
        self.steps = [{'bad_embedding': True}, 200]
        with self.gateway() as g:
            with self.assertRaises(Exception): g.embed(['remember'])
            self.assertEqual(len(self.requests), 1)
            self.assertIsNotNone(g.budget.failed)
            self.assert_accounting(g)

    def test_failure_latch_is_shared_between_llm_and_embedding(self):
        for initial in ('llm', 'embedding'):
            with self.subTest(initial=initial):
                self.requests.clear(); self.steps = [503]
                with self.gateway() as g:
                    with self.assertRaises(Exception): complete(g) if initial == 'llm' else g.embed(['test'])
                    with self.assertRaises(Exception): g.embed(['test']) if initial == 'llm' else complete(g)
                    self.assertEqual(len(self.requests), 3)

    def test_fresh_gateway_can_recover_after_exhaustion(self):
        self.steps = [503, 503, 503, 200]
        with self.gateway() as g:
            with self.assertRaises(Exception): complete(g)
            self.assertIsNotNone(g.budget.failed)
        with self.gateway() as g:
            self.assertEqual(complete(g).text, 'recovered')
            self.assertIsNone(g.budget.failed)
        self.assertEqual(len(self.requests), 4)

    def test_retry_after_seconds_is_honored(self):
        self.steps = [{'status': 429, 'headers': {'Retry-After': '7'}}, 200]
        with self.gateway() as g:
            complete(g)
            self.assertEqual(self.sleeps, [7])
            self.assertEqual(g.budget.rows[0]['retry_delay_source'], 'retry_after')

    def test_retry_after_http_date_is_honored(self):
        now = 1710000000
        date = format_datetime(datetime.fromtimestamp(now + 7, timezone.utc), usegmt=True)
        self.steps = [{'status': 503, 'headers': {'Retry-After': date}}, 200]
        with patch.object(retry.time, 'time', return_value=now), self.gateway() as g:
            complete(g)
            self.assertEqual(self.sleeps, [7])

    def test_invalid_retry_after_uses_exponential_backoff(self):
        for value in ('not-a-date', '-1', 'nan', 'inf'):
            with self.subTest(value=value):
                self.requests.clear(); self.sleeps.clear()
                self.steps = [{'status': 503, 'headers': {'Retry-After': value}}] * 3 + [200]
                with patch.object(retry.random, 'random', return_value=.5), self.gateway(max_retries=3) as g:
                    complete(g)
                    self.assertEqual(self.sleeps, [1, 2, 4])

    def test_long_retry_after_stops_instead_of_retrying_early(self):
        self.steps = [{'status': 503, 'headers': {'Retry-After': '3600'}}]
        with self.gateway() as g:
            with self.assertRaises(openai.InternalServerError): complete(g)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(self.sleeps, [])
            self.assertIsNotNone(g.budget.failed)
            self.assertEqual(g.budget.rows[0]['retry_delay_source'], 'retry_after_exceeds_limit')

    def test_provider_retry_opt_out_is_honored(self):
        self.steps = [{'status': 503, 'headers': {'x-should-retry': 'false'}}]
        with self.gateway() as g:
            with self.assertRaises(openai.InternalServerError): complete(g)
            self.assertEqual(len(self.requests), 1)

    def test_diagnostics_redacted_on_every_failure(self):
        self.steps = [{'status': 503, 'message': 'FAKE_LLM_SECRET FAKE_EMBED_SECRET'}]
        with self.gateway() as g:
            with self.assertRaises(Exception): complete(g)
            text = g.output.read_text()
            self.assertNotIn('FAKE_LLM_SECRET', text)
            self.assertNotIn('FAKE_EMBED_SECRET', text)
            self.assertIn('[REDACTED]', text)
            self.assertEqual([r['http_status'] for r in g.budget.rows], [503, 503, 503])

    def test_failed_http_with_usage_is_counted_once(self):
        self.steps = [{'status': 503, 'usage': {'prompt_tokens': 7, 'completion_tokens': 0}}, 200]
        with self.gateway() as g:
            complete(g)
            self.assertEqual([r['usage_status'] for r in g.budget.rows], ['known', 'known'])
            self.assertAlmostEqual(g.budget.reserved, g.budget.cost('llm', 7, 0) + g.budget.cost('llm', 1, 1), places=12)
            self.assert_accounting(g)

    def test_non_json_temporary_error_can_recover(self):
        self.steps = [{'status': 503, 'invalid_json': True}, 200]
        with self.gateway() as g:
            complete(g)
            self.assertEqual(len(self.requests), 2)
            self.assertIsNone(g.budget.failed)

    def test_policy_is_frozen_in_provider_identity(self):
        identity = public_identity(self.profile, self.runtime)
        self.assertEqual(identity['request_retry_policy']['max_attempts'], 3)
        for profile, runtime in (({**self.profile, 'AMI_ALLOW_RETRIES': '0'}, self.runtime),
                                 (self.profile, {**self.runtime, 'max_retries': 3})):
            with patch.object(model_client, 'read_profile', return_value=profile), self.assertRaisesRegex(ValueError, 'identity changed'):
                model_client.Gateway(runtime, Path(self.temp.name) / 'changed.json', expected_identity=identity)
        self.assertEqual(self.requests, [])

    def test_retry_settings_are_read_from_real_fixture_files(self):
        env_file = Path(self.temp.name) / 'fake.env'
        env_file.write_text('\n'.join(f'{key}={value}' for key, value in self.profile.items()) + '\n')
        runtime_file = Path(self.temp.name) / 'runtime.json'
        runtime_file.write_text(json.dumps({**self.runtime, 'max_retries': 10, 'env_file': str(env_file)}))
        with patch.object(model_client, 'make_transport', side_effect=self.factory):
            g = model_client.Gateway(read_json(runtime_file), Path(self.temp.name) / 'from-files.json')
        self.current = g
        try:
            with patch.object(retry, 'sleep'):
                self.assertEqual(complete(g).text, 'recovered')
            self.assertEqual(len(self.requests), 2)
            self.assertEqual(g.budget.retry_policy.public()['max_attempts'], 11)
            self.assertEqual(g.client.max_retries, 0)
            self.assert_accounting(g)
        finally:
            g.close()

    def test_endpoint_model_and_sdk_retry_header_guards_stay_active(self):
        endpoint = self.profile['OPENAI_BASE_URL'] + '/chat/completions'
        for url, model, headers in ((endpoint + '/other', 'gpt-4o-mini', {}),
                                     (endpoint, 'wrong-model', {}),
                                     (endpoint, 'gpt-4o-mini', {'x-stainless-retry-count': '1'})):
            with self.subTest(url=url, model=model, headers=headers), self.gateway() as g:
                request = httpx.Request('POST', url, headers=headers, json={'model': model,
                    'messages': [{'role': 'user', 'content': 'test'}], 'max_tokens': 16})
                with self.assertRaises(RuntimeError):
                    g.transports['llm'].handle_request(request)
                self.assertIsNotNone(g.budget.failed)
                self.assertEqual(self.requests, [])
                self.assertEqual(g.budget.rows, [])

    def test_pacing_observes_every_attempt_and_overload(self):
        current, starts, original = [100.0], [], self.respond
        def respond(request):
            starts.append(current[0])
            return original(request)
        self.respond = respond
        def advance(seconds): current[0] += seconds
        def pacer(transport, policy): return paced_transport(transport, policy, clock=lambda: current[0], sleeper=advance)
        policy = {'state_directory': str(Path(self.temp.name) / 'pacing'), 'minimum_interval_seconds': 2,
                  'overload_cooldown_seconds': 10, 'maximum_cooldown_seconds': 20}
        with patch('memory_benchmark.runtime.pacing.paced_transport', side_effect=pacer), self.gateway(provider_request_pacing=policy) as g:
            complete(g)
            self.assertEqual(starts, [100, 110])
            state = read_json(Path(policy['state_directory']) / 'state.json')
            self.assertEqual(state['requests'], 2)
            self.assertEqual(state['overload_events'], 1)
            self.assertEqual(state['consecutive_overload'], 0)
            self.assert_accounting(g)

    def test_native_embedding_pacing_observes_each_retry(self):
        from memory_benchmark.runtime.native_bridge.common import pace_method_embeddings
        self.steps = [429, 200]
        policy = {'state_directory': str(Path(self.temp.name) / 'native-pacing'),
                  'minimum_interval_seconds': 0, 'cooldown_seconds': 0}
        with self.gateway() as g:
            pace_method_embeddings(g, policy)
            g.embed(['remember'])
            state = read_json(Path(policy['state_directory']) / 'state.json')
            self.assertEqual(state['requests'], 2)
            self.assertEqual(state['rate_limit_events'], 1)
            self.assertIsNone(g.budget.failed)
            self.assert_accounting(g)


class LoopbackTests(unittest.TestCase):
    def test_five_concurrent_gateways_send_ten_real_http_requests(self):
        requests, active, peak, lock = {}, [0], [0], threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                payload = json.loads(body)
                with lock:
                    rows = requests.setdefault(payload['seed'], [])
                    rows.append((body, self.headers.get('x-stainless-retry-count', '0')))
                    status = 503 if len(rows) == 1 else 200
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                try:
                    time.sleep(.02)
                    response = success(payload) if status == 200 else {'error': {'code': 'rate_limit_exceeded'}}
                    data = json.dumps(response).encode()
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    with lock: active[0] -= 1
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        gateways = []
        try:
            with tempfile.TemporaryDirectory() as folder:
                base = f'http://127.0.0.1:{server.server_port}/v1'
                profile = {'OPENAI_BASE_URL': base, 'AMI_EMBED_BASE_URL': base,
                           'OPENAI_API_KEY': 'FAKE_LOOPBACK', 'AMI_EMBED_API_KEY': 'FAKE_LOOPBACK', 'AMI_ALLOW_RETRIES': '1'}
                runtime = {**read_json(ROOT / 'configs/runtime.json'), 'max_retries': 2,
                           'request_timeout_seconds': 3, 'persist_answer_responses': False}
                with patch.object(model_client, 'read_profile', return_value=profile), \
                        patch.object(model_client, 'validate_profile', side_effect=lambda p, m: p):
                    for seed in range(17, 22):
                        g = model_client.Gateway({**runtime, 'seed': seed}, Path(folder) / f'{seed}.json')
                        for transport in g.transports.values():
                            transport.client.close()
                            transport.client = httpx.Client(timeout=3, trust_env=False)
                        gateways.append(g)
                barrier = threading.Barrier(5)
                def invoke(g):
                    barrier.wait(timeout=10)
                    return complete(g).text
                with patch.object(retry, 'sleep'), ThreadPoolExecutor(max_workers=5) as pool:
                    self.assertEqual(list(pool.map(invoke, gateways)), ['recovered'] * 5)
                self.assertEqual(sum(map(len, requests.values())), 10)
                self.assertEqual(sorted(requests), list(range(17, 22)))
                self.assertGreaterEqual(peak[0], 2)
                self.assertLessEqual(peak[0], 5)
                for g in gateways:
                    rows = requests[g.runtime['seed']]
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(rows[0][0], rows[1][0])
                    self.assertEqual([r[1] for r in rows], ['0', '0'])
                    self.assertIsNone(g.budget.failed)
                    self.assertEqual(len(g.budget.rows), 2)
                    self.assertEqual(g.client.max_retries, 0)
                    g.close()
                gateways.clear()
                destination = os.environ.get('RETRY_AUDIT_LOOPBACK_EVIDENCE')
                if destination:
                    Path(destination).write_text(json.dumps({'sdk': openai.__version__, 'wire_requests': 10,
                        'gateways': 5, 'successful_gateways': 5, 'requests_per_seed': {str(k): len(v) for k, v in requests.items()},
                        'observed_peak_active_http': peak[0], 'network': '127.0.0.1 only'}, indent=2) + '\n')
        finally:
            for g in gateways: g.close()
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
