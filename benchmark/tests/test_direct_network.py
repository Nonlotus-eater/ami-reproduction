PROXY_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
'Real loopback requests prove model transports ignore environment proxies.'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import httpx
from memory_benchmark.runtime.artifacts import file_hash, write_json, write_rows
from memory_benchmark.runtime.budget import CallBudget, make_transport

class DirectNetworkTests(unittest.TestCase):

    def test_answer_judge_and_embeddings_ignore_a_failing_environment_proxy(self):
        hits = {'model': 0, 'proxy': 0}

        class Model(BaseHTTPRequestHandler):

            def do_POST(self):
                hits['model'] += 1
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if payload['model'] == 'text-embedding-v4':
                    body = {'model': payload['model'], 'data': [{'index': 0, 'embedding': [1.0] + [0.0] * 63}], 'usage': {'prompt_tokens': 1, 'total_tokens': 1}}
                else:
                    body = {'model': payload['model'], 'choices': [{'message': {'content': 'fixture'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class Proxy(BaseHTTPRequestHandler):

            def do_POST(self):
                hits['proxy'] += 1
                self.send_error(502)
            do_CONNECT = do_POST

            def log_message(self, *args):
                pass
        model, proxy = (ThreadingHTTPServer(('127.0.0.1', 0), Model), ThreadingHTTPServer(('127.0.0.1', 0), Proxy))
        for server in (model, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {k: f'http://127.0.0.1:{proxy.server_port}' for k in PROXY_KEYS}
        env.update(NO_PROXY='', no_proxy='')
        profile = {'AMI_TEST_INPUT_CNY_PER_MILLION': '1', 'AMI_TEST_OUTPUT_CNY_PER_MILLION': '1', 'AMI_TEST_EMBED_CNY_PER_MILLION': '1'}
        try:
            with patch.dict(os.environ, env):
                for kind, phase in [('llm', 'answer'), ('llm', 'judge'), ('embedding', 'memory_add')]:
                    budget = CallBudget(profile)
                    budget.phase = phase
                    url = f'http://127.0.0.1:{model.server_port}/v1/' + ('embeddings' if kind == 'embedding' else 'chat/completions')
                    name = 'text-embedding-v4' if kind == 'embedding' else 'gpt-4o-mini'
                    guard = make_transport(budget, kind, url, name, disable_thinking_required=False, embedding_dimensions=64, timeout=2)
                    self.assertFalse(guard.client._trust_env)
                    payload = {'model': name, 'input': ['fixture'], 'dimensions': 64} if kind == 'embedding' else {'model': name, 'messages': [{'role': 'user', 'content': 'fixture'}], 'max_tokens': 8}
                    with httpx.Client(transport=guard, trust_env=False) as client:
                        result = client.post(url, json=payload)
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(budget.rows[0]['phase'], phase)
                    self.assertIsNone(budget.failed)
            self.assertEqual(hits, {'model': 3, 'proxy': 0})
        finally:
            for server in (model, proxy):
                server.shutdown()
                server.server_close()
if __name__ == '__main__':
    unittest.main()
