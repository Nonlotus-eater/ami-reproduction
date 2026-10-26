"""Integration checks for portable workers and the fixed five-seed supervisor.

The supervisor and its child processes are real. Only queue workloads and model
providers are fixtures; these tests do not measure baseline answer quality.
"""
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.pipeline.context import FixtureTokenizer
from memory_benchmark.pipeline.ingestion import POLICY, plan
from memory_benchmark.pipeline.prepare import prepare
from memory_benchmark.pipeline.reader import answer
from memory_benchmark.pipeline.windowed_reader import count_prompt
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows
from memory_benchmark.runtime.model_client import Reply
from memory_benchmark.runtime import worker
from memory_benchmark.runtime.native_bridge import worker as bridge_worker
from memory_benchmark.schemas import Evidence, History, Message, Query, Session
METHODS = ('activememoryindex', 'mem0', 'invmem', 'refind')
FIXTURES = Path(__file__).parent / 'fixtures'

class WorkerParityTests(unittest.TestCase):

    def test_both_worker_paths_apply_the_same_chunk_plan_to_all_four_methods(self):
        history = History('locomo_refined', 'episode', ('Alice',), (Session('s1', None, '', tuple((Message(str(i), 'system' if i == 0 else 'user', 'Alice', f'Alice message {i}.', None) for i in range(41)))), Session('s2', '2026-10-01T00:00:00+00:00', '', (Message('long', 'assistant', 'Alice', 'word ' * 4001, None),))))
        expected, audit = plan(history, POLICY)
        for method in METHODS:
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                write_json(root / 'history.json', history.to_wire())
                write_rows(root / 'queries.jsonl', [Query('locomo_refined', 'episode', 'q', 'Alice?').to_wire()])
                received = []

                class Gateway:

                    def __init__(self, *args, **kwargs):
                        self.identity = {}
                        self.budget = SimpleNamespace(failed=None, phase=None, check=lambda: None)

                    def install_native(self):
                        pass

                    def close(self):
                        pass

                class Adapter:

                    def __init__(self, *args, **kwargs):
                        pass

                    def add(self, chunk):
                        received.append(chunk)

                    def finalize(self):
                        pass

                    def search(self, *args):
                        return [Evidence('e', 'Alice fixture evidence')]

                    def close(self):
                        pass
                job = {'method': method, 'method_config': {'adapter_file': 'fixture'}, 'runtime': {'top_k': 10, 'add_chunk_policy': POLICY}, 'offline': False, 'provider_identity': {}, 'scope': 'fixture', 'output': str(root / 'worker'), 'history': str(root / 'history.json'), 'queries': str(root / 'queries.jsonl')}
                job['input_hashes'] = {key: file_hash(job[key]) for key in ('history', 'queries')}
                if method in {'invmem', 'refind'}:
                    write_json(root / 'job.json', job)
                    with patch.object(bridge_worker, 'Gateway', Gateway), patch.object(bridge_worker, 'adapter_class', return_value=Adapter):
                        self.assertEqual(bridge_worker.main(root / 'job.json'), 0)
                else:
                    with patch.object(worker, 'Gateway', Gateway), patch.object(worker, 'create', side_effect=Adapter):
                        self.assertEqual(worker.execute(job)['status'], 'complete')
                self.assertEqual(len(received), len(expected))
                self.assertEqual([chunk.chunk_id for chunk in received], [chunk.chunk_id for chunk in expected])
                self.assertEqual(read_json(root / 'worker/add_plan.json'), audit)

