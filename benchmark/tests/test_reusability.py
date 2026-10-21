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
from memory_benchmark.pipeline.ingestion import POLICY, plan
from memory_benchmark.pipeline.prepare import prepare
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows
from memory_benchmark.runtime import worker
from memory_benchmark.runtime.native_bridge import worker as bridge_worker
from memory_benchmark.schemas import Evidence, History, Message, Query, Session

METHODS = ('activememoryindex', 'mem0', 'invmem', 'refind')
FIXTURES = Path(__file__).parent / 'fixtures'


class SchedulerProcessTests(unittest.TestCase):
    def setup_run(self, root, *, barrier=1, hold=False, failure=None):
        config, prepared = root / 'config', root / 'prepared'
        config.mkdir()
        runtime = read_json(ROOT / 'configs/runtime.json')
        suite = {'suite_id': 'reusable-fixture', 'scope': 'expanded', 'purpose': 'offline test',
                 'aggregation': 'macro_benchmark', 'benchmarks': {'locomo_refined': {
                     'data_dir': str(ROOT / 'examples/smoke/locomo_refined'), 'media_policy': 'text_only'}}}
        for seed in range(17, 22):
            prepare(suite, {**runtime, 'seed': seed}, prepared / f'seed-{seed}')
        methods = {}
        for method in METHODS:
            native = root / 'sources' / method
            native.mkdir(parents=True)
            (native / 'source.py').write_text('# Native identity fixture\n')
            methods[method] = {'source_root': str(native), 'python': sys.executable}
            if method in {'invmem', 'refind'}:
                methods[method]['adapter_file'] = str(ROOT / f'src/memory_benchmark/methods/{method}.py')
            write_json(config / f'methods.{method}.json', {method: methods[method]})
        write_json(config / 'methods.json', methods)
        write_json(config / 'embedding_policy.json', {})
        settings = root / 'fixture.json'
        write_json(settings, {'barrier': barrier, 'hold': hold, 'failure': failure or {}})
        return settings

    def launch(self, root, *, parallel, per_method, passes=2, extra=()):
        command = [sys.executable, str(FIXTURES / 'scheduler_bootstrap.py'), str(root / 'fixture.json'),
                   '--prepared', str(root / 'prepared'), '--config', str(root / 'config'),
                   '--output', str(root / 'output'), '--parallel', str(parallel),
                   '--parallel-per-method', str(per_method), '--passes', str(passes), *extra]
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(ROOT / 'src'), str(ROOT)]),
               'PYTHONDONTWRITEBYTECODE': '1', 'OMP_NUM_THREADS': '1'}
        log = (root / f'supervisor-{time.time_ns()}.log').open('w')
        process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(log.close)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

    def wait_for(self, condition, root):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.02)
        self.fail('Timed out; logs: ' + '\n'.join(p.read_text()[-1000:] for p in root.glob('supervisor-*.log')))

    def finish(self, process, root, code=0):
        self.assertEqual(process.wait(timeout=25), code,
                         '\n'.join(p.read_text()[-2000:] for p in root.glob('supervisor-*.log')))
        state = read_json(root / 'processes.json')
        if path := os.environ.get('MEMORY_BENCHMARK_TEST_RESULTS'):
            with Path(path).open('a') as handle:
                handle.write(json.dumps({'test': self.id(), 'code_root': str(ROOT), 'exit_code': code,
                    'peak_queues': state['peak'], 'method_peaks': state['method_peaks'],
                    'launches': len(state['launches']), 'active_at_end': len(state['active'])}) + '\n')
        return state

    def test_real_process_limits_for_twenty_eight_and_five_queues(self):
        for total, per in [(20, 5), (8, 2), (5, 2)]:
            with self.subTest(total=total, per=per), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.setup_run(root, barrier=total)
                state = self.finish(self.launch(root, parallel=total, per_method=per), root)
                self.assertEqual(state['peak'], total)
                self.assertTrue(all(n <= per for n in state['method_peaks'].values()))
                self.assertEqual(len(state['launches']), 20)
                self.assertEqual(len({r['pid'] for r in state['launches']}), 20)
                self.assertEqual(read_json(root / 'output/acceptance.json')['status'], 'passed')

    def test_single_method_launches_five_overlapping_real_processes(self):
        for method in METHODS:
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.setup_run(root, barrier=5)
                config = root / 'config'
                available = read_json(config / 'methods.json')
                write_json(config / 'methods.json', {method: available[method]})
                for other in METHODS:
                    if other != method:
                        (config / f'methods.{other}.json').unlink()
                state = self.finish(self.launch(root, parallel=5, per_method=5,
                                                extra=['--method', method]), root)
                self.assertEqual(state['peak'], 5)
                self.assertEqual(state['method_peaks'], {method: 5})
                self.assertEqual(len({row['pid'] for row in state['launches']}), 5)
                self.assertEqual({row['key'] for row in state['launches']},
                                 {f'{method}:seed-{seed}' for seed in range(17, 22)})
                acceptance = read_json(root / 'output/acceptance.json')
                self.assertEqual({key: acceptance[key] for key in ('status', 'scored', 'expected')},
                                 {'status': 'passed', 'scored': 10, 'expected': 10})
                progress = read_json(root / 'output/progress.json')
                self.assertEqual(progress['total_rounds'], 5)
                self.assertEqual(progress['completed_rounds'], 5)
                self.assertEqual(set(read_json(root / 'output/summary.json')['overall']), {method})

    def test_single_method_still_obeys_per_method_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.setup_run(root, barrier=2)
            state = self.finish(self.launch(root, parallel=5, per_method=2,
                                           extra=['--method', 'mem0']), root)
            self.assertEqual(state['peak'], 2)
            self.assertEqual(state['method_peaks'], {'mem0': 2})
            self.assertEqual(len(state['launches']), 5)

    def test_selected_methods_resume_and_reject_changed_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.setup_run(root, barrier=10)
            state = self.finish(self.launch(root, parallel=10, per_method=5,
                                           extra=['--method', 'invmem', '--method', 'mem0']), root)
            self.assertEqual(state['peak'], 10)
            self.assertEqual(state['method_peaks'], {'mem0': 5, 'invmem': 5})
            self.assertEqual(len(state['launches']), 10)
            self.assertEqual(read_json(root / 'output/progress.json')['total_rounds'], 10)
            self.assertEqual(read_json(root / 'output/acceptance.json')['expected'], 20)
            snapshots = {p: file_hash(p) for p in (root / 'output').rglob('answers.fixture.json')}
            state = self.finish(self.launch(root, parallel=10, per_method=5,
                                           extra=['--resume', '--method', 'mem0', '--method', 'invmem']), root)
            self.assertEqual(len(state['launches']), 10)
            self.assertEqual(snapshots, {p: file_hash(p) for p in snapshots})
            changed = self.launch(root, parallel=10, per_method=5,
                                  extra=['--resume', '--method', 'mem0'])
            self.assertNotEqual(changed.wait(timeout=5), 0)
            self.assertEqual(len(read_json(root / 'processes.json')['launches']), 10)
            self.assertEqual(snapshots, {p: file_hash(p) for p in snapshots})

    def test_invalid_method_selection_never_launches_a_queue(self):
        for selected in [('mem0', 'mem0'), ('unknown',), ('invmem',)]:
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.setup_run(root)
                methods = read_json(root / 'config/methods.json')
                write_json(root / 'config/methods.json', {'mem0': methods['mem0']})
                flags = [part for method in selected for part in ('--method', method)]
                process = self.launch(root, parallel=5, per_method=5, extra=flags)
                self.assertEqual(process.wait(timeout=5), 2)
                self.assertFalse((root / 'processes.json').exists())

    def test_failed_stage_retries_and_complete_resume_preserves_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.setup_run(root, failure={'key': 'mem0:seed-17'})
            state = self.finish(self.launch(root, parallel=5, per_method=2), root)
            counts = Counter(r['key'] for r in state['launches'])
            self.assertEqual(counts['mem0:seed-17'], 2)
            self.assertEqual(sum(counts.values()), 21)
            retried = [r for r in state['launches'] if r['key'] == 'mem0:seed-17']
            self.assertTrue(retried[1]['resume'])
            answer = root / 'output/mem0/seed-17/answers.fixture.json'
            self.assertEqual(read_json(answer)['writer_pid'], retried[0]['pid'])
            snapshots = {p: file_hash(p) for p in (root / 'output').rglob('answers.fixture.json')}
            state = self.finish(self.launch(root, parallel=5, per_method=2, extra=['--resume']), root)
            self.assertEqual(len(state['launches']), 21)
            self.assertEqual(snapshots, {p: file_hash(p) for p in snapshots})

    def test_exhausted_queue_is_incomplete_and_never_publishes_subset_overall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.setup_run(root, failure={'key': 'mem0:seed-17', 'always': True})
            state = self.finish(self.launch(root, parallel=5, per_method=2), root, code=1)
            self.assertEqual(sum(r['key'] == 'mem0:seed-17' for r in state['launches']), 2)
            summary = read_json(root / 'output/summary.json')
            self.assertEqual(summary['status'], 'incomplete')
            self.assertIsNone(summary['overall']['mem0']['overall_macro_capability'])
            self.assertIsNotNone(summary['overall']['refind']['overall_macro_capability'])

    def test_supervisor_lock_and_handoff_preserve_real_queue_pids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.setup_run(root, hold=True)
            first = self.launch(root, parallel=2, per_method=1)
            self.addCleanup(lambda: settings.with_suffix('.release').touch() if settings.parent.exists() else None)
            self.wait_for(lambda: len(read_json(root / 'processes.json')['active']) == 2 and
                          read_json(root / 'output/progress.json')['active_count'] == 2, root)
            pids = set(read_json(root / 'processes.json')['active'])
            duplicate = self.launch(root, parallel=2, per_method=1, extra=['--resume', '--adopt-running'])
            self.assertNotEqual(duplicate.wait(timeout=5), 0)
            self.assertEqual(set(read_json(root / 'processes.json')['active']), pids)
            self.stop(first)
            second = self.launch(root, parallel=5, per_method=2,
                                 extra=['--resume', '--adopt-running', '--resize-concurrency'])
            self.wait_for(lambda: len(read_json(root / 'processes.json')['active']) == 5, root)
            self.assertTrue(pids <= set(read_json(root / 'processes.json')['active']))
            changes = [json.loads(l) for l in (root / 'output/scheduling_changes.jsonl').read_text().splitlines()]
            self.assertEqual(len(changes[-1]['adopted_tasks']), 2)
            settings.with_suffix('.release').touch()
            state = self.finish(second, root)
            self.assertEqual(len(state['launches']), 20)
            self.assertEqual(state['peak'], 5)
            self.assertTrue(all(v <= 2 for v in state['method_peaks'].values()))


