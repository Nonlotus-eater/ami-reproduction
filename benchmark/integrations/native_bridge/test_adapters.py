"""Method-side compatibility checks; no model calls or benchmark edits."""
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from common import adapter_class
from memory_benchmark.schemas import History, Session, Message, Query


class FakeGateway:
    def __init__(self):
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


class AdapterTests(unittest.TestCase):
    def test_native_api_preserves_system_roles_dates_and_user_isolation(self):
        root, method = Path(sys.argv[1]), sys.argv[2]
        gateway = FakeGateway()
        history = History('locomo_refined', 'contract', ('Alice', 'Bob'), (
            Session('s1', None, '', (Message('m1', 'system', 'Alice', 'Alice prefers green tea.', None),)),
            Session('s2', '2023-05-01T12:00:00+00:00', '1 May 2023', (
                Message('m2', 'user', 'Alice', 'Alice lives in Kyoto.', '2023-05-01T12:00:00+00:00'),
                Message('m3', 'assistant', 'Bob', 'Bob enjoys hiking near Kyoto.', '2023-05-01T12:00:00+00:00')))))
        with tempfile.TemporaryDirectory() as tmp:
            adapter = adapter_class(Path(__file__).resolve().parents[1] / 'adapters' / f'{method}.py', method)({'source_root': str(root)}, history, Path(tmp), 'scope-a', gateway)
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
                results = adapter.search(query, 100)
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


if __name__ == '__main__':
    import socket
    def denied(*args, **kwargs):
        raise RuntimeError('Network forbidden in adapter tests')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
    unittest.main(argv=[sys.argv[0]])
