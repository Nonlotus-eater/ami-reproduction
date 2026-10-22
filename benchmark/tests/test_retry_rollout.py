"""Memory-only activation preserves legacy identities, live workers and rollback."""
from contextlib import redirect_stdout
import copy
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
from memory_benchmark.runtime.memory_checkpoint import MemoryCheckpoint, POLICY
from memory_benchmark.runtime.memory_retry import gateway_for_memory
from memory_benchmark.runtime.model_client import Gateway
from memory_benchmark.runtime.profile import public_identity
from scripts import enable_request_retries as rollout


class MemoryRetryTests(unittest.TestCase):
    def fixture(self, root):
        profile = {'OPENAI_BASE_URL': 'https://provider.invalid/v1', 'AMI_EMBED_BASE_URL': 'https://embed.invalid/v1',
                   'OPENAI_API_KEY': 'FAKE_LLM', 'AMI_EMBED_API_KEY': 'FAKE_EMBED', 'AMI_ALLOW_RETRIES': '1'}
        env = root / 'fixture.env'; env.write_text('\n'.join(k+'='+v for k,v in profile.items()))
        runtime = read_json(Path(__file__).parents[1] / 'configs/runtime.json')
        runtime.pop('max_retries', None); runtime['env_file'] = str(env)
        output = root / 'mem0/beam/history/memory_attempt_001'; output.mkdir(parents=True)
        identity = public_identity(profile, runtime)
        receipt = {'version': 1, 'activation': 'new_memory_workers', 'memory_checkpoint_policy': POLICY,
                   'baseline_framework_sha256': 'fixture', 'changes': {},
                   'request_retry_policy': {'version': 1, 'owner': 'transport', 'max_retries': 10, 'activated_at': 'fixture'}}
        write_json(root/'execution_overlay.json', receipt)
        job = {'method': 'mem0', 'offline': False, 'runtime': runtime, 'provider_identity': identity,
               'output': str(output), 'scope': 'run:mem0:history:memory_attempt_001',
               'method_config': {'adapter_file': str(Path(__file__).parents[1]/'src/memory_benchmark/methods/mem0.py')},
               'input_hashes': {'history': 'fixture', 'queries': 'fixture'}}
        return job, receipt

    def test_new_worker_enables_retries_without_mutating_job_or_embedding_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); job,_=self.fixture(root); original=copy.deepcopy(job)
            legacy=Gateway(job['runtime'], root/'legacy.json', expected_identity=job['provider_identity'])
            current=gateway_for_memory(job, root/'current.json', Gateway)
            try:
                self.assertEqual(job, original)
                self.assertEqual(current.identity, legacy.identity)
                self.assertNotIn('request_retry_policy', current.identity)
                self.assertEqual(legacy.budget.retry_policy.effective_max_retries, 0)
                self.assertEqual(current.budget.retry_policy.effective_max_retries, 10)
                saved=read_json(root/'current.json')
                self.assertEqual(saved['request_retry_policy']['max_attempts'], 11)
                self.assertIsNotNone(saved['memory_retry_activation'])
            finally:
                current.close(); legacy.close()

    def test_existing_memory_checkpoint_contract_survives_retry_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); job,receipt=self.fixture(root)
            before=dict(receipt); before.pop('request_retry_policy')
            write_json(root/'execution_overlay.json', before)
            first=MemoryCheckpoint(job, {'policy': 'fixture'}, 1)
            contract=first.contract; scope=first.scope; first.close()
            write_json(root/'execution_overlay.json', receipt)
            next_output=Path(job['output']).with_name('memory_attempt_002'); next_output.mkdir()
            next_job={**job, 'output': str(next_output), 'scope': 'run:mem0:history:memory_attempt_002'}
            second=MemoryCheckpoint(next_job, {'policy': 'fixture'}, 1)
            try:
                self.assertEqual(second.contract, contract)
                self.assertEqual(second.scope, scope)
            finally: second.close()

    def test_changed_receipt_file_rejected_before_gateway_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); job,receipt=self.fixture(root)
            path=root/'checked.py'; path.write_text('changed')
            receipt['changes']['checked.py']={'before_sha256':None,'after_sha256':'wrong'}
            write_json(root/'execution_overlay.json',receipt)
            with patch('memory_benchmark.runtime.memory_retry.checked_changes', side_effect=ValueError('changed')):
                with patch('memory_benchmark.runtime.memory_retry.RetryPolicy') as policy:
                    with self.assertRaises(ValueError): gateway_for_memory(job, root/'usage.json', Gateway)
                    policy.assert_not_called()


