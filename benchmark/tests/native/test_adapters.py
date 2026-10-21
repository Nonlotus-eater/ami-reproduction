"""Method-side compatibility checks; no model calls or benchmark edits."""
import hashlib
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from memory_benchmark.runtime.native_bridge.common import adapter_class
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows
from memory_benchmark.methods import create
from memory_benchmark.methods.base import MemoryAdapter
from memory_benchmark.schemas import History, Session, Message, Query


class FakeGateway:
    def __init__(self, *args, **kwargs):
        self.identity = {'model': 'text-embedding-v4', 'dimensions': 1024}
        self.profile = {'OPENAI_API_KEY': 'offline-test-key', 'OPENAI_BASE_URL': 'https://example.invalid/v1'}
        self.runtime = {'model': 'gpt-4o-mini', 'request_timeout_seconds': 1}
        self.budget = SimpleNamespace(rows=[], failed=None, check=lambda: None)
        self.replies = iter([
            'Thought: Find Alice.\nAction: search_chatrecord\nAction Input: {"keywords":["Kyoto"]}',
            'Thought: Save evidence.\nAction: take_note\nAction Input: {"indices":[1]}',
            'Thought: Enough evidence.\nAction: finish_search\nAction Input: {}'])

    def embed(self, texts):
        values = []
        for text in texts:
            vector = [0.0] * 1024
            for word in text.lower().split():
                vector[int.from_bytes(hashlib.sha256(word.encode()).digest()[:2]) % 1024] += 1
            scale = sum(x*x for x in vector) ** 0.5
            values.append([x / scale for x in vector])
        return values

    def complete(self, messages, **kwargs):
        assert kwargs['max_tokens'] == 900 and kwargs['stage'] == 'memory_retrieval_planner'
        self.budget.rows.append({'input_tokens': 20, 'output_tokens': 10})
        return SimpleNamespace(text=next(self.replies))

    def close(self):
        pass


