"""Run in each native environment; model boundaries are offline fixtures.

Crash AFTER the second real native Add but BEFORE its checkpoint. A fresh
process must restore chunk 1 and write chunks 2/3 once, including Mem0's two
stores and message history. No provider requests or experiment data are used.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows
from memory_benchmark.runtime.memory_checkpoint import POLICY
from memory_benchmark.schemas import History, Message, Query, Session


def child(path, failure):
    from test_method_reuse import FixtureGateway, model_boundaries
    from memory_benchmark.methods import create
    import memory_benchmark.runtime.worker as worker
    import memory_benchmark.runtime.native_bridge.worker as bridge
    class Gateway(FixtureGateway):
        def install_native(self):
            pass
        def close(self):
            pass
    job = read_json(path)
    method = job['method']
    def blocked(*args, **kwargs):
        raise RuntimeError('Network forbidden in native checkpoint test')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = blocked
    with ExitStack() as stack:
        def factory(*args, **kwargs):
            adapter = create(method, *args, **kwargs)
            model_boundaries(method, adapter, stack)
            if method == 'mem0':
                points, _ = adapter.memory.vector_store.client.scroll(
                    collection_name='benchmark', limit=100, with_vectors=False)
                write_json(Path(job['output']) / 'test_initial_vector_ids.json', sorted(str(p.id) for p in points))
            native_add = adapter.add
            calls = []
            write_json(Path(job['output']) / 'test_add_calls.json', calls)
            def add(chunk):
                calls.append(chunk.chunk_id)
                write_json(Path(job['output']) / 'test_add_calls.json', calls)
                if method == 'mem0':
                    fact = f'Alice lives in Kyoto. Fact for chunk {chunk.chunk_id}.'
                    with patch.object(adapter.memory.llm, 'generate_response', return_value=json.dumps({
                            'memory': [{'id': '0', 'text': fact, 'attributed_to': 'user'}]})):
                        native_add(chunk)
                    points, _ = adapter.memory.vector_store.client.scroll(
                        collection_name='benchmark', limit=100, with_vectors=False)
                    write_json(Path(job['output']) / 'test_vector_ids.json', sorted(str(p.id) for p in points))
                else:
                    native_add(chunk)
                if failure == 'add' and len(calls) == 2:
                    os._exit(73)
            adapter.add = add
            if failure == 'search':
                native_search = adapter.search
                def search(*args, **kwargs):
                    native_search(*args, **kwargs)
                    os._exit(74)
                adapter.search = search
            return adapter
        stack.enter_context(patch.object(worker, 'Gateway', Gateway))
        stack.enter_context(patch.object(bridge, 'Gateway', Gateway))
        if method in {'invmem', 'refind'}:
            stack.enter_context(patch.object(bridge, 'adapter_class', return_value=factory))
            return bridge.main(path)
        stack.enter_context(patch.object(worker, 'create', side_effect=lambda name, *args, **kwargs: factory(*args, **kwargs)))
        return 0 if worker.execute(job)['status'] == 'complete' else 1


def check(source, method):
    source = Path(source).resolve()
    cache = source.parents[1] / 'models/fastembed'
    config = {'source_root': str(source), 'embedding_backend': 'shared_api',
              'extraction_max_tokens': 2048, 'fastembed_cache': str(cache)}
    config['adapter_file'] = str(Path(__file__).resolve().parents[2] /
                                'src/memory_benchmark/methods' / f'{method}.py')
    history = History('locomo_refined', 'resume', ('Alice',), (
        Session('one-source-session', None, '', tuple(Message(str(i), 'user',
            'Alice', f'Alice lives in Kyoto. Message {i}.', None) for i in range(41))),))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        write_json(root / 'history.json', history.to_wire())
        write_rows(root / 'queries.jsonl', [Query('locomo_refined', 'resume', 'q', 'Where does Alice live?').to_wire()])
        base = {'method': method, 'method_config': config, 'offline': False, 'provider_identity': {},
                'runtime': {'memory_checkpoint_policy': POLICY,
                            'add_chunk_policy': 'aml_20_messages_2000_words_v1', 'top_k': None},
                'history': str(root / 'history.json'), 'queries': str(root / 'queries.jsonl'),
                'input_hashes': {k: file_hash(root / v) for k, v in
                                  [('history', 'history.json'), ('queries', 'queries.jsonl')]}}
        for attempt, failure, expected_exit in ((1, 'add', 73), (2, 'none', 0),
                                                (3, 'search', 74), (4, 'none', 0)):
            output = root / f'memory_attempt_{attempt:03d}'
            output.mkdir()
            path = output / 'job.json'
            write_json(path, {**base, 'output': str(output), 'scope': f'run:{method}:history:{output.name}'})
            result = subprocess.run([sys.executable, __file__, '--child', str(path), failure],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
            if result.returncode != expected_exit:
                print(result.stdout[-12000:])
                if (output / 'report.json').exists():
                    print(read_json(output / 'report.json'))
                raise AssertionError(f'{method}: attempt {attempt} exit {result.returncode}; expected {expected_exit}')
        first_resume = root / 'memory_attempt_002'
        report = read_json(first_resume / 'report.json')
        assert report['resumed_add_chunks'] == 1, report
        assert report['memory_checkpoint']['completed_add_chunks'] == 3, report
        assert len(read_json(first_resume / 'test_add_calls.json')) == 2
        report = read_json(output / 'report.json')
        assert report['resumed_add_chunks'] == 3 and report['memory_checkpoint']['finalized'], report
        assert read_json(output / 'test_add_calls.json') == [], report
        state = output / 'memory_state'
        if method == 'mem0':
            path, query = state / 'history.db', 'SELECT COUNT(*) FROM messages'
        elif method == 'activememoryindex':
            path, query = state / 'memory.sqlite3', "SELECT COUNT(*) FROM items WHERE kind='raw'"
        elif method == 'invmem':
            path, query = state / 'memory.sqlite3', 'SELECT COUNT(*) FROM memories'
        else:
            path, query = state / 'memory.sqlite3', 'SELECT COUNT(*) FROM messages'
        with sqlite3.connect(path) as connection:
            count = connection.execute(query).fetchone()[0]
            if method == 'mem0':
                # Native Mem0 keeps only the latest ten source messages.
                assert count == 10, count
                assert any('Message 40.' in row[0] for row in connection.execute('SELECT content FROM messages'))
                history_ids = {row[0] for row in connection.execute('SELECT memory_id FROM history')}
                final_ids = set(read_json(first_resume / 'test_vector_ids.json'))
                assert final_ids == set(read_json(output / 'test_initial_vector_ids.json'))
                partial_ids = set(read_json(root / 'memory_attempt_001/test_vector_ids.json'))
                snapshot = read_json(root / 'memory_resume/checkpoint.json')
                assert len(final_ids) == 3 and final_ids == history_ids, (final_ids, history_ids)
                assert len(partial_ids & final_ids) == 1, (partial_ids, final_ids)
                assert any('storage.sqlite' in name for name in snapshot['files']), snapshot
            else:
                assert count == 41, (method, count)
        assert 'Kyoto' in (output / 'retrievals.jsonl').read_text()
        print({'method': method, 'status': 'passed', 'resumed_add_chunks': 1,
               'remaining_add_calls': 2, 'add_calls_after_search_failure': 0,
               'stored_message_rows': count, 'provider_requests': 0})


if __name__ == '__main__':
    os.environ.update(MEM0_TELEMETRY='false', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', TOKENIZERS_PARALLELISM='false')
    if sys.argv[1] == '--child':
        raise SystemExit(child(sys.argv[2], sys.argv[3]))
    check(sys.argv[1], sys.argv[2])
