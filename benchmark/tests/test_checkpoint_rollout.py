"""Exercise installation and rollback with a live coordinator/worker tree."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from memory_benchmark.pipeline.prepare import code_identity
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json
from scripts import enable_memory_checkpoints as rollout
from scripts.resume_with_memory_checkpoints import overlay_environment


class CheckpointRolloutTests(unittest.TestCase):
    def test_new_queue_selects_its_overlay_and_preserves_other_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp)
            command = ['python', '-m', 'memory_benchmark.runtime.native_bridge.runner', '--output', temp]
            environment = {'PYTHONPATH': 'release/src', 'OMP_NUM_THREADS': '1'}
            self.assertEqual(overlay_environment(command, environment), environment)
            write_json(queue / 'execution_overlay.json', {'version': 1,
                'activation': 'new_memory_workers', 'changes': {}})
            actual = overlay_environment(command, environment)
            self.assertEqual(actual['PYTHONPATH'], str(queue / 'frozen_framework/src'))
            self.assertEqual(actual['OMP_NUM_THREADS'], '1')
            self.assertEqual(environment['PYTHONPATH'], 'release/src')

    def test_new_queue_refuses_changed_overlay_file(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp)
            (queue / 'worker.py').write_text('changed')
            write_json(queue / 'execution_overlay.json', {'version': 1,
                'activation': 'new_memory_workers', 'changes': {'worker.py': {'after_sha256': 'original'}}})
            with self.assertRaisesRegex(ValueError, 'overlay changed'):
                overlay_environment(['python', '--output', temp], {})

    def check(self, fail=False):
        with tempfile.TemporaryDirectory() as temp:
            experiment = Path(temp)
            results = experiment / 'results'
            queue = results / 'mem0/seed-17'
            source = queue / 'frozen_framework/src/dummy.py'
            source.parent.mkdir(parents=True)
            source.write_text('original_framework = True\n')
            framework = code_identity(queue / 'frozen_framework')
            bridge = queue / 'frozen_method_bridge'
            bridge.mkdir()
            for name in ('worker.py', 'runner.py'):
                (bridge / name).write_text('original_bridge = True\n')
            bridge_hashes = {p.name: file_hash(p) for p in bridge.iterdir()}
            write_json(queue / 'manifest.json', {'framework': framework,
                'execution': {'method_side_bridge_sha256': bridge_hashes}})
            write_json(results / 'launch_plan.json', {'file_sha256': {}, 'methods': {'mem0': {}}, 'seeds': [17]})
            write_json(results / 'progress.json', {'tasks': {}})
            answer = queue / 'answers.jsonl'
            answer.write_text('{"id":"q","answer":"preserve"}\n')
            saved_answer = answer.read_bytes()
            worker_code = 'import time; time.sleep(120)'
            queue_code = ('import subprocess,sys,time; '
                f"p=subprocess.Popen([sys.executable,'-c',{worker_code!r},sys.argv[1]]); "
                "open(sys.argv[1]+'/worker.pid','w').write(str(p.pid)); time.sleep(120)")
            supervisor_code = ('import subprocess,sys,time; '
                f"p=subprocess.Popen([sys.executable,'-c',{queue_code!r},sys.argv[1]]); "
                "open(sys.argv[1]+'/queue.pid','w').write(str(p.pid)); time.sleep(120)")
            supervisor = subprocess.Popen([sys.executable, '-c', supervisor_code, str(experiment)], start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not (experiment / 'worker.pid').exists():
                    if time.monotonic() > deadline:
                        self.fail('Fixture worker did not start')
                    time.sleep(0.01)
                worker_pid = int((experiment / 'worker.pid').read_text())
                worker = rollout.process(worker_pid)
                state = rollout.process(supervisor.pid)
                write_json(results / 'supervisor.json', {'pid': supervisor.pid, 'process_token': state['token']})
                real_kill = os.kill
                signals = []
                def kill(pid, sig):
                    signals.append((pid, sig))
                    real_kill(pid, sig)
                with patch.object(rollout.os, 'kill', side_effect=kill), redirect_stdout(io.StringIO()):
                    if fail:
                        with patch.object(rollout, 'verify_framework_overlay', side_effect=ValueError('injected')):
                            with self.assertRaisesRegex(ValueError, 'injected'):
                                rollout.apply(experiment)
                    else:
                        rollout.apply(experiment)
                self.assertFalse(any(pid == worker_pid for pid, _ in signals))
                self.assertEqual(rollout.process(worker_pid)['token'], worker['token'])
                self.assertNotIn(rollout.process(supervisor.pid)['state'], ('T', 't'))
                self.assertEqual(answer.read_bytes(), saved_answer)
                if fail:
                    self.assertFalse((queue / 'execution_overlay.json').exists())
                    self.assertEqual(code_identity(queue / 'frozen_framework'), framework)
                    self.assertEqual({p.name: file_hash(p) for p in bridge.iterdir()}, bridge_hashes)
                else:
                    receipt = read_json(next(experiment.glob('memory-checkpoint-rollout-*/activation_receipt.json')))
                    self.assertEqual(receipt['worker_processes_signaled'], 0)
                    self.assertTrue(receipt['active_workers_preserved'][0]['still_same_process'])
                    self.assertTrue((queue / 'execution_overlay.json').exists())
            finally:
                os.killpg(supervisor.pid, signal.SIGTERM)
                supervisor.wait(timeout=5)

    def test_rollout_keeps_active_worker_alive_and_saved_answer_unchanged(self):
        self.check()

    def test_failed_rollout_restores_files_and_resumes_coordinators(self):
        self.check(fail=True)


if __name__ == '__main__':
    unittest.main()
