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
import shutil
import subprocess
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
    def test_real_adapter_runs_in_filesystem_sandbox_with_checkpoint_resume(self):
        from memory_benchmark.benchmarks.native_contract import ROOT
        from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows
        from memory_benchmark.runtime.native_freeze import freeze_methods
        from memory_benchmark.runtime.worker_launch import run_memory_worker
        source, method = Path(sys.argv[1]).resolve(), sys.argv[2]
        cache = os.environ.get('MEMORY_BENCHMARK_FASTEMBED_CACHE',
                               str(source.parents[1] / 'models/fastembed'))
        config = {'source_root': str(source), 'python': sys.executable, 'embedding_backend': 'shared_api',
                  'extraction_max_tokens': 2048, 'fastembed_cache': cache}
        if method != 'mem0':
            config.pop('fastembed_cache')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            framework = root / 'queue/frozen_framework'
            shutil.copytree(ROOT / 'src', framework / 'src', ignore=shutil.ignore_patterns('__pycache__'))
            history = History('locomo_refined', 'sandbox', ('Alice',), (
                Session('s', None, '', (Message('m', 'user', 'Alice', 'Alice lives in Kyoto.', None),)),))
            hp, qp = root / 'prepared/history.json', root / 'prepared/queries.jsonl'
            write_json(hp, history.to_wire())
            write_rows(qp, [Query('locomo_refined', 'sandbox', 'q', 'Where does Alice live?').to_wire()])
            profile = root / 'fixture.env'
            profile.write_text('OPENAI_API_KEY=offline-fixture-key\nAMI_ALLOW_RETRIES=0\n')
            runtime = {'env_file': str(profile), 'top_k': 100, 'add_chunk_policy': POLICY,
                       'memory_checkpoint_policy': 'add_chunks_sqlite_snapshot_v1'}
            record = freeze_methods({method: config}, root / 'queue', runtime)[method]
            bootstrap = root / 'bootstrap.py'
            fixture = root / 'native_fixture.py'
            fixture.write_text(Path(__file__).read_text())
            bootstrap.write_text('''import importlib.util,json,sys,socket
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('native_fixture',Path(__file__).with_name('native_fixture.py'))
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
job=json.loads(Path(sys.argv[1]).read_text())
bridge=job['method'] in ('invmem','refind')
if bridge:
 from memory_benchmark.runtime.native_bridge import worker
else:
 from memory_benchmark.runtime import worker
gateway=fixture.FixtureGateway()
gateway.install_native=lambda:None
gateway.close=lambda:None
worker.gateway_for_memory=lambda *a,**k:gateway
stack=ExitStack()
if not bridge:
 original=worker.create
 def create(*a,**k):
  adapter=original(*a,**k)
  fixture.model_boundaries(job['method'],adapter,stack)
  return adapter
 worker.create=create
def denied(*a,**k):raise RuntimeError('Network forbidden in sandbox integration test')
socket.socket.connect=socket.socket.connect_ex=socket.create_connection=denied
try:status=worker.main(sys.argv[1]) if bridge else (0 if worker.execute(job)['status']=='complete' else 1)
finally:stack.close()
raise SystemExit(status)
''')
            for attempt in (1, 2):
                output = root / f'queue/{method}/locomo_refined/h/memory_attempt_{attempt:03d}'
                output.mkdir(parents=True)
                job = {'method': method, 'method_config': config, 'runtime': runtime, 'offline': False,
                       'scope': f'sandbox:{method}:h:{output.name}', 'provider_identity': {},
                       'history': str(hp), 'queries': str(qp), 'output': str(output),
                       'input_hashes': {'history': file_hash(hp), 'queries': file_hash(qp)}}
                if method in ('invmem', 'refind'):
                    job['method_config'] = {**config, 'adapter_file': str(framework / 'src/memory_benchmark/methods' / (method + '.py'))}
                with (output / 'worker.log').open('w') as log:
                    child = run_memory_worker(job, executable=sys.executable, framework=framework,
                        queue_output=root / 'queue', record=record, timeout=45, log=log,
                        script=bootstrap, extra_readonly=[(bootstrap, bootstrap), (fixture, fixture)])
                self.assertTrue((output / 'report.json').exists(), (output / 'worker.log').read_text())
                report = read_json(output / 'report.json')
                self.assertEqual(child.returncode, 0, ((output / 'worker.log').read_text(), report))
                evidence = (output / 'retrievals.jsonl').read_text()
                self.assertIn('Kyoto', evidence)
                self.assertFalse((output / 'worker_inputs/provider.env').exists())
                self.assertEqual(report.get('resumed_add_chunks', 0), attempt - 1)

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
