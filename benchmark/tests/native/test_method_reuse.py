"""Four-method storage/protocol reuse with deterministic model boundaries.

Run separately in each native environment: this deliberately prevents the
upstreams' different `app` packages from sharing Python module globals.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memory_benchmark.methods import create
from memory_benchmark.methods.base import MemoryAdapter
from memory_benchmark.pipeline.ingestion import POLICY, plan
from memory_benchmark.runtime.native_bridge.common import adapter_class
from memory_benchmark.schemas import History, Message, Query, Session


class FixtureGateway:
    embedding_dimensions = 1024
    embedding_batch = 10
    identity = {'model': 'offline-fixture', 'embedding_dimensions': 1024}
    profile = {'OPENAI_API_KEY': 'offline-test-key', 'OPENAI_BASE_URL': 'https://example.invalid/v1',
               'AMI_EMBED_API_KEY': 'offline-embedding-key', 'AMI_EMBED_BASE_URL': 'https://example.invalid/v1'}
    runtime = {'model': 'gpt-4o-mini', 'request_timeout_seconds': 1}

    def __init__(self, *args, **kwargs):
        self.budget = SimpleNamespace(rows=[], failed=None, check=lambda: None)
        self.replies = iter(['Thought: Find Alice.\nAction: search_chatrecord\nAction Input: {"keywords":["Kyoto"]}',
                             'Thought: Keep evidence.\nAction: take_note\nAction Input: {"indices":[1]}',
                             'Thought: Done.\nAction: finish_search\nAction Input: {}'])

    def embed(self, texts):
        result = []
        for text in texts:
            vector = [0.0] * self.embedding_dimensions
            for word in text.lower().split():
                vector[int.from_bytes(hashlib.sha256(word.encode()).digest()[:2]) % len(vector)] += 1
            scale = sum(v*v for v in vector) ** 0.5
            result.append([v / scale for v in vector])
        return result

    def complete(self, *args, **kwargs):
        self.budget.rows.append({'input_tokens': 1, 'output_tokens': 1})
        return SimpleNamespace(text=next(self.replies))


def model_boundaries(method, adapter, stack):
    if method == 'activememoryindex':
        from app import llm
        stack.enter_context(patch.object(llm, 'extract_facts', side_effect=lambda text: [text]))
        stack.enter_context(patch.object(llm, 'recall_question', side_effect=lambda query, options: query))
        for name in ('classify_update_intent', 'extract_updates', 'verify_replaced_v2',
                     'detect_updates', 'verify_replaced'):
            stack.enter_context(patch.object(llm, name, return_value=[]))
        stack.enter_context(patch.object(llm, 'classify_question', return_value={}))
        stack.enter_context(patch.object(llm, 'reflect_gap', return_value=None))
    elif method == 'mem0':
        def extraction(*args, **kwargs):
            text = str(kwargs.get('messages', args))
            fact = ('Alice SECRET_OTHER_USER lives in Rome.' if 'SECRET_OTHER_USER' in text
                    else 'Alice lives in Kyoto.')
            return json.dumps({'memory': [{'id': '0', 'text': fact, 'attributed_to': 'user'}]})
        stack.enter_context(patch.object(adapter.memory.llm, 'generate_response', side_effect=extraction))


class MethodReuseTests(unittest.TestCase):
    def check(self, *, frozen=False):
        source, method = Path(sys.argv[1]), sys.argv[2]
        history = History('locomo_refined', 'reuse', ('Alice',), (
            Session('source-session', None, '', tuple(Message(str(i), 'system' if i == 0 else 'user',
                'Alice', f'Alice lives in Kyoto. Message {i}.', None) for i in range(41))),))
        chunks, _ = plan(history, POLICY)
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            output = Path(temp)
            cache = os.environ.get('MEMORY_BENCHMARK_FASTEMBED_CACHE',
                                   str(source.resolve().parents[1] / 'models/fastembed'))
            if method == 'mem0':
                self.assertTrue(Path(cache).is_dir(), 'Prefetch Qdrant/bm25 or set MEMORY_BENCHMARK_FASTEMBED_CACHE')
            config = {'source_root': str(source), 'embedding_backend': 'shared_api',
                      'extraction_max_tokens': 2048, 'fastembed_cache': cache}
            if frozen:
                from importlib import import_module, util
                module = import_module('memory_benchmark.methods.' + method)
                path = output / f'adapter_{method}.py'
                path.write_text(Path(module.__file__).read_text())
                if method in {'invmem', 'refind'}:
                    factory = adapter_class(path, method)
                else:
                    # AMI/Mem0 are imported within the frozen framework package;
                    # their relative imports are not standalone bridge adapters.
                    spec = util.spec_from_file_location('memory_benchmark.methods.frozen_' + method, path)
                    copy = util.module_from_spec(spec)
                    spec.loader.exec_module(copy)
                    factory = copy.Adapter
            else:
                factory = lambda *args: create(method, *args)
            adapter = factory(config, history, output, 'scope-a', FixtureGateway())
            stack.callback(adapter.close)
            model_boundaries(method, adapter, stack)
            self.assertIsInstance(adapter, MemoryAdapter)
            for chunk in chunks:
                adapter.add(chunk)
            adapter.finalize()
            if method == 'mem0':
                self.assertNotIn(adapter.memory.vector_store._bm25_encoder, (None, False))
                adapter.memory.add([{'role': 'user', 'content': 'Alice SECRET_OTHER_USER lives in Rome.'}],
                                   user_id='scope-other')
                rows = [json.loads(line) for line in (output / 'native_add.jsonl').read_text().splitlines()]
                self.assertEqual(len(rows), 3)
                self.assertEqual({row['session_id'] for row in rows}, {'source-session'})
            else:
                client = adapter.service if method == 'activememoryindex' else adapter.client
                response = client.post('/add', json={'request_id': 'foreign', 'user_id': 'scope-other',
                    'session_id': 'foreign', 'messages': [{'role': 'user',
                        'content': 'Alice SECRET_OTHER_USER lives in Rome.', 'timestamp': None}]})
                self.assertEqual(response.status_code, 200)
                import sqlite3
                with sqlite3.connect(output / 'memory.sqlite3') as connection:
                    if method == 'invmem':
                        count = connection.execute("select count(*) from memories where user_id='scope-a'").fetchone()[0]
                        self.assertEqual(count, 41)
                    elif method == 'refind':
                        count = connection.execute("select count(*) from messages m join ingestions i on m.ingestion_sequence=i.sequence where i.user_id='scope-a'").fetchone()[0]
                        self.assertEqual(count, 41)
            result = adapter.search(Query('locomo_refined', 'reuse', 'q', 'Where does Alice live?'), None)
            text = '\n'.join(e.text for e in result)
            self.assertIn('Kyoto', text)
            self.assertNotIn('SECRET_OTHER_USER', text)

    def test_registered_method_accepts_distinct_chunks_in_one_session(self):
        self.check()

    def test_frozen_adapter_reuses_the_same_native_contract(self):
        self.check(frozen=True)


if __name__ == '__main__':
    import faulthandler
    if os.environ.get('MEMORY_BENCHMARK_TEST_TRACE'):
        faulthandler.dump_traceback_later(15, repeat=False)
    def denied(*args, **kwargs):
        raise RuntimeError('Network forbidden in native reuse test')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
    os.environ.update(MEM0_TELEMETRY='false', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    unittest.main(argv=[sys.argv[0], *sys.argv[3:]])