class ConfigPortabilityTests(unittest.TestCase):

    @unittest.skipUnless((ROOT / 'src/aml_sdk/client.py').is_file(), 'HTTP client excluded from core export')
    def test_benchmark_export_keeps_http_retries_and_reexports_without_research_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = (root / 'first', root / 'second')
            for script, output in [(ROOT / 'scripts/export_publication.py', first), (first / 'scripts/export_publication.py', second)]:
                result = subprocess.run([sys.executable, str(script), '--profile', 'benchmark', '--output', str(output)], cwd=root, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
            a, b = (read_json(first / 'PUBLICATION_MANIFEST.json'), read_json(second / 'PUBLICATION_MANIFEST.json'))
            self.assertEqual(a['files'], b['files'])
            self.assertEqual(a['publication_profile'], 'benchmark')
            self.assertEqual(a['sdk']['client'], 'aml_sdk.client.MemoryClient')
            for relative in ['configs/reproduction', 'configs/small_benchmark_v1.json', 'samples/seed-17', 'scripts/run_five_seed_experiments.py', 'scripts/sample_small_benchmark.py', 'scripts/enable_request_retries.py', 'scripts/merge_experiment_repeats.py']:
                self.assertFalse((first / relative).exists(), relative)
            self.assertTrue((first / 'samples/proportional-200-v1/prepared/seed-21/manifest.json').is_file())
            self.assertFalse(a['sample_data']['legacy_seed17_sample_retained'])
            self.assertEqual(a['legacy_version']['commit'], '6ab98adeacd50dc934772a3c64f630b3709f9a24')
            self.assertTrue((first / 'src/aml_sdk/client.py').is_file())
            self.assertFalse((first / 'src/aml_sdk/server.py').exists())
            self.assertFalse((first / 'methods/my_method').exists())
            self.assertIn('http_memory =', (first / 'pyproject.toml').read_text())
            self.assertIn('aml_http', (first / 'src/memory_benchmark/methods/__init__.py').read_text())
            result = subprocess.run([sys.executable, '-c', 'from aml_sdk.client import MemoryClient; print(MemoryClient.__name__)'], cwd=root, env={**os.environ, 'PYTHONPATH': str(first / 'src')}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_export_is_self_contained_and_can_export_itself_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = (root / 'first', root / 'second')
            for script, output in [(ROOT / 'scripts/export_publication.py', first), (first / 'scripts/export_publication.py', second)]:
                result = subprocess.run([sys.executable, str(script), '--profile', 'core', '--output', str(output)], cwd=root, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
            a = read_json(first / 'PUBLICATION_MANIFEST.json')
            b = read_json(second / 'PUBLICATION_MANIFEST.json')
            self.assertEqual(a['files'], b['files'])
            self.assertEqual(a['publication_profile'], 'core')
            self.assertIsNone(a['sdk'])
            self.assertFalse((first / 'src/aml_sdk').exists())
            self.assertFalse((first / 'methods/my_method').exists())
            self.assertFalse((first / 'tests/test_sdk.py').exists())
            self.assertNotIn('aml-sdk =', (first / 'pyproject.toml').read_text())
            self.assertNotIn('aml_http', (first / 'src/memory_benchmark/methods/__init__.py').read_text())

    def test_configuration_reuses_paths_from_other_working_directories_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / 'native'
            for name, roles in [('ami-reproduction', '"user", "assistant"'), ('ami-reproduction-system-role', '"user", "assistant", "system"')]:
                file = base / name / 'app/main.py'
                file.parent.mkdir(parents=True)
                file.write_text(f'class Message:\n    role: Literal[{roles}]\n')
            executable = base / 'ami-reproduction/.venv/bin/python'
            executable.parent.mkdir(parents=True)
            executable.touch()
            env = root / 'nonexistent.env'
            outputs = []
            for index in (1, 2):
                cwd = root / f'work-{index}'
                cwd.mkdir()
                output = root / f'config-{index}'
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/configure_release.py'), '--baseline-root', str(base), '--env-file', str(env), '--output', str(output)], cwd=cwd, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append({p.name: p.read_bytes() for p in output.glob('*.json')})
            self.assertEqual(outputs[0], outputs[1])
            self.assertFalse(env.exists())
            methods = read_json(root / 'config-1/methods.json')
            self.assertEqual(set(methods), set(METHODS))
            self.assertTrue(methods['activememoryindex']['source_root'].endswith('-system-role'))
            self.assertEqual(methods['activememoryindex']['python'], str(executable))
            runtime = read_json(root / 'config-1/runtime.json')
            self.assertEqual(runtime['add_chunk_policy'], POLICY)
            self.assertEqual(runtime['top_k'], 100)
            self.assertEqual(runtime['answer_context_policy'], 'aml_ranked_prefix_v1')
            self.assertIsNone(runtime['reader_token_budget'])
            self.assertEqual(runtime['answer_input_token_limit'], 117760)
            self.assertEqual(runtime['answer_max_tokens'], 8192)
            self.assertEqual(runtime['answer_safety_margin_tokens'], 2048)
            source = 'fact ' * 26000
            calls = []

            class Client:

                def complete(inner, prompt, **options):
                    calls.append((prompt, options))
                    return Reply('evidence notes' if options['stage'] == 'answer_map' else 'answer', runtime['model'])
            tokenizer = FixtureTokenizer()
            result = answer(Query('longmemeval_s', 'e', 'q', 'What are the facts?'), [Evidence('full', source)], ('user', 'assistant'), Client(), tokenizer, runtime)
            self.assertEqual(result['included_ids'], ['full'])
            self.assertEqual(result['omitted_ids'], [])
            self.assertEqual(result['partially_included_ids'], ['full'])
            self.assertTrue(source.startswith(result['reader_context']))
            self.assertEqual(result['reader_mode'], 'ranked_prefix')
            self.assertEqual(len(calls), 1)
            for prompt, options in calls:
                self.assertLessEqual(count_prompt(prompt, tokenizer), 117760)
                self.assertEqual(options['max_tokens'], 8192)
                self.assertLessEqual(count_prompt(prompt, tokenizer) + options['max_tokens'] + 2048, 128000)
if __name__ == '__main__':
    unittest.main()