class RetryRolloutTests(unittest.TestCase):
    def check(self, failure):
        with tempfile.TemporaryDirectory() as temp:
            experiment=Path(temp); results=experiment/'results'; queue=results/'mem0/seed-17'
            worker=queue/'frozen_framework/src/memory_benchmark/runtime/worker.py'
            worker.parent.mkdir(parents=True); worker.write_text('baseline = True\n')
            baseline=code_identity(queue/'frozen_framework'); original=queue/'execution_overlays/old/originals/worker.py'
            original.parent.mkdir(parents=True); original.write_bytes(worker.read_bytes())
            worker.write_text('checkpoint_overlay = True\n'); previous_worker=worker.read_bytes()
            env=experiment/'fixture.env'; env.write_text('OPENAI_BASE_URL=https://provider.invalid/v1\nAMI_EMBED_BASE_URL=https://embed.invalid/v1\nOPENAI_API_KEY=FAKE\nAMI_EMBED_API_KEY=FAKE\nAMI_ALLOW_RETRIES=1\n')
            runtime={'model':'gpt-4o-mini','env_file':str(env)}
            profile=rollout.read_profile(env)
            write_json(queue/'manifest.json',{'runtime':runtime,'framework':baseline,'execution':{'provider_identity':public_identity(profile,runtime)}})
            receipt={'version':1,'activation':'new_memory_workers','baseline_framework_sha256':baseline['sha256'],
                     'memory_checkpoint_policy':POLICY,'changes':{str(worker.relative_to(queue)):{
                         'before_sha256':file_hash(original),'after_sha256':file_hash(worker),'original':str(original.relative_to(queue))}}}
            write_json(queue/'execution_overlay.json',receipt)
            write_json(results/'launch_plan.json',{'file_sha256':{},'methods':{'mem0':{}},'seeds':[17]})
            answer=queue/'answers.jsonl'; answer.write_text('{"id":"keep"}\n'); preserved=answer.read_bytes()
            worker_code='import time;time.sleep(120)'
            queue_code='import subprocess,sys,time;p=subprocess.Popen([sys.executable,"-c",'+repr(worker_code)+',sys.argv[1]]);open(sys.argv[1]+"/worker.pid","w").write(str(p.pid));time.sleep(120)'
            supervisor_code='import subprocess,sys,time;p=subprocess.Popen([sys.executable,"-c",'+repr(queue_code)+',sys.argv[1]]);open(sys.argv[1]+"/queue.pid","w").write(str(p.pid));time.sleep(120)'
            supervisor=subprocess.Popen([sys.executable,'-c',supervisor_code,str(experiment)],start_new_session=True)
            try:
                deadline=time.monotonic()+5
                while not (experiment/'worker.pid').exists():
                    if time.monotonic()>deadline: self.fail('fixture worker did not start')
                    time.sleep(.01)
                worker_pid=int((experiment/'worker.pid').read_text()); queue_pid=int((experiment/'queue.pid').read_text())
                before=rollout.process(worker_pid)
                write_json(results/'supervisor.json',{'pid':supervisor.pid,'process_token':rollout.process(supervisor.pid)['token']})
                signals=[]; kill=os.kill; verify=rollout.verify_framework_overlay; verifications=[0]
                def record(pid,sig): signals.append((pid,sig));kill(pid,sig)
                def verify_or_fail(*args):
                    verifications[0]+=1
                    if failure and verifications[0]==2: raise ValueError('injected post-install failure')
                    return verify(*args)
                with patch.object(rollout,'coordinators',return_value=[rollout.process(supervisor.pid),rollout.process(queue_pid)]), \
                     patch.object(rollout,'workers',return_value=[before]), patch.object(rollout,'wait_for_imports'), \
                     patch.object(rollout.os,'kill',side_effect=record), patch.object(rollout,'verify_framework_overlay',side_effect=verify_or_fail):
                    if failure:
                        with self.assertRaisesRegex(ValueError,'injected'): rollout.apply(experiment)
                    else:
                        result=rollout.apply(experiment)
                        self.assertEqual(result['worker_processes_signaled'],0)
                self.assertFalse(any(pid==worker_pid for pid,_ in signals))
                self.assertEqual(rollout.process(worker_pid)['token'],before['token'])
                self.assertNotIn(rollout.process(supervisor.pid)['state'],('T','t'))
                self.assertEqual(answer.read_bytes(),preserved)
                current=read_json(queue/'execution_overlay.json')
                if failure:
                    self.assertEqual(current,receipt)
                    self.assertEqual(worker.read_bytes(),previous_worker)
                else:
                    self.assertEqual(current['request_retry_policy']['max_retries'],10)
                    self.assertEqual(current['changes'][str(worker.relative_to(queue))]['before_sha256'],file_hash(original))
                    self.assertEqual(worker.read_bytes(),(rollout.ROOT/'src/memory_benchmark/runtime/worker.py').read_bytes())
                verify(queue/'frozen_framework',baseline,code_identity(queue/'frozen_framework'))
            finally:
                os.killpg(supervisor.pid,signal.SIGTERM);supervisor.wait(timeout=5)

    def test_live_worker_preserved_and_old_overlay_extended(self): self.check(False)
    def test_failed_install_rolls_back_and_resumes_coordinators(self): self.check(True)


if __name__=='__main__': unittest.main()
