"""One authorized synthetic Add/Search run. Not a benchmark or a reusable sweep.

Provider attempts are globally serialized, counted before I/O, and latched off
on the first error. Retries and redirects are disabled. Only allowlisted numeric
metadata goes to the report; provider bodies, keys and auth headers never do.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

import httpx

from .serve import ROOT, clear_model_environment, read_profile


# Exact documented alias/snapshot mapping; never accept an arbitrary prefix.
# https://developers.openai.com/api/docs/models/gpt-4o-mini (Snapshots)
ALLOWED_RESPONSE_MODELS = {
    'gpt-4o-mini': frozenset({'gpt-4o-mini', 'gpt-4o-mini-2024-07-18'}),
    'text-embedding-v4': frozenset({'text-embedding-v4'}),
}


class StopRun(RuntimeError):
    pass


class Budget:
    def __init__(self, report_path=None, *, prior=None):
        self.lock = threading.RLock()
        self.path = report_path
        prior = prior or {'llm': 0, 'embedding': 0}
        if any(type(prior[k]) is not int or not 0 <= prior[k] <= 5 for k in ['llm', 'embedding']):
            raise ValueError('Invalid historical request counts')
        self.report = {'prior_attempts': dict(prior),
                       'limits': {k: 5-prior[k] for k in ['llm', 'embedding']},
                       'attempts': {'llm': 0, 'embedding': 0},
                       'calls': [], 'stopped': None, 'startup': False,
                       'add_http_status': None, 'search_http_status': None,
                       'add_success': False, 'search_success': False, 'checks': {}}

    def save(self):
        self.report['cumulative_attempts'] = {k: self.report['prior_attempts'][k] + self.report['attempts'][k]
                                              for k in ['llm', 'embedding']}
        if self.path:
            # Contains only fixed labels, counts, booleans and token usage.
            self.path.write_text(json.dumps(self.report, indent=2) + '\n')

    def fail(self, reason):
        if self.report['stopped'] is None:
            self.report['stopped'] = reason
        self.save()
        raise StopRun('Minimal integration stopped; inspect sanitized report')

    def check(self):
        if self.report['stopped'] is not None:
            raise StopRun('Minimal integration already stopped')


class CappedTransport(httpx.BaseTransport):
    def __init__(self, budget, kind, expected_url, model, *, client=None):
        self.budget, self.kind = budget, kind
        self.expected_url, self.model = expected_url, model
        # HTTPX defaults to zero retries; normal client preserves configured proxy support.
        self.client = client or httpx.Client(timeout=25, follow_redirects=False)

    def close(self):
        self.client.close()

    def handle_request(self, request):
        b = self.budget
        with b.lock:
            b.check()
            if request.method != 'POST' or str(request.url) != self.expected_url:
                b.fail('unexpected_provider_endpoint_or_method')
            try:
                payload = json.loads(request.content)
            except Exception:
                b.fail('invalid_provider_request')
            if payload.get('model') != self.model:
                b.fail('request_model_mismatch')
            if request.headers.get('x-stainless-retry-count', '0') != '0':
                b.fail('unexpected_sdk_retry')
            if b.report['attempts'][self.kind] >= b.report['limits'][self.kind]:
                b.fail(self.kind + '_budget_exhausted')
            b.report['attempts'][self.kind] += 1
            row = {'kind': self.kind, 'ordinal': b.report['attempts'][self.kind], 'http_status': None}
            b.report['calls'].append(row)
            b.save()
            try:
                response = self.client.send(request, stream=False, follow_redirects=False)
                response.read()
            except Exception:
                b.fail(self.kind + '_network_error')
            row['http_status'] = response.status_code
            if response.status_code != 200:
                response.close()
                b.fail(self.kind + '_http_' + str(row['http_status']))
            try:
                body = response.json()
                if not isinstance(body, dict) or body.get('error'):
                    raise ValueError()
            except Exception:
                response.close()
                b.fail(self.kind + '_invalid_response')
            usage = body.get('usage')
            if isinstance(usage, dict):
                row['usage'] = {k: usage[k] for k in ['prompt_tokens', 'completion_tokens', 'total_tokens']
                                if type(usage.get(k)) is int and usage[k] >= 0}
            # Requests stay gpt-4o-mini; accept only its documented snapshot response.
            returned_model = body.get('model')
            if not isinstance(returned_model, str) or returned_model not in ALLOWED_RESPONSE_MODELS[self.model]:
                row['model_matches'] = False
                response.close()
                b.fail(self.kind + '_response_model_mismatch_or_missing')
            row['model_matches'] = True
            row['returned_model'] = returned_model  # Explicit allowlist, never arbitrary provider text.
            try:
                if self.kind == 'llm':
                    content = body['choices'][0]['message']['content']
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError()
                else:
                    data = body['data']
                    if not isinstance(data, list) or len(data) != len(payload['input']):
                        raise ValueError()
                    indices = [item['index'] for item in data]
                    if any(type(i) is not int for i in indices) or sorted(indices) != list(range(len(data))):
                        raise ValueError()
                    for item in data:
                        vector = item['embedding']
                        if len(vector) != payload['dimensions'] or not all(isinstance(v, (int,float)) and math.isfinite(v) for v in vector) or not any(vector):
                            raise ValueError()
            except Exception:
                response.close()
                b.fail(self.kind + '_invalid_response_shape')
            usage = body.get('usage')
            if isinstance(usage, dict):
                row['usage'] = {k: usage[k] for k in ['prompt_tokens', 'completion_tokens', 'total_tokens']
                                if type(usage.get(k)) is int and usage[k] >= 0}
            b.save()
            return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute-authorized-once', action='store_true')
    parser.add_argument('--authorized-completion', action='store_true', help='Separately authorized completion after snapshot diagnosis')
    args = parser.parse_args()
    if not args.execute_authorized_once:
        parser.error('Explicit one-shot execution flag required')
    os.chdir(ROOT)
    logging.disable(logging.CRITICAL)
    profile = read_profile(ROOT / '.env')
    expected = {'AMI_LLM_MODEL': 'gpt-4o-mini', 'AMI_EMBED_MODEL': 'text-embedding-v4',
                'AMI_EMBED_BACKEND': 'openai', 'AMI_REPRO_EMBED_PROTOCOL': 'openai',
                'AMI_EMBED_DIMENSIONS': '1024', 'AMI_LLM_RETRIES': '0', 'AMI_EMBED_RETRIES': '0'}
    if any(profile.get(k) != v for k, v in expected.items()):
        raise SystemExit('Configuration mismatch; no provider calls made')
    if not all(profile.get(k, '').strip() for k in ['OPENAI_API_KEY', 'AMI_AUTH_TOKEN']):
        raise SystemExit('Missing credentials; no provider calls made')
    if not (profile.get('AMI_EMBED_API_KEY') or profile.get('DASHSCOPE_API_KEY')):
        raise SystemExit('Missing embedding credential; no provider calls made')
    for key in ['OPENAI_BASE_URL', 'AMI_EMBED_BASE_URL']:
        value = profile.get(key, '')
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or any(c in value for c in '{}<>')):
            raise SystemExit('Invalid provider URL; no provider calls made')
    prior = {'llm': 0, 'embedding': 0}
    if args.authorized_completion:
        previous = json.loads((ROOT / 'artifacts/real-minimal-once/report.json').read_text())
        diagnostic = json.loads((ROOT / 'artifacts/model-diagnostic-once/report.json').read_text())
        prior = {k: previous['attempts'][k] + diagnostic[k+'_attempts'] for k in prior}
        if prior['llm'] >= 5 or prior['embedding'] >= 5:
            raise SystemExit('Historical budget exhausted; no API requests made')
    output = ROOT / 'artifacts' / ('real-minimal-completion-once' if args.authorized_completion else 'real-minimal-once')
    output.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(output / 'attempt.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        raise SystemExit('One-shot attempt already recorded; refuse automatic rerun')
    b = Budget(output / 'report.json', prior=prior)
    b.save()
    clear_model_environment()
    os.environ.update(profile)
    os.environ['AMI_DB_PATH'] = str(output / 'synthetic.sqlite3')
    os.environ['AMI_REQUEST_LOG'] = ''
    clients = []
    try:
        from openai import OpenAI
        from app import config, embed, llm, main as api, store
        llm_http = httpx.Client(transport=CappedTransport(b, 'llm',
            profile['OPENAI_BASE_URL'].rstrip('/') + '/chat/completions', 'gpt-4o-mini'), follow_redirects=False)
        embed_http = httpx.Client(transport=CappedTransport(b, 'embedding',
            profile['AMI_EMBED_BASE_URL'].rstrip('/') + '/embeddings', 'text-embedding-v4'), follow_redirects=False)
        llm_client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL,
                            max_retries=0, timeout=config.LLM_TIMEOUT, http_client=llm_http)
        embed_client = OpenAI(api_key=config.EMBED_API_KEY, base_url=config.EMBED_BASE_URL,
                              max_retries=0, timeout=config.EMBED_TIMEOUT, http_client=embed_http)
        clients.extend([llm_client, embed_client])
        # Replace only SDK client construction; all prompts and algorithm code remain active.
        llm._get_client = lambda: llm_client
        gate = threading.BoundedSemaphore(config.EMBED_CONCURRENCY)
        embed._get_remote_client = lambda: (embed_client, gate)
        original_store_add = store.add
        def guarded_store_add(*a, **kw):
            with b.lock:
                b.check()
                return original_store_add(*a, **kw)
        store.add = guarded_store_add
        # ASGI client runs the real startup and POST routes, using real provider SDK calls.
        # The earlier baseline already validated loopback network serving separately.
        from fastapi.testclient import TestClient
        with TestClient(api.app, raise_server_exceptions=False) as client:
            b.check()
            b.report['startup'] = True
            headers = {'Authorization': 'Bearer ' + config.AUTH_TOKEN}
            payload = {'request_id':'synthetic-minimal-request-1', 'user_id':'synthetic-minimal-user-1',
                       'session_id':'synthetic-minimal-session-1', 'messages':[
                           {'role':'user', 'content':'My favorite fruit is mango.', 'timestamp':1704067200000}]}
            response = client.post('/add', json=payload, headers=headers)
            b.report['add_http_status'] = response.status_code
            b.check()
            if response.status_code != 200:
                b.fail('add_http_failure')
            expected_add = {k: payload[k] for k in ['request_id','user_id','session_id']}
            if response.json() != dict(success=True, **expected_add):
                b.fail('add_contract_failure')
            rows = store._conn.execute('SELECT kind,content FROM items WHERE user_id=?', (payload['user_id'],)).fetchall()
            if not any(r[0]=='raw' for r in rows) or not any(r[0]=='fact' for r in rows):
                b.fail('raw_or_fact_missing')
            b.report['checks']['persisted_raw_and_fact'] = True
            b.report['checks']['add_id_echo'] = True
            b.report['add_success'] = True
            b.save()
            response = client.post('/search', headers=headers, json={
                'query':'What is my favorite fruit?', 'user_id':payload['user_id'], 'top_k':100})
            b.report['search_http_status'] = response.status_code
            b.check()
            if response.status_code != 200:
                b.fail('search_http_failure')
            data = response.json().get('data')
            if (not isinstance(data, list) or not 0 < len(data) <= 100 or
                    not all(isinstance(x.get('id'),str) and x['id'] and isinstance(x.get('content'),str) and x['content'] for x in data)):
                b.fail('search_contract_failure')
            if not any('mango' in x['content'].lower() for x in data):
                b.fail('synthetic_evidence_missing')
            if llm.counters['failures'] or llm.counters['empty_extractions']:
                b.fail('llm_degradation_detected')
            b.report['checks'].update(immediate_retrieval=True, top_k_bound=True, search_schema=True)
            b.report['returned_count'] = len(data)
            b.report['search_success'] = True
    except Exception:
        if b.report['stopped'] is None:
            b.report['stopped'] = 'local_runtime_or_response_validation_error'
    finally:
        for client in clients:
            client.close()
        b.save()
        print(json.dumps(b.report, indent=2))
    return int(b.report['stopped'] is not None)


if __name__ == '__main__':
    raise SystemExit(main())
