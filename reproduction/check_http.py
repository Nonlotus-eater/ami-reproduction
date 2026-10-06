"""Loopback-only fake integration checks. Starts/stops its own server; no paid APIs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    artifacts = ROOT / 'artifacts' / 'http'
    artifacts.mkdir(parents=True, exist_ok=True)
    results = []
    token = secrets.token_urlsafe(32)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AMI_', 'OPENAI_', 'DASHSCOPE_'))}
    env.update(AMI_REPRO_TEST_TOKEN=token, NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    server = None
    def check(value, label):
        results.append({'check': label, 'passed': bool(value)})
        print(('PASS' if value else 'FAIL'), label, flush=True)
        if not value:
            raise AssertionError(label)
    with tempfile.TemporaryDirectory(prefix='ami-http-') as temp, (artifacts / 'server.log').open('w') as log:
        db = Path(temp) / 'fake.sqlite3'
        def start():
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            proc = subprocess.Popen([sys.executable, '-m', 'reproduction.serve', '--mode', 'fake',
                                     '--port', str(port), '--db', str(db)], cwd=ROOT, env=env, stdout=log, stderr=log)
            base = f'http://127.0.0.1:{port}'
            for _ in range(150):
                if proc.poll() is not None:
                    raise RuntimeError('Server exited; inspect artifacts/http/server.log')
                try:
                    with opener.open(base + '/health', timeout=1) as response:
                        if response.status == 200:
                            print('Loopback server:', base, flush=True)
                            return proc, base
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            proc.terminate()
            proc.wait(timeout=10)
            raise RuntimeError('Server startup timed out')
        def stop(proc):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        def call(path, payload=None, auth=True, headers=None):
            hdr = {'Content-Type': 'application/json'}
            if auth:
                hdr['Authorization'] = 'Bearer ' + token
            hdr.update(headers or {})
            req = urllib.request.Request(base + path, data=None if payload is None else json.dumps(payload).encode(), headers=hdr)
            try:
                with opener.open(req, timeout=20) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        def rows():
            with sqlite3.connect(db) as connection:
                return connection.execute('SELECT user_id,kind,content FROM items ORDER BY id').fetchall()
        payload = {'request_id': 'repro-request', 'user_id': 'repro-user-a', 'session_id': 'repro-session-a',
                   'messages': [{'role': 'user', 'content': 'My favorite fruit is mango.', 'timestamp': 1700000000000},
                                {'role': 'assistant', 'content': 'I will remember your preference.'}]}
        query = {'query': 'What fruit do I like?', 'user_id': payload['user_id'], 'top_k': 100}
        try:
            server, base = start()
            check(call('/health', auth=False) == (200, {'status': 'ok'}), 'anonymous health liveness')
            for path, body in [('/add', payload), ('/search', query)]:
                check(call(path, body, auth=False)[0] == 401, path + ' rejects missing authentication')
                check(call(path, body, headers={'Authorization': 'Bearer invalid'})[0] == 401, path + ' rejects wrong token')
            status, added = call('/add', payload)
            expected = {k: payload[k] for k in ['request_id', 'user_id', 'session_id']}
            check(status == 200 and added == dict(success=True, **expected), 'Add response schema and exact ID echo')
            stored = rows()
            check(len(stored) == 3 and {r[1] for r in stored} == {'raw', 'fact'}, 'raw + fake extracted fact committed before 200')
            status, found = call('/search', query)
            data = found.get('data', [])
            check(status == 200 and data and any('mango' in x['content'] for x in data), 'immediate Search after Add')
            check(all(isinstance(x['id'], str) and isinstance(x['content'], str) and isinstance(x['score'], (int,float)) and ('created_at' not in x or isinstance(x['created_at'], str)) for x in data), 'Search data[] schema')
            check(len({x['id'] for x in data}) == len(data), 'unique returned memory IDs')
            for k in [0, 1, 2, 100]:
                status, result = call('/search', dict(query, top_k=k))
                check(status == 200 and len(result['data']) <= k, f'top_k={k} respected')
            for header in [{'Authorization': 'Token ' + token}, {'X-API-Key': token}]:
                check(call('/search', query, auth=False, headers=header)[0] == 200, 'documented alternate authentication scheme')
            check(call('/add', payload)[0] == 200 and rows() == stored, 'idempotent Add leaves rows unchanged')
            check(call('/search', dict(query, user_id='unknown')) == (200, {'data': []}), 'unknown user isolation')
            other = dict(payload, user_id='repro-user-b', messages=[{'role': 'user', 'content': 'My private code is violet.'}])
            check(call('/add', other)[0] == 200, 'same request_id for another user is independent')
            check(all('mango' not in x['content'] for x in call('/search', dict(query, user_id='repro-user-b'))[1]['data']), 'populated user B cannot retrieve A')
            check(all('violet' not in x['content'] for x in call('/search', query)[1]['data']), 'populated user A cannot retrieve B')
            session2 = dict(payload, request_id='second-session', session_id='repro-session-b',
                            messages=[{'role': 'user', 'content': 'My bicycle is teal.'}])
            check(call('/add', session2)[0] == 200 and any('teal' in x['content'] for x in call('/search', query)[1]['data']), 'same-user cross-session retrieval')
            for field in ['request_id', 'messages', 'user_id', 'session_id']:
                missing = {k: v for k, v in payload.items() if k != field}
                check(call('/add', missing)[0] == 422, 'Add requires ' + field)
            check(call('/add', dict(payload, messages=[]))[0] == 422, 'empty messages rejected')
            before_invalid = rows()
            for message in [{'role': 'user', 'content': ''}, {'role': 'user', 'content': '   '},
                            {'role': 'system', 'content': 'invalid textual role'}]:
                check(call('/add', dict(payload, request_id='invalid-input', messages=[message]))[0] == 422,
                      'Textual role/nonblank content validation')
            check(rows() == before_invalid, 'invalid messages do not persist')
            for field in ['query', 'user_id', 'top_k']:
                check(call('/search', {k:v for k,v in query.items() if k != field})[0] == 422, 'Search requires ' + field)
            # Run upstream's script against ONLY our fake service, never an arbitrary URL.
            smoke_env = dict(env, AMI_AUTH_TOKEN=token, AMI_AUTH_SCHEME='bearer')
            smoke = subprocess.run([sys.executable, 'scripts/smoke_contract.py', base], cwd=ROOT,
                                   env=smoke_env, capture_output=True, text=True, timeout=60)
            (artifacts / 'upstream-local-contract.txt').write_text(smoke.stdout + smoke.stderr)
            check(smoke.returncode == 0, 'upstream local contract script (not AML Smoke)')
            before_restart = rows()
            stop(server)
            server = None
            server, base = start()
            check(any('mango' in x['content'] for x in call('/search', query)[1]['data']), 'retrieval persists across process restart')
            check(call('/add', payload)[0] == 200 and rows() == before_restart, 'idempotency persists across process restart')
        finally:
            if server is not None:
                stop(server)
            (artifacts / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
