"""Regression checks for queue."""
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows
from memory_benchmark.runtime.model_client import Reply
from memory_benchmark.runtime.queue import execute_task, publish
from memory_benchmark.schemas import History, Message, Query, Session, Target

class QueueTests(unittest.TestCase):

    def test_resume_reuses_memory_answer_and_score_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepared, output = (root / 'prepared', root / 'run')
            prepared.mkdir()
            output.mkdir()
            h = History('personamem_v1', 'e', ('User', 'Assistant'), (Session('s', None, '', (Message('m', 'user', 'User', 'history', None),)),))
            q = Query('personamem_v1', 'e', 'q', 'Question', all_options='(a) A (b) B')
            write_json(prepared / 'history.json', h.to_wire())
            write_rows(prepared / 'queries.jsonl', [q.to_wire()])
            write_rows(prepared / 'targets.jsonl', [Target('q', '(b)', ('E1',)).to_wire()])
            case = {'benchmark': 'personamem_v1', 'episode_id': 'e', 'key': 'k', 'history': 'history.json', 'queries': 'queries.jsonl', 'targets': 'targets.jsonl'}
            runtime = {'model': 'gpt-4o-mini', 'worker_timeout_seconds': 60, 'reader_tokenizer': 'fixture', 'top_k': 100, 'reader_token_budget': 4096, 'answer_input_token_limit': 12000, 'answer_max_tokens': 256, 'answer_temperature': 0}
            manifest = {'suite_id': 's', 'scope': 'expanded', 'aggregation': 'macro_benchmark', 'runtime': runtime, 'execution': {'provider_identity': {}}, 'methods': {'mem0': {'config': {'python': 'unused'}}}, 'benchmarks': {'personamem_v1': {'primary_metric': 'mcq_accuracy', 'items': {'q': {'capability_leaves': ['E1'], 'cluster_id': 'p'}}}}, 'inputs': {field: file_hash(prepared / field) for field in ['history.json', 'queries.jsonl']}}
            calls = []

            def worker(job, **kwargs):
                self.assertNotIn('targets', job)
                calls.append('worker')
                stage = Path(job['output'])
                write_json(stage / 'report.json', {'status': 'complete'})
                write_rows(stage / 'retrievals.jsonl', [{'id': 'q', 'evidence': []}])
                return SimpleNamespace(returncode=0)

            class Client:

                def __init__(self, *args, **kwargs):
                    pass

                def complete(self, prompt, **kwargs):
                    calls.append('answer')
                    self_test.assertFalse((output / 'mem0/personamem_v1/k/native_scores.jsonl').exists())
                    return Reply('<final_answer>(b)</final_answer>', 'gpt-4o-mini')

                def close(self):
                    pass
            from memory_benchmark.pipeline.context import FixtureTokenizer

            class Tokenizer(FixtureTokenizer):
                name = 'fixture'

                def __init__(self, *args):
                    pass
            self_test = self
            with patch('memory_benchmark.runtime.queue.run_memory_worker', side_effect=worker), patch('memory_benchmark.runtime.queue.Gateway', Client), patch('memory_benchmark.runtime.queue.GPTTokenizer', Tokenizer):
                first = execute_task(prepared, output, manifest, 'mem0', case)
                self.assertEqual(first['status'], 'complete', first)
                second = execute_task(prepared, output, manifest, 'mem0', case)
                self.assertEqual(second['status'], 'complete', second)
                self.assertEqual(calls, ['worker', 'answer'])
                state = publish(output, manifest, {'job': first})
                self.assertEqual(state['status'], 'complete')
                path = output / 'mem0/personamem_v1/k/answers.jsonl'
                path.write_text(path.read_text() + ' ')
                damaged = execute_task(prepared, output, manifest, 'mem0', case)
                self.assertEqual(damaged['status'], 'failed')
                self.assertEqual(calls, ['worker', 'answer'])
if __name__ == '__main__':
    unittest.main()
