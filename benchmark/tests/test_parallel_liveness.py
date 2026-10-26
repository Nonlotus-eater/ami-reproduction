"""HTTP settlement and process-death recovery across real PID namespaces."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from parallel_runner.shared_control import SharedControl, DuplicateReservation
from parallel_runner.prepare import clear_user_stop
from parallel_runner.budget import CallBudget, make_transport
from memory_benchmark.runtime.worker_launch import sandbox_command, python_prefixes
import httpx


class NamespaceLivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.ledger = self.folder / 'ledger.sqlite3'
        self.control = SharedControl.create(self.ledger, cap_cny=None,
            providers={k:{'inflight':None,'rpm':None,'tpm':None} for k in ['llm','embedding']},
            rates={'input':1,'output':1,'embedding':1}, identity={'test':'namespace-liveness'})

    def wait(self, condition, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = condition()
            if value:
                return value
            time.sleep(.01)
        self.fail('Timed out waiting for the local test workload')

    def read(self, role, kind):
        path = self.folder / (role + '-' + kind + '.json')
        return json.loads(path.read_text()) if path.exists() else None

    def sandbox(self, mode, role, endpoint='unused'):
        fixture = ROOT / 'tests/fixtures/ledger_namespace_worker.py'
        readonly = [(ROOT / 'scripts', ROOT / 'scripts'), (fixture, fixture)]
        readonly += [(p,p) for p in python_prefixes(sys.executable) if not p.is_relative_to('/usr')]
        env = {'PATH':'/usr/bin:/bin','HOME':'/tmp/home','PYTHONPATH':str(ROOT / 'scripts'),
            'PYTHONDONTWRITEBYTECODE':'1','AML_SHARED_LEDGER':str(self.ledger),
            'AML_RETRY_MAX_ATTEMPTS':'1'}
        command = sandbox_command([sys.executable,'-B',str(fixture),mode,str(self.folder),role,endpoint],
            readonly=readonly, writable=[self.folder], cwd=self.folder, env=env)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            (self.folder / (role + '-crash')).touch()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
        self.addCleanup(cleanup)
        return process

    def limit_to_one(self):
        with self.control.transaction() as db:
            config = json.loads(db.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0])
            config['providers']['llm']['inflight'] = 1
            db.execute("UPDATE settings SET value=? WHERE key='config'", (json.dumps(config),))

    def ticket(self, ident):
        with self.control.reader() as db:
            return db.execute('SELECT state FROM tickets WHERE call_id=?', (ident,)).fetchone()

    def finish_one(self):
        ident = self.control.reserve('llm','follower','host',100,10)
        with self.control.admit(ident,'llm',110,timeout=8):
            self.control.settle(ident,1,1)
        return ident

    def test_http_200_settles_with_host_and_two_isolated_workers_in_both_directions(self):
        release = threading.Event()
        entered = []
        condition = threading.Condition()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                role = value['messages'][0]['content']
                with condition:
                    entered.append(role)
                    condition.notify_all()
                if not release.wait(15):
                    self.send_error(500)
                    return
                body = json.dumps({'model':'gpt-4o-mini','choices':[{'message':{'content':role}}],
                    'usage':{'prompt_tokens':10,'completion_tokens':1}}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self,*args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        endpoint = f'http://127.0.0.1:{server.server_port}/v1/chat/completions'
        def host(role):
            budget = CallBudget({'AMI_TEST_INPUT_CNY_PER_MILLION':'1',
                'AMI_TEST_OUTPUT_CNY_PER_MILLION':'1','AMI_TEST_EMBED_CNY_PER_MILLION':'1'})
            budget.phase = role
            transport = make_transport(budget,'llm',endpoint,'gpt-4o-mini',disable_thinking_required=False)
            with httpx.Client(transport=transport,trust_env=False) as client:
                response = client.post(endpoint,json={'model':'gpt-4o-mini',
                    'messages':[{'role':'user','content':role}],'max_tokens':8})
                self.assertIsNone(budget.failed)
                return response.status_code, response.json()['choices'][0]['message']['content']
        try:
            with patch.dict(os.environ,{'AML_SHARED_LEDGER':str(self.ledger),'AML_RETRY_MAX_ATTEMPTS':'1'}), ThreadPoolExecutor(2) as pool:
                try:
                    first = pool.submit(host,'host-first')
                    self.wait(lambda:len(entered)==1)
                    a = self.sandbox('request','sandbox-a',endpoint)
                    self.wait(lambda:len(entered)==2)
                    b = self.sandbox('request','sandbox-b',endpoint)
                    self.wait(lambda:len(entered)==3)
                    last = pool.submit(host,'host-last')
                    self.wait(lambda:len(entered)==4)
                    self.assertEqual([x['state'] for x in self.control.snapshot()['calls']], ['reserved']*4)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=8),(200,'host-first'))
                self.assertEqual(last.result(timeout=8),(200,'host-last'))
                for process in [a,b]:
                    stdout,stderr = process.communicate(timeout=8)
                    self.assertEqual(process.returncode,0,stderr)
            values = [self.read(role,'result') for role in ['sandbox-a','sandbox-b']]
            self.assertEqual([v['status'] for v in values],[200,200])
            self.assertTrue(all(v['failed'] is None for v in values))
            self.assertEqual(values[0]['pid'],values[1]['pid'])
            self.assertNotEqual(values[0]['namespace'],values[1]['namespace'])
            self.assertNotEqual(values[0]['namespace'],os.readlink('/proc/self/ns/pid'))
            snapshot = self.control.snapshot()
            self.assertEqual([x['state'] for x in snapshot['calls']],['settled']*4)
            self.assertEqual([x['state'] for x in snapshot['provider_tickets']],['finished']*4)
        finally:
            release.set()
            server.shutdown()
            server.server_close()

    def test_crashed_active_worker_releases_slot_and_keeps_unknown_charge(self):
        self.limit_to_one()
        process = self.sandbox('hold','active')
        ident = self.wait(lambda:self.read('active','active'))['id']
        self.assertIs(self.control.request_alive(ident),True)
        (self.folder / 'active-crash').touch()
        process.communicate(timeout=8)
        self.assertEqual(process.returncode,7)
        self.assertIs(self.control.request_alive(ident),False)
        follower = self.finish_one()
        calls = {x['id']:x for x in self.control.snapshot()['calls']}
        self.assertEqual(calls[ident]['state'],'unknown')
        self.assertEqual(calls[ident]['charged'],calls[ident]['reserved'])
        self.assertEqual(self.ticket(ident)['state'],'abandoned')
        self.assertEqual(calls[follower]['state'],'settled')

    def test_crashed_waiting_head_is_cancelled_without_charge_or_starvation(self):
        self.limit_to_one()
        first = self.control.reserve('llm','first','host',100,10)
        with ThreadPoolExecutor(1) as pool:
            with self.control.admit(first,'llm',110):
                process = self.sandbox('hold','waiting')
                ident = self.wait(lambda:self.read('waiting','ready'))['id']
                self.wait(lambda:self.ticket(ident))
                self.assertEqual(self.ticket(ident)['state'],'waiting')
                (self.folder / 'waiting-crash').touch()
                process.communicate(timeout=8)
                self.assertEqual(process.returncode,7)
                follower = pool.submit(self.finish_one)
                self.wait(lambda:self.ticket(ident)['state']=='abandoned')
                self.control.settle(first,1,1)
            follower.result(timeout=8)
        call = next(x for x in self.control.snapshot()['calls'] if x['id']==ident)
        self.assertEqual(call['state'],'cancelled_unsent')
        self.assertEqual(call['charged'],0)

    def test_resume_refuses_live_isolated_request_then_recovers_real_crash(self):
        process = self.sandbox('hold','resume')
        ident = self.wait(lambda:self.read('resume','active'))['id']
        self.control.stop('user_stop')
        (self.folder / 'STOP.json').write_text('{}')
        config = {'ledger_dir':str(self.folder)}
        with self.assertRaisesRegex(ValueError,'still live'):
            clear_user_stop(self.folder,config)
        self.assertTrue((self.folder/'STOP.json').exists())
        (self.folder / 'resume-crash').touch()
        process.communicate(timeout=8)
        clear_user_stop(self.folder,config)
        self.control.check()
        self.assertFalse((self.folder/'STOP.json').exists())
        self.assertEqual(self.ticket(ident)['state'],'abandoned')

    def test_missing_lease_is_not_evidence_of_exit_and_duplicate_settle_stays_rejected(self):
        ident = self.control.reserve('llm','missing','host',100,10)
        with self.control.transaction() as db:
            db.execute("INSERT INTO tickets(call_id,provider,pid,state,tokens,queued) VALUES(?,?,?,?,?,?)",
                (ident,'llm',999999999,'waiting',110,time.time()))
            self.assertEqual(self.control.reap_abandoned(db),0)
        self.assertEqual(self.ticket(ident)['state'],'waiting')
        done = self.control.reserve('llm','done','host',100,10)
        self.control.settle(done,1,1)
        with self.assertRaises(DuplicateReservation):
            self.control.settle(done,1,1)

    def test_legacy_pid_ledger_is_rejected(self):
        with self.control.transaction() as db:
            config=json.loads(db.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0])
            config.pop('liveness_policy')
            db.execute("UPDATE settings SET value=? WHERE key='config'",(json.dumps(config),))
        with self.assertRaisesRegex(ValueError,'prepare a fresh run'):
            SharedControl(self.ledger)