class AdapterTests(unittest.TestCase):
    def test_native_api_preserves_system_roles_dates_and_user_isolation(self):
        self.check_native_search(100)

    def test_unbounded_search_preserves_native_agent_dates_and_scope(self):
        self.check_native_search(None)

    def test_frozen_adapter_copy_preserves_native_api(self):
        self.check_native_search(100, frozen=True)

    def test_frozen_worker_runs_outside_repository_with_native_environment(self):
        import memory_benchmark
        root, method = Path(sys.argv[1]).resolve(), sys.argv[2]
        history = History('locomo_refined', 'worker', ('Alice',), (
            Session('s1', None, '', (Message('m1', 'user', 'Alice', 'Alice lives in Kyoto.', None),)),))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            framework = folder / 'frozen_framework/src'
            package = framework / 'memory_benchmark'
            shutil.copytree(Path(memory_benchmark.__file__).parent, package,
                            ignore=shutil.ignore_patterns('__pycache__'))
            bridge = folder / 'frozen_method_bridge'
            shutil.copytree(package / 'runtime/native_bridge', bridge)
            adapter_file = bridge / ('adapter_' + method + '.py')
            shutil.copyfile(package / 'methods' / (method + '.py'), adapter_file)
            write_json(folder / 'history.json', history.to_wire())
            write_rows(folder / 'queries.jsonl', [Query('locomo_refined', 'worker', 'q1', 'Where does Alice live?').to_wire()])
            job = {'method': method, 'method_config': {'source_root': str(root), 'adapter_file': str(adapter_file)},
                'runtime': {'top_k': 100}, 'provider_identity': {}, 'scope': 'worker-scope',
                'history': str(folder / 'history.json'), 'queries': str(folder / 'queries.jsonl'),
                'output': str(folder / 'memory_attempt_001')}
            job['input_hashes'] = {key: file_hash(job[key]) for key in ('history', 'queries')}
            write_json(folder / 'job.json', job)
            # Replace only the model gateway. Execute the real frozen worker,
            # adapter, native store and retrieval from a different working directory.
            bootstrap = '''
import importlib.util
from pathlib import Path
import runpy
import socket
import sys
import memory_benchmark
assert Path(memory_benchmark.__file__).resolve().is_relative_to(Path(sys.argv[4]))
spec = importlib.util.spec_from_file_location('native_fixture', sys.argv[3])
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
def denied(*args, **kwargs):
    raise RuntimeError('Network forbidden in frozen worker test')
socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
import memory_benchmark.runtime.model_client as model_client
model_client.Gateway = fixture.FakeGateway
sys.argv = sys.argv[1:3]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
            result = subprocess.run([sys.executable, '-c', bootstrap, str(bridge / 'worker.py'),
                str(folder / 'job.json'), str(Path(__file__).resolve()), str(framework)],
                cwd=folder, env={**os.environ, 'PYTHONPATH': str(framework), 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, text=True, timeout=60)
            report = read_json(folder / 'memory_attempt_001/report.json')
            self.assertEqual(result.returncode, 0, (result.stderr, report))
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(report['questions'], 1)
            retrieval = json.loads((folder / 'memory_attempt_001/retrievals.jsonl').read_text())
            self.assertEqual(retrieval['id'], 'q1')
            self.assertIn('Kyoto', '\n'.join(item['text'] for item in retrieval['evidence']))

    def make_adapter(self, root, method, history, output, scope, gateway, *, frozen=False):
        config = {'source_root': str(root)}
        if frozen:
            from importlib import import_module
            module = import_module('memory_benchmark.methods.' + method)
            path = output / ('adapter_' + method + '.py')
            shutil.copyfile(module.__file__, path)
            result = adapter_class(path, method)(config, history, output, scope, gateway)
        else:
            result = create(method, config, history, output, scope, gateway)
        self.assertIsInstance(result, MemoryAdapter)
        return result

    def check_native_search(self, top_k, *, frozen=False):
        root, method = Path(sys.argv[1]), sys.argv[2]
        gateway = FakeGateway()
        history = History('locomo_refined', 'contract', ('Alice', 'Bob'), (
            Session('s1', None, '', (Message('m1', 'system', 'Alice', 'Alice prefers green tea.', None),)),
            Session('s2', '2023-05-01T12:00:00+00:00', '1 May 2023', (
                Message('m2', 'user', 'Alice', 'Alice lives in Kyoto.', '2023-05-01T12:00:00+00:00'),
                Message('m3', 'assistant', 'Bob', 'Bob enjoys hiking near Kyoto.', '2023-05-01T12:00:00+00:00')))))
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(root, method, history, Path(tmp), 'scope-a', gateway, frozen=frozen)
            try:
                for session in history.sessions:
                    adapter.add(session)
                adapter.finalize()
                foreign = {'request_id': 'other-message', 'user_id': 'scope-b', 'session_id': 'foreign',
                    'messages': [{'role': 'user', 'content': 'Alice SECRET_OTHER_USER lives in Rome.', 'timestamp': None}]}
                self.assertEqual(adapter.client.post('/add', json=foreign).status_code, 200)
                import sqlite3
                with sqlite3.connect(Path(tmp) / 'memory.sqlite3') as connection:
                    if method == 'invmem':
                        stored = connection.execute("SELECT role, source_timestamp FROM memories WHERE user_id='scope-a'").fetchall()
                    else:
                        stored = connection.execute("SELECT m.role, m.timestamp_ms FROM messages m JOIN ingestions i ON m.ingestion_sequence=i.sequence WHERE i.user_id='scope-a'").fetchall()
                self.assertIn(('system', None), stored)
                self.assertTrue(any(role == 'user' and stamp == 1682942400000 for role, stamp in stored))
                query = Query('locomo_refined', 'contract', 'q1', 'Where does Alice live?')
                results = adapter.search(query, top_k)
                self.assertTrue(results)
                text = '\n'.join(r.text for r in results)
                self.assertNotIn('SECRET_OTHER_USER', text)
                self.assertIn('Kyoto', text)
                self.assertIn('2023-05-01', text)
                if method == 'invmem':
                    self.assertIn('Recording date unknown', text)
                else:
                    self.assertEqual(len(gateway.budget.rows), 3)
                self.assertTrue(all(r.timestamp is None for r in results))
            finally:
                adapter.close()

    def test_unbounded_invmem_returns_more_than_the_http_ceiling(self):
        root, method = Path(sys.argv[1]), sys.argv[2]
        if method != 'invmem':
            self.skipTest('ReFind retains its native agent-selected notes')
        history = History('locomo_refined', 'large', ('Alice',), (
            Session('large-session', None, '', tuple(Message(str(i), 'user', 'Alice',
                f'Alice Kyoto fact number {i}.', None) for i in range(125))),))
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(root, method, history, Path(tmp), 'scope-large', FakeGateway())
            try:
                adapter.add(history.sessions[0])
                result = adapter.search(Query('locomo_refined', 'large', 'q', 'Kyoto facts'), None)
                self.assertEqual(len(result), 125)
                self.assertEqual(len({r.id for r in result}), 125)
            finally:
                adapter.close()


if __name__ == '__main__':
    import socket
    def denied(*args, **kwargs):
        raise RuntimeError('Network forbidden in adapter tests')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
    unittest.main(argv=[sys.argv[0]])
