from pathlib import Path
import sys, os, json, tempfile, unittest, sqlite3, threading, concurrent.futures as cf, time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from parallel_runner.common import write, read, sha, allowed_retry, validate_config
from parallel_runner.supervisor import ordered_jobs
from parallel_runner.shared_control import SharedControl, ControlStopped
from parallel_runner.controlled_transport import wrap
from unittest.mock import patch
import httpx

class OfflineControl(unittest.TestCase):

    def test_unlimited_budget_and_provider_slots_admit_more_than_twenty(self):
        from contextlib import ExitStack
        control = SharedControl.create(Path(self.t.name) / 'unlimited.sqlite3',
            cap_cny=None, providers={k:{'inflight':None,'rpm':None,'tpm':None} for k in ['llm','embedding']},
            rates={'input':1,'output':1,'embedding':1}, identity={'fixture':'unlimited'})
        with ExitStack() as stack:
            calls = []
            for _ in range(25):
                ident = control.reserve('llm', 'answer', 'fixture', 1000000000, 1)
                calls.append(ident)
                stack.enter_context(control.admit(ident, 'llm', 1000000001, timeout=1))
            self.assertEqual(control.snapshot()['provider_peak_inflight']['llm'], 25)
            self.assertIsNone(control.snapshot()['cap_cny'])
            for ident in calls:
                control.settle(ident, 1, 1)

    def test_optional_provider_limits_still_apply_independently(self):
        from parallel_runner.shared_control import slot_available
        self.assertTrue(slot_available({'inflight':None,'rpm':None,'tpm':None}, 200, 999999, 10**12, 10**12))
        self.assertFalse(slot_available({'inflight':2,'rpm':None,'tpm':None}, 2, 0, 0, 1))
        self.assertFalse(slot_available({'inflight':None,'rpm':2,'tpm':None}, 0, 2, 0, 1))
        self.assertFalse(slot_available({'inflight':None,'rpm':None,'tpm':10}, 0, 0, 8, 3))

    def test_context_overflow_wrapped_as_429_is_not_retried(self):
        from parallel_runner.budget import CallBudget, make_transport
        b = CallBudget({'AMI_TEST_INPUT_CNY_PER_MILLION': '1', 'AMI_TEST_OUTPUT_CNY_PER_MILLION': '1', 'AMI_TEST_EMBED_CNY_PER_MILLION': '1'})
        calls = []
        def send(client, request, **kwargs):
            calls.append(1)
            return httpx.Response(429, request=request, json={'error': {'message': "This model's maximum context length is 128000 tokens."}})
        with patch.dict(os.environ, {'AML_SHARED_LEDGER': str(self.p), 'AML_RETRY_MAX_ATTEMPTS': '8'}), patch('httpx.Client.send', send):
            tr = make_transport(b, 'llm', 'https://fixture.invalid', 'gpt-4o-mini', disable_thinking_required=False)
            with self.assertRaises(RuntimeError):
                tr.handle_request(httpx.Request('POST', 'https://fixture.invalid', json={'model': 'gpt-4o-mini', 'messages': [{'content': 'x'}], 'max_tokens': 1}))
            tr.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(b.rows[-1]['failure_class'], 'context_length_exceeded')

    def setUp(self):
        self.temp_root = ROOT / 'tests/.test-tmp'
        self.temp_root.mkdir(exist_ok=True)
        self.t = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.p = Path(self.t.name) / 'ledger.sqlite3'
        self.c = SharedControl.create(self.p, cap_cny=1, providers={k: {'inflight': 3, 'rpm': 9999, 'tpm': 99999999} for k in ['llm', 'embedding']}, rates={'input': 1, 'output': 1, 'embedding': 1}, identity={'fixture': True})

    def tearDown(self):
        import gc
        gc.collect()
        assert Path(self.t.name).resolve().is_relative_to(self.temp_root.resolve())
        self.t.cleanup()

    def test_parallel_never_exceeds_cap(self):
        active = peak = 0
        lock = threading.Lock()

        def work(i):
            nonlocal active, peak
            x = SharedControl(self.p)
            ident = x.reserve('llm', 'test', 'mock', 100, 20)
            with x.admit(ident, 'llm', 120):
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.01)
                x.settle(ident, 10, 2)
                with lock:
                    active -= 1
        with cf.ThreadPoolExecutor(12) as pool:
            list(pool.map(work, range(24)))
        self.assertLessEqual(peak, 3)
        self.assertGreater(peak, 1)
        self.assertEqual(len(self.c.snapshot()['calls']), 24)

    def test_stop_blocks_new_requests(self):
        self.c.stop('user_stop')
        with self.assertRaises(ControlStopped):
            self.c.reserve('llm', 'test', 'x', 1, 1)

    def test_settlement_after_stop_kept(self):
        i = self.c.reserve('llm', 'test', 'x', 100, 100)
        with self.c.admit(i, 'llm', 200):
            self.c.stop('user_stop')
            self.c.settle(i, 10, 1)
        self.assertEqual(self.c.snapshot()['calls'][0]['state'], 'settled')

    def test_unknown_retains_reservation(self):
        i = self.c.reserve('llm', 'test', 'x', 100, 100)
        self.c.unknown(i, 503)
        s = self.c.snapshot()
        self.assertEqual(s['calls'][0]['state'], 'unknown')
        self.assertGreater(s['committed_and_unknown_cny'], 0)

    def test_budget_blocks_before_network(self):
        with sqlite3.connect(self.p) as db:
            cfg = json.loads(db.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0])
            cfg['cap_units'] = 1
            db.execute("UPDATE settings SET value=? WHERE key='config'", (json.dumps(cfg),))
        with self.assertRaises(ControlStopped):
            SharedControl(self.p).reserve('llm', 'test', 'x', 1000, 1000)

    def test_retry_delay_holds_no_slot(self):

        class Budget:
            rows = []
            failed = None
            phase = 'fixture'
        budget = Budget()
        states = []

        class Inner(httpx.BaseTransport):

            def handle_request(s, req):
                status = 503 if len(budget.rows) == 0 else 200
                row = {'http_status': status}
                budget.rows.append(row)
                if status == 200:
                    row.update(estimated_cny=1.2e-05, input_tokens=10, output_tokens=2)
                return httpx.Response(status, request=req, json={})

        def sleeper(_):
            with sqlite3.connect(self.p) as db:
                states.append(db.execute("SELECT COUNT(*) FROM tickets WHERE state='active'").fetchone()[0])
        with patch.dict(os.environ, {'AML_SHARED_LEDGER': str(self.p), 'AML_RETRY_MAX_ATTEMPTS': '2', 'AML_RETRY_BASE_SECONDS': '0', 'AML_RETRY_MAX_SECONDS': '0'}):
            transport = wrap(Inner(), budget, 'llm', sleeper=sleeper)
            req = httpx.Request('POST', 'https://fixture.invalid/v1/chat/completions', json={'messages': [{'content': 'hi'}], 'max_tokens': 10})
            response = transport.handle_request(req)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(budget.rows), 2)
        self.assertTrue(all((x == 0 for x in states)))
        self.assertEqual([x['state'] for x in self.c.snapshot()['calls']].count('settled'), 1)

    def test_fatal_401_not_retried(self):

        class Budget:
            rows = []
            failed = None
            phase = 'fixture'
        b = Budget()

        class Inner(httpx.BaseTransport):

            def handle_request(s, req):
                b.rows.append({'http_status': 401})
                return httpx.Response(401, request=req, json={})
        with patch.dict(os.environ, {'AML_SHARED_LEDGER': str(self.p), 'AML_RETRY_MAX_ATTEMPTS': '5'}):
            tr = wrap(Inner(), b, 'llm')
            resp = tr.handle_request(httpx.Request('POST', 'https://fixture.invalid', json={'messages': [{'content': 'x'}], 'max_tokens': 1}))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(len(b.rows), 1)

    def test_finite_retry_limit(self):

        class Budget:
            rows = []
            failed = None
            phase = 'fixture'
        b = Budget()

        class Inner(httpx.BaseTransport):

            def handle_request(s, req):
                b.rows.append({'http_status': 429})
                return httpx.Response(429, request=req, json={})
        with patch.dict(os.environ, {'AML_SHARED_LEDGER': str(self.p), 'AML_RETRY_MAX_ATTEMPTS': '3', 'AML_RETRY_BASE_SECONDS': '0', 'AML_RETRY_MAX_SECONDS': '0'}):
            tr = wrap(Inner(), b, 'llm', sleeper=lambda _: None)
            resp = tr.handle_request(httpx.Request('POST', 'https://fixture.invalid', json={'messages': [{'content': 'x'}], 'max_tokens': 1}))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(len(b.rows), 3)

    def test_schedule_interleaves_seeds(self):
        jobs = [{'id': str((s, m, w)), 'seed': s, 'method': m, 'weight': w} for s in [17, 18] for m in ['A', 'B'] for w in [10, 20, 30]]
        out = ordered_jobs(jobs)
        self.assertEqual(len({(j['seed'], j['method']) for j in out[:4]}), 4)
        self.assertEqual([j['weight'] for j in out if j['seed'] == 17 and j['method'] == 'A'], [10, 30, 20])

    def test_attempt_policy(self):
        self.assertTrue(allowed_retry(500, None))
        self.assertFalse(allowed_retry(3, 3))
        self.assertTrue(allowed_retry(1, 3))

    def test_atomic_unicode_artifact(self):
        p = Path(self.t.name) / 'result.json'
        write(p, {'text': '完整结果'})
        self.assertEqual(read(p)['text'], '完整结果')
        self.assertEqual(list(p.parent.glob('*.tmp')), [])

    def test_successful_futures_persist_after_stop(self):
        from parallel_runner.tasks import reader_task
        from parallel_runner.common import taskdir, qdir
        import hashlib
        run = Path(self.t.name) / 'run'
        prep = run / 'prepared/seed-17'
        prep.mkdir(parents=True)
        c = read(ROOT / 'configs/parallel/four-methods.json')
        c.update(ledger_dir=self.t.name, offline=True, query_workers=3)
        runtime = read(ROOT / 'configs/runtime.json')
        runtime['seed'] = 17
        write(prep / 'manifest.json', {'runtime': runtime})
        write(prep / 'history.json', {'benchmark': 'locomo_refined', 'episode_id': 'e', 'speakers': ['A', 'B'], 'sessions': []})
        queries = [{'benchmark': 'locomo_refined', 'episode_id': 'e', 'qa_id': 'q' + str(i), 'text': 'fixture'} for i in range(3)]
        (prep / 'queries.jsonl').write_text('\n'.join((json.dumps(q) for q in queries)) + '\n')
        (prep / 'targets.jsonl').write_text('\n'.join((json.dumps({'qa_id': q['qa_id'], 'gold': 'x', 'capability_leaves': ['A1']}) for q in queries)) + '\n')
        job = {'id': 'fixture', 'seed': 17, 'method': 'AMI', 'case': {'history': 'history.json', 'queries': 'queries.jsonl', 'targets': 'targets.jsonl'}}
        out = taskdir(run, job)
        for q in queries:
            write(out / 'retrieval_cache' / (hashlib.sha256(q['qa_id'].encode()).hexdigest() + '.json'), {'id': q['qa_id'], 'evidence': []})
        barrier = threading.Barrier(3)

        def answer(q, *args):
            barrier.wait(timeout=5)
            if q.qa_id == 'q0':
                self.c.stop('user_stop')
            time.sleep(0.05)
            return {'id': q.qa_id, 'generated_answer': 'successful in-flight result'}
        with patch.dict(os.environ, {}), patch('memory_benchmark.pipeline.reader.answer', answer):
            result = reader_task(str(run), c, job)
        self.assertEqual(result['stage'], 'judge')
        self.assertTrue(all(((qdir(out, q['qa_id']) / 'answer.json').exists() for q in queries)))
        self.assertFalse(any(((qdir(out, q['qa_id']) / 'score.json').exists() for q in queries)))

    def guard_roundtrip(self, network_timeout=False):
        from parallel_runner.budget import CallBudget, make_transport
        budget = CallBudget({'AMI_TEST_INPUT_CNY_PER_MILLION': '1', 'AMI_TEST_OUTPUT_CNY_PER_MILLION': '1', 'AMI_TEST_EMBED_CNY_PER_MILLION': '1'})
        counter = []

        def send(client, request, **kw):
            counter.append(1)
            if len(counter) == 1:
                if network_timeout:
                    raise httpx.ReadTimeout('synthetic timeout')
                return httpx.Response(503, request=request, headers={'retry-after': '0'}, json={'error': {'code': 'server_error'}})
            return httpx.Response(200, request=request, json={'model': 'gpt-4o-mini', 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}, 'choices': [{'message': {'content': 'OK'}}]})
        with patch.dict(os.environ, {'AML_SHARED_LEDGER': str(self.p), 'AML_RETRY_MAX_ATTEMPTS': '2', 'AML_RETRY_BASE_SECONDS': '0', 'AML_RETRY_MAX_SECONDS': '0'}), patch('httpx.Client.send', send):
            tr = make_transport(budget, 'llm', 'https://fixture.invalid/v1/chat/completions', 'gpt-4o-mini', disable_thinking_required=False)
            response = tr.handle_request(httpx.Request('POST', 'https://fixture.invalid/v1/chat/completions', json={'model': 'gpt-4o-mini', 'messages': [{'content': 'x'}], 'max_tokens': 10}))
            tr.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(counter), 2)
        self.assertIsNone(budget.failed)

    def test_real_guard_503_retries_through_wrapped_error(self):
        self.guard_roundtrip()

    def test_real_guard_timeout_retries_through_wrapped_error(self):
        self.guard_roundtrip(True)

    def test_retry_after_survives_closed_response(self):
        from parallel_runner.controlled_transport import retry_delay
        with patch.dict(os.environ, {'AML_RETRY_BASE_SECONDS': '0', 'AML_RETRY_MAX_SECONDS': '60'}):
            self.assertGreaterEqual(retry_delay(None, 0, '5'), 5)

    def test_direct_client_default(self):
        from parallel_runner.budget import CallBudget, make_transport
        b = CallBudget({'AMI_TEST_INPUT_CNY_PER_MILLION': '1', 'AMI_TEST_OUTPUT_CNY_PER_MILLION': '1', 'AMI_TEST_EMBED_CNY_PER_MILLION': '1'})
        cls = httpx.Client
        with patch.dict(os.environ, {'AML_SHARED_LEDGER': str(self.p)}), patch('httpx.Client', wraps=cls) as spy:
            tr = make_transport(b, 'llm', 'https://fixture.invalid', 'gpt-4o-mini')
            self.assertFalse(spy.call_args.kwargs['trust_env'])
            tr.close()

    def test_capability_primary_matches_pinned_queue_rule(self):
        from parallel_runner.report import capability_overall
        s = {'complete': True, 'capabilities': {k: {'score': v} for (k, v) in zip(['A', 'B', 'C', 'D', 'E', 'G', 'H'], [10, 20, 30, 40, 50, 60, 70])}}
        self.assertEqual(capability_overall(s), 40)

    def test_capability_no_incomplete_substitute(self):
        from parallel_runner.report import capability_overall
        self.assertIsNone(capability_overall({'complete': False, 'capabilities': {}}))
        self.assertIsNone(capability_overall({'complete': True, 'capabilities': {'A': {'score': 100}}}))
if __name__ == '__main__':
    unittest.main(verbosity=2)
