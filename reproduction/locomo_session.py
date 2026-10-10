"""One-session LoCoMo smoke test: AMI Add/Search, then a Qwen reader.

Default mode only inspects local data. Model calls require --execute and an
explicit configuration file. Gold answers are used only after predictions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import secrets
import threading

from .serve import ROOT, clear_model_environment, install_fakes, read_profile

DATASET = Path('/home/fengruizhe/Agent_Memory/datasets/LoCoMo_refined')


def load_records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def select_session(dataset, sample_id=None, session_index=1, question_limit=2):
    conversations = load_records(dataset / 'data/public/conversations.jsonl')
    conversation = next((c for c in conversations if sample_id is None or
                         c['sample_id'] == sample_id), None)
    if conversation is None:
        raise ValueError('Unknown sample_id')
    session = next((s for s in conversation['sessions'] if
                    s['session_index'] == session_index), None)
    if session is None:
        raise ValueError('Unknown session_index')
    messages = [m for m in session['messages'] if m['text'].strip()]
    ids = {m['dia_id'] for m in messages}
    questions = [q for q in load_records(dataset / 'data/public/questions.jsonl')
                 if q['sample_id'] == conversation['sample_id']
                 and not q.get('is_multi_modality') and q.get('evidence')
                 and set(q['evidence']) <= ids][:question_limit]
    if not questions:
        raise ValueError('No text-only questions fully supported by this session')
    return conversation, session, messages, questions


class CallBudget:
    """Reserve conservative token costs before each request; no automatic retry.

    Prices must be the user's provider rates (CNY per million tokens), including
    any applicable context tier. UTF-8 bytes plus overhead bound input estimates
    for the standard Qwen tokenizer. This is a local estimate, not a billing cap.
    """
    def __init__(self, profile, maximum, *, limits=None, on_change=None):
        self.maximum = maximum
        self.lock = threading.RLock()
        self.rows = []
        self.reserved = 0.0
        self.failed = None
        self.limits = limits or {'llm': 10, 'embedding': 12}
        self.on_change = on_change
        self.peak_reserved = 0.0
        self.rates = {}
        for kind, names in {
            'llm': ('AMI_TEST_INPUT_CNY_PER_MILLION', 'AMI_TEST_OUTPUT_CNY_PER_MILLION'),
            'embedding': ('AMI_TEST_EMBED_CNY_PER_MILLION',),
        }.items():
            values = [float(profile.get(name, 'nan')) for name in names]
            if any(not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError('Set positive provider prices: ' + ', '.join(names))
            self.rates[kind] = values
        if 'AMI_TEST_JUDGE_INPUT_CNY_PER_MILLION' in profile:
            values = [float(profile.get(name, 'nan')) for name in
                      ['AMI_TEST_JUDGE_INPUT_CNY_PER_MILLION', 'AMI_TEST_JUDGE_OUTPUT_CNY_PER_MILLION']]
            if any(not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError('Set positive judge provider prices')
            self.rates['judge'] = values

    def check(self):
        if self.failed:
            self.notify()
            raise RuntimeError(self.failed)

    def notify(self):
        if self.on_change is not None:
            self.on_change()

    def cost(self, kind, input_tokens, output_tokens=0):
        rates = self.rates[kind]
        return (input_tokens * rates[0] +
                (output_tokens * rates[1] if len(rates) > 1 else 0)) / 1_000_000


def make_transport(budget, kind, endpoint, model):
    import httpx

    class GuardedTransport(httpx.BaseTransport):
        def __init__(self):
            self.client = httpx.Client(timeout=30, follow_redirects=False)

        def close(self):
            self.client.close()

        def handle_request(self, request):
            with budget.lock:
                budget.check()
                payload = json.loads(request.content)
                if request.method != 'POST' or str(request.url) != endpoint or payload.get('model') != model:
                    budget.failed = 'Unexpected provider endpoint or model'
                    budget.check()
                if request.headers.get('x-stainless-retry-count', '0') != '0':
                    budget.failed = 'Retry forbidden'
                    budget.check()
                count = sum(row['kind'] == kind for row in budget.rows)
                if count >= budget.limits[kind]:
                    budget.failed = 'Provider request limit reached'
                    budget.check()
                if kind in {'llm', 'judge'}:
                    if payload.get('enable_thinking') is not False or payload.get('stream'):
                        budget.failed = 'Smoke test requires nonthinking, nonstreaming calls'
                        budget.check()
                    messages = payload['messages']
                    input_bound = sum(len(m['content'].encode('utf-8')) + 256 for m in messages) + 256
                    output_bound = payload['max_tokens']
                else:
                    input_bound = sum(len(text.encode('utf-8')) + 256 for text in payload['input'])
                    output_bound = 0
                reservation = budget.cost(kind, input_bound, output_bound)
                if budget.reserved + reservation > budget.maximum:
                    budget.failed = 'Estimated CNY budget would be exceeded; stopped before request'
                    budget.check()
                budget.reserved += reservation
                budget.peak_reserved = max(budget.peak_reserved, budget.reserved)
                row = {'kind': kind, 'input_token_bound': input_bound,
                       'output_token_bound': output_bound, 'reserved_cny': reservation}
                budget.rows.append(row)
                budget.notify()
                response = None
                try:
                    response = self.client.send(request, stream=False, follow_redirects=False)
                    response.read()
                    row['http_status'] = response.status_code
                    if response.status_code != 200:
                        try:
                            error = response.json().get('error', {})
                            code = error.get('code') if isinstance(error, dict) else None
                        except (ValueError, AttributeError):
                            code = None
                        # Retain known error codes, never arbitrary provider bodies.
                        safe_codes = {'model_not_found', 'ModelNotFound', 'invalid_api_key',
                            'InvalidApiKey', 'rate_limit_exceeded', 'insufficient_quota',
                            'WorkSpaceNotFound', 'model_not_supported', 'NotFound',
                            'RequestPath.NotFound', 'invalid_parameter', 'InvalidParameter'}
                        if isinstance(code, str) and code in safe_codes:
                            row['provider_error_code'] = code
                        budget.failed = f"{kind} HTTP {response.status_code}" + (
                            f" ({code})" if row.get('provider_error_code') else '')
                        raise RuntimeError('Provider returned non-200 status')
                    body = response.json()
                    if body.get('error'):
                        raise ValueError('Provider error response')
                    if kind in {'llm', 'judge'} and body.get('model') != model:
                        raise ValueError('Qwen response model does not match selected model')
                    usage = body['usage']
                    input_tokens = usage['prompt_tokens']
                    output_tokens = usage.get('completion_tokens', 0)
                    if any(type(v) is not int or v < 0 for v in [input_tokens, output_tokens]):
                        raise ValueError('Invalid token usage')
                    row['input_tokens'], row['output_tokens'] = input_tokens, output_tokens
                    row['estimated_cny'] = budget.cost(kind, input_tokens, output_tokens)
                    if input_tokens > input_bound or output_tokens > output_bound:
                        raise ValueError('Provider usage exceeds reserved bounds')
                    if kind in {'llm', 'judge'} and not body['choices'][0]['message']['content'].strip():
                        raise ValueError('Empty completion')
                except Exception:
                    if response is not None:
                        response.close()
                    if budget.failed is None:
                        budget.failed = 'Provider failure; no further requests allowed'
                    budget.check()
                # Keep actual completed costs, plus any in-flight reservation,
                # rather than charging every conservative estimate forever.
                budget.reserved += row['estimated_cny'] - reservation
                budget.notify()
                return response

    return GuardedTransport()


class QwenCompletions:
    def __init__(self, completions):
        self.completions = completions

    def create(self, **kwargs):
        kwargs['extra_body'] = {**kwargs.get('extra_body', {}), 'enable_thinking': False}
        return self.completions.create(**kwargs)


def run(args, output, conversation, session, messages, questions):
    profile = read_profile(args.env_file) if args.execute else read_profile(ROOT / '.env.example')
    if args.execute:
        for key in ['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'AMI_LLM_MODEL', 'AMI_EMBED_BASE_URL']:
            if not profile.get(key, '').strip():
                raise ValueError('Missing configuration: ' + key)
        profile['AMI_EMBED_API_KEY'] = profile.get('AMI_EMBED_API_KEY') or profile.get('DASHSCOPE_API_KEY') or profile['OPENAI_API_KEY']
        if 'qwen' not in profile['AMI_LLM_MODEL'].lower():
            raise ValueError('This entry point expects an explicitly selected Qwen model')
        from urllib.parse import urlsplit
        for key in ['OPENAI_BASE_URL', 'AMI_EMBED_BASE_URL']:
            url = urlsplit(profile[key])
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or any(c in profile[key] for c in '{}<>'):
                raise ValueError('Set an actual HTTPS base URL: ' + key)
    budget = CallBudget(profile, args.max_cny) if args.execute else None
    # Preserve the release algorithm switches, but isolate storage and diagnostics.
    settings = read_profile(ROOT / '.env.example')
    settings.update(profile)
    settings.update(AMI_DB_PATH=str(output / 'memory.sqlite3'), AMI_REQUEST_LOG='',
                    AMI_AUTH_TOKEN=secrets.token_urlsafe(32), AMI_AUTH_SCHEME='bearer',
                    AMI_EMBED_BACKEND='openai', AMI_REPRO_EMBED_PROTOCOL='openai',
                    AMI_LLM_RETRIES='0', AMI_EMBED_RETRIES='0', AMI_LLM_DISABLE_THINKING='0')
    if args.fake:
        settings.update(OPENAI_API_KEY='offline-fixture', AMI_EMBED_API_KEY='offline-fixture',
                        OPENAI_BASE_URL='http://offline.invalid/v1', AMI_EMBED_BASE_URL='http://offline.invalid/v1')
    # Refuse reusing a database or replaying a completed/failed paid attempt.
    fd = os.open(output / 'attempt.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    if (output / 'memory.sqlite3').exists():
        raise ValueError('Use a fresh output directory')
    clear_model_environment()
    os.environ.update(settings)
    logging.disable(logging.CRITICAL)
    if args.fake:
        install_fakes()
    import httpx
    from types import SimpleNamespace
    from openai import OpenAI
    from fastapi.testclient import TestClient
    from app import config, embed, llm, main as api, store
    clients = []
    report = {'mode': 'real-qwen' if args.execute else 'offline-fake',
              'sample_id': conversation['sample_id'], 'session_index': session['session_index'],
              'messages': len(messages), 'llm_model': config.LLM_MODEL,
              'embedding_model': config.EMBED_MODEL, 'max_cny': args.max_cny,
              'date_timezone_assumption': 'UTC (dataset does not specify timezone)',
              'tests_http_routes_in_process': True, 'results': [], 'success': False}
    try:
        if args.execute:
            for kind, base, key, model, suffix in [
                ('llm', config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL, '/chat/completions'),
                ('embedding', config.EMBED_BASE_URL, config.EMBED_API_KEY, config.EMBED_MODEL, '/embeddings'),
            ]:
                client = OpenAI(api_key=key, base_url=base, max_retries=0, timeout=30,
                                http_client=httpx.Client(transport=make_transport(budget, kind, base.rstrip('/') + suffix, model)))
                clients.append(client)
            completions = QwenCompletions(clients[0].chat.completions)
            llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            embed._client, embed._client_settings = clients[1], (
                config.EMBED_API_KEY, embed._remote_config(), config.EMBED_TIMEOUT,
                config.EMBED_RETRIES, config.EMBED_CONCURRENCY)
            embed._remote_gate = threading.BoundedSemaphore(config.EMBED_CONCURRENCY)
        else:
            completions = None
        when = dt.datetime.strptime(session['date_time'], '%I:%M %p on %d %B, %Y').replace(tzinfo=dt.timezone.utc)
        user_id = 'locomo-smoke-' + conversation['sample_id']
        request_id = f"{conversation['sample_id']}-session-{session['session_index']}"
        body = {'user_id': user_id, 'session_id': request_id, 'request_id': request_id,
                'messages': [{'role': m['role'], 'content': f"{m['speaker']}: {m['text']}",
                              'timestamp': int(when.timestamp() * 1000)} for m in messages]}
        metrics_spec = importlib.util.spec_from_file_location('locomo_lexical', args.dataset / 'src/bleu_f1.py')
        metrics = importlib.util.module_from_spec(metrics_spec)
        metrics_spec.loader.exec_module(metrics)
        predictions = []
        with TestClient(api.app) as service:
            headers = {'Authorization': 'Bearer ' + config.AUTH_TOKEN}
            response = service.post('/add', json=body, headers=headers)
            report['add_http_status'] = response.status_code
            if budget:
                budget.check()
            if response.status_code != 200 or not response.json().get('success'):
                raise RuntimeError('Add failed')
            index = store.get(user_id)
            report['stored_raw'] = sum(item.kind == 'raw' for item in index.items)
            report['stored_facts'] = sum(item.kind == 'fact' for item in index.items)
            report['vector_shape'] = list(index.matrix.shape)
            if report['stored_raw'] != len(messages) or report['stored_facts'] == 0:
                raise RuntimeError('Expected raw messages and usable extracted facts')
            for question in questions:
                response = service.post('/search', json={'user_id': user_id,
                    'query': question['question'], 'top_k': args.top_k}, headers=headers)
                if budget:
                    budget.check()
                if response.status_code != 200 or not response.json().get('data'):
                    raise RuntimeError('Search failed or returned no memories')
                memories = response.json()['data']
                if len(memories) > args.top_k:
                    raise RuntimeError('Search exceeded top_k')
                (output / (question['qa_id'].replace('#', '_') + '_retrieval.json')).write_text(json.dumps(memories, ensure_ascii=False, indent=2))
                context = '\n'.join(item['content'] for item in memories)
                if args.execute:
                    answer = completions.create(model=config.LLM_MODEL, temperature=0, max_tokens=256,
                        messages=[{'role': 'system', 'content':
                            f"Answer using only retrieved memories. The memory owner is {conversation['speaker_a']}; "
                            f"the other human speaker is {conversation['speaker_b']}. Named speaker prefixes identify who spoke. "
                            "Use session dates to resolve relative dates. Return only a concise answer. "
                            "If evidence is insufficient, answer 'Unknown'."},
                            {'role': 'user', 'content': f"Memories:\n{context}\n\nQuestion: {question['question']}"}]).choices[0].message.content
                    budget.check()
                else:
                    answer = 'Offline fixture; no real answer generated'
                gold = question['answer']
                evidence_ids = {store.item_id(request_id, 'raw', i, user_id)
                                for i, m in enumerate(messages) if m['dia_id'] in question['evidence']}
                found = evidence_ids & {item['id'] for item in memories}
                report['results'].append({'qa_id': question['qa_id'], 'question': question['question'],
                    'answer': answer, 'references': gold, 'returned': len(memories),
                    'raw_evidence_recall': len(found) / len(evidence_ids),
                    **({'token_f1': max(metrics.compute_token_f1(answer, ref) for ref in gold),
                        'bleu1': max(metrics.compute_bleu1(answer, ref) for ref in gold)} if args.execute else {})})
                predictions.append({'qa_id': question['qa_id'], 'predicted_answer': answer})
        (output / 'predictions.jsonl').write_text(''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in predictions))
        report['success'] = True
        print(f"{'REAL' if args.execute else 'FAKE'} PASS: Add + {len(questions)} Search/reader cases; output={output}")
    except Exception as exc:
        # Do not print provider exceptions, request headers, or config values.
        report['failure_type'] = type(exc).__name__
        raise RuntimeError('Session smoke test stopped; inspect report.json') from None
    finally:
        if budget:
            report.update(provider_calls=budget.rows, reserved_cny=budget.reserved,
                          estimated_cny=sum(row.get('estimated_cny', 0) for row in budget.rows),
                          stopped=budget.failed)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        for client in clients:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DATASET)
    parser.add_argument('--sample-id')
    parser.add_argument('--session-index', type=int, default=1)
    parser.add_argument('--questions', type=int, default=2)
    parser.add_argument('--top-k', type=int, default=10)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--max-cny', type=float, default=1.0)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--execute', action='store_true', help='Make authorized paid API calls')
    modes.add_argument('--fake', action='store_true', help='Run full routes with offline SDK fixtures')
    args = parser.parse_args()
    if not 1 <= args.questions <= 2 or not 1 <= args.top_k <= 100:
        parser.error('This smoke entry point supports 1-2 questions and top_k 1-100')
    if not math.isfinite(args.max_cny) or not 0 < args.max_cny <= 1:
        parser.error('Budget must be positive and no greater than the authorized CNY 1')
    if args.execute and args.env_file is None:
        parser.error('--execute requires an explicit --env-file')
    output = (args.output or ROOT / 'artifacts' / ('locomo-qwen-once' if args.execute else
               'locomo-fake' if args.fake else 'locomo-plan')).resolve()
    output.mkdir(parents=True, exist_ok=True)
    conversation, session, messages, questions = select_session(args.dataset, args.sample_id, args.session_index, args.questions)
    plan = {'sample_id': conversation['sample_id'], 'session_index': session['session_index'],
            'date_time': session['date_time'], 'messages': len(messages), 'top_k': args.top_k,
            'questions': [{k: q[k] for k in ['qa_id', 'question', 'evidence']} for q in questions],
            'selection': 'text-only; all annotated evidence belongs to selected session',
            'gold_is_sent_to_model': False}
    (output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute and not args.fake:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    run(args, output, conversation, session, messages, questions)


if __name__ == '__main__':
    main()