class WorkerParityTests(unittest.TestCase):
    def test_both_worker_paths_apply_the_same_chunk_plan_to_all_four_methods(self):
        history = History('locomo_refined', 'episode', ('Alice',), (
            Session('s1', None, '', tuple(Message(str(i), 'system' if i == 0 else 'user',
                'Alice', f'Alice message {i}.', None) for i in range(41))),
            Session('s2', '2026-10-01T00:00:00+00:00', '', (
                Message('long', 'assistant', 'Alice', 'word ' * 4001, None),))))
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
                    def install_native(self): pass
                    def close(self): pass

                class Adapter:
                    def __init__(self, *args, **kwargs): pass
                    def add(self, chunk): received.append(chunk)
                    def finalize(self): pass
                    def search(self, *args): return [Evidence('e', 'Alice fixture evidence')]
                    def close(self): pass

                job = {'method': method, 'method_config': {'adapter_file': 'fixture'},
                       'runtime': {'top_k': 10, 'add_chunk_policy': POLICY}, 'offline': False,
                       'provider_identity': {}, 'scope': 'fixture', 'output': str(root / 'worker'),
                       'history': str(root / 'history.json'), 'queries': str(root / 'queries.jsonl')}
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
    def test_core_export_is_self_contained_and_can_export_itself_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / 'first', root / 'second'
            for script, output in [(ROOT / 'scripts/export_publication.py', first),
                                   (first / 'scripts/export_publication.py', second)]:
                result = subprocess.run([sys.executable, str(script), '--profile', 'core', '--output', str(output)],
                                        cwd=root, capture_output=True, text=True, timeout=15)
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
            for name, roles in [('ami-reproduction', '"user", "assistant"'),
                                ('ami-reproduction-system-role', '"user", "assistant", "system"')]:
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
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/configure_release.py'),
                    '--baseline-root', str(base), '--env-file', str(env), '--output', str(output)],
                    cwd=cwd, capture_output=True, text=True, timeout=10)
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
            self.assertEqual(runtime['answer_context_policy'], 'model_window_v2')

    def test_sampling_replays_and_freezing_rejects_modified_source_or_selected_list(self):
        from test_sampling import PublicSourceSamplingTests
        helper = PublicSourceSamplingTests()
        for changed in ('source', 'selected'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                suite, policy = helper.inputs(root)
                for name in ('sampling-one', 'sampling-two'):
                    helper.run_cli('sample_small_benchmark.py', ['--suite', suite, '--config', policy,
                        '--output', root / name], root)
                for seed in range(17, 22):
                    relative = f'seed-{seed}/selected_questions.jsonl'
                    self.assertEqual((root / 'sampling-one' / relative).read_bytes(),
                                     (root / 'sampling-two' / relative).read_bytes())
                target = root / ('public.jsonl' if changed == 'source' else
                                  'sampling-one/seed-17/selected_questions.jsonl')
                with target.open('a') as handle:
                    handle.write('\n')
                output = root / 'rejected-prepared'
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/prepare_sampled_seeds.py'),
                    '--sampling', str(root / 'sampling-one'), '--suite', str(suite),
                    '--runtime', str(ROOT / 'configs/runtime.json'), '--output', str(output)],
                    cwd=root, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                    capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('changed', result.stderr)
                self.assertFalse((output / 'summary.json').exists())


if __name__ == '__main__':
    unittest.main()
