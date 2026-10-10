"""Complete conv-26 build/retrieve/answer test; offline inspection by default.

Uses the same conversation and exported questions as LightMem, the LightMem
concise reader prompt, and the dataset's Refined Qwen14B scorer. AMI retrieval remains
native. Paid execution requires an explicit configuration and fresh directory.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from collections import Counter
import datetime as dt
import hashlib
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time

from .locomo_session import DATASET, CallBudget, QwenCompletions, load_records, make_transport
from .serve import ROOT, clear_model_environment, install_fakes, read_profile


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def load_conversation(dataset, sample_id):
    conversation = next((c for c in load_records(dataset / 'data/public/conversations.jsonl')
                         if c['sample_id'] == sample_id), None)
    if conversation is None:
        raise ValueError('Unknown conversation')
    questions = [q for q in load_records(dataset / 'data/public/questions.jsonl')
                 if q['sample_id'] == sample_id]
    if not questions:
        raise ValueError('Conversation has no questions')
    # Do not filter by evidence, category, or modality: preserve the full QA set.
    return conversation, questions


def lightmem_reader(workspace):
    prompt_path = workspace / 'LightMem/experiments/locomo/prompts.py'
    evaluator_path = workspace / 'memma/backends/lightmem/evaluator.py'
    template = None
    for node in ast.walk(ast.parse(prompt_path.read_text())):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ANSWER_PROMPT' for t in node.targets):
            template = ast.literal_eval(node.value)
            break
    if not isinstance(template, str):
        raise ValueError('LightMem reader template missing')
    formatter = next(node for node in ast.parse(evaluator_path.read_text()).body
                     if isinstance(node, ast.FunctionDef) and node.name == '_format_final_answer_prompt')
    # Reuse only the pure formatting function, without importing LightMem/GPU deps.
    namespace = {'ANSWER_PROMPT': template}
    exec(compile(ast.Module(body=[formatter], type_ignores=[]), str(evaluator_path), 'exec'), namespace)
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [prompt_path, evaluator_path]}
    return namespace['_format_final_answer_prompt'], hashes


def message_text(message):
    # Match memma.common.locomo.extract_locomo_sessions's textual image handling.
    text = message['text']
    if message.get('blip_caption'):
        text += f" (image description: {message['blip_caption']})"
    return f"[{message['dia_id']}] {message['speaker']}: {text}"


def make_add(conversation, session, user_id):
    when = dt.datetime.strptime(session['date_time'], '%I:%M %p on %d %B, %Y').replace(tzinfo=dt.timezone.utc)
    request_id = f"{conversation['sample_id']}-session-{session['session_index']}"
    return {'user_id': user_id, 'session_id': request_id, 'request_id': request_id,
            'messages': [{'role': m['role'], 'content': message_text(m),
                          'timestamp': int(when.timestamp() * 1000)} for m in session['messages']]}


def reader_context(memories, conversation, raw_speakers):
    a, b = conversation['speaker_a'], conversation['speaker_b']
    grouped = {a: [], b: [], 'shared': []}
    for memory in memories:
        line = memory['content']
        speaker = raw_speakers.get(memory['id'])
        if speaker is None:
            named = [name for name in (a, b) if re.search(r'\b' + re.escape(name) + r'\b', line, re.I)]
            if len(named) == 1:
                speaker = named[0]
            elif not named and re.match(r'^(?:\[[^\]]+\]\s*)?(?:I\b|My\b)', line, re.I):
                speaker = a
        grouped[speaker if speaker in (a, b) else 'shared'].append(line)
    shared = '\n'.join(grouped['shared'])
    fields = {'speaker_1_name': a, 'speaker_1_memories': '\n'.join(grouped[a]),
              'speaker_2_name': b, 'speaker_2_memories': '\n'.join(grouped[b])}
    if shared:
        # Explicitly mark uncertain attribution; never silently assign a fact.
        for key in ['speaker_1_memories', 'speaker_2_memories']:
            fields[key] += '\nShared or unattributed evidence (follow names in the text):\n' + shared
    return fields


def score_official(dataset, output, completions, report, checkpoint, budget):
    src = str(dataset / 'src')
    sys.path.insert(0, src)
    try:
        import evaluate
        import summarize
        import llm_judge
        import llm_judge_runtime as runtime
        from tenacity import stop_after_attempt
        from types import SimpleNamespace
        original_retry = runtime.AsyncRetrying
        # Keep the official scoring/JSON parser; suppress transport retries only.
        runtime.AsyncRetrying = lambda **kwargs: original_retry(**{**kwargs, 'stop': stop_after_attempt(1)})
        judge = runtime.AsyncBinaryJudge(prompt_template=llm_judge.JUDGE_PROMPTS['refined'],
            default_model='qwen3-14b', default_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
            default_api_key=None)

        class AsyncCompletions:
            async def create(self, **kwargs):
                kwargs['max_tokens'] = 128
                return await asyncio.to_thread(completions.create, **kwargs)

        judge._client = SimpleNamespace(chat=SimpleNamespace(completions=AsyncCompletions()))
        report['judge_results'] = []

        async def judge_fn(*, question, reference_answer, predicted_answer):
            budget.check()
            try:
                value = await judge.evaluate(question, reference_answer, predicted_answer)
            except Exception as exc:
                if budget.failed is None:
                    budget.failed = 'Official judge failed (' + type(exc).__name__ + '); no further provider requests allowed'
                budget.check()
            verdict = {'question': question, 'reference_answer': reference_answer,
                       'predicted_answer': predicted_answer, 'score': value}
            report['judge_results'].append(verdict)
            with (output / 'judge_verdicts.jsonl').open('a') as file:
                file.write(json.dumps(verdict, ensure_ascii=False) + '\n')
            checkpoint()
            count = len(report['judge_results'])
            if count == 1 or count % 10 == 0:
                print(f"Judge: {count} reference comparisons", flush=True)
            return {'score': float(value), 'reason': 'official_refined_qwen3-14b'}

        try:
            records = asyncio.run(evaluate.evaluate_public_predictions(
                questions_path=output / 'questions.jsonl', predictions_path=output / 'predictions.jsonl',
                output_path=output / 'predictions_scored.jsonl', metrics=('llm', 'f1', 'bleu'),
                llm_judge='refined', judge_fn=judge_fn, concurrency=1))
        finally:
            runtime.AsyncRetrying = original_retry
        summary = summarize.summarize_public_scores(records)
        save(output / 'predictions_scored_summary.json', summary)
        (output / 'predictions_scored_summary.md').write_text(summarize.render_public_score_markdown(summary))
        return summary
    finally:
        sys.path.remove(src)


def run(args, output, conversation, questions, formatter, hashes):
    profile = read_profile(args.env_file) if args.execute else read_profile(ROOT / '.env.example')
    if args.execute:
        from urllib.parse import urlsplit
        for key in ['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'AMI_LLM_MODEL', 'AMI_EMBED_API_KEY', 'AMI_EMBED_BASE_URL']:
            if not profile.get(key, '').strip():
                raise ValueError('Missing configuration field: ' + key)
        if profile['AMI_LLM_MODEL'] != 'qwen3-8b':
            raise ValueError('This authorized run is pinned to qwen3-8b')
        if profile.get('AMI_EMBED_MODEL') != 'text-embedding-v4' or profile.get('AMI_EMBED_DIMENSIONS') != '1024':
            raise ValueError('Expected text-embedding-v4, 1024 dimensions')
        for key in ['OPENAI_BASE_URL', 'AMI_EMBED_BASE_URL']:
            parts = urlsplit(profile[key])
            if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or any(c in profile[key] for c in '{}<>'):
                raise ValueError('Actual HTTPS provider URL required')
    # Beijing Qwen3-14B nonthinking prices, verified alongside Qwen8B prices.
    profile.update(AMI_TEST_JUDGE_INPUT_CNY_PER_MILLION='1', AMI_TEST_JUDGE_OUTPUT_CNY_PER_MILLION='4')
    previous = json.loads((output / 'report.json').read_text()) if args.resume else None
    if previous and (previous.get('success') or previous.get('qa')):
        raise ValueError('Resume currently supports an interrupted build with no saved answers')
    budget = CallBudget(profile, math.inf if args.no_cost_limit else args.max_cny,
                        limits={'llm': 800, 'embedding': 500, 'judge': 400}) if args.execute else None
    if args.resume:
        if not (output / 'attempt.lock').exists() or not (output / 'memory.sqlite3').exists():
            raise ValueError('Resume requires the existing attempt and database')
        if budget:
            budget.rows = previous.get('provider_calls', [])
            budget.reserved = sum(r.get('estimated_cny', r['reserved_cny']) for r in budget.rows)
            budget.peak_reserved = previous.get('peak_reserved_cny', budget.reserved)
    else:
        fd = os.open(output / 'attempt.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        if (output / 'memory.sqlite3').exists():
            raise ValueError('Refuse database reuse')
    settings = read_profile(ROOT / '.env.example')
    settings.update(profile)
    settings.update(AMI_DB_PATH=str(output / 'memory.sqlite3'), AMI_REQUEST_LOG='',
                    AMI_AUTH_TOKEN=secrets.token_urlsafe(32), AMI_AUTH_SCHEME='bearer',
                    AMI_LLM_RETRIES='0', AMI_EMBED_RETRIES='0', AMI_LLM_DISABLE_THINKING='0',
                    AMI_LLM_TIMEOUT='30', AMI_EMBED_TIMEOUT='30', AMI_REPRO_EMBED_PROTOCOL='openai')
    if args.fake:
        settings.update(OPENAI_API_KEY='offline-fixture', AMI_EMBED_API_KEY='offline-fixture',
                        OPENAI_BASE_URL='http://offline.invalid/v1', AMI_EMBED_BASE_URL='http://offline.invalid/v1')
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
    started = time.monotonic()
    report = {'mode': 'real-qwen' if args.execute else 'offline-fake',
              'sample_id': conversation['sample_id'], 'sessions_expected': len(conversation['sessions']),
              'messages_expected': sum(len(s['messages']) for s in conversation['sessions']),
              'questions_expected': len(questions), 'top_k': args.top_k,
              'llm_model': config.LLM_MODEL, 'embedding_model': config.EMBED_MODEL,
              'max_cny': None if args.no_cost_limit else args.max_cny,
              'stage': 'startup', 'build': previous.get('build', []) if previous else [], 'qa': [], 'success': False,
              'reader_prompt_sha256': hashes, 'scoring': 'official dataset Refined scorer: llm + f1 + bleu',
              'judge_model': 'qwen3-14b', 'judge_thinking': False, 'judge_max_tokens': 128,
              'date_timezone_assumption': 'UTC; dataset timezone unspecified',
              'switches': {key: getattr(config, key) for key in ['EXTRACT_ENABLED', 'RECALL_QUERY_ENABLED',
                  'UPDATE_DETECT', 'UPDATE_RENDER', 'UPDATE_WITHHOLD', 'UPDATE_VERSION', 'RAW_FIRST',
                  'WINDOW_RADIUS', 'CHUNK_MEMORY', 'AGENTIC_SEARCH', 'DCI_SEARCH', 'HOP2_SLOTS']}}
    if previous:
        report['resumed_build'] = True
        report['prior_elapsed_seconds'] = previous.get('elapsed_seconds', 0)

    def checkpoint():
        def write():
            report['elapsed_seconds'] = report.get('prior_elapsed_seconds', 0) + time.monotonic() - started
            if budget:
                report.update(provider_calls=budget.rows, estimated_cny=sum(r.get('estimated_cny', 0) for r in budget.rows),
                              outstanding_and_actual_cny=budget.reserved, peak_reserved_cny=budget.peak_reserved,
                              stopped=budget.failed)
            save(output / 'report.json', report)
        if budget:
            with budget.lock:
                write()
        else:
            write()

    if budget:
        budget.on_change = checkpoint
    try:
        if args.execute:
            for kind, base, key, model, suffix in [
                ('llm', config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL, '/chat/completions'),
                ('embedding', config.EMBED_BASE_URL, config.EMBED_API_KEY, config.EMBED_MODEL, '/embeddings'),
                ('judge', config.LLM_BASE_URL, config.LLM_API_KEY, 'qwen3-14b', '/chat/completions'),
            ]:
                clients.append(OpenAI(api_key=key, base_url=base, max_retries=0, timeout=30,
                    http_client=httpx.Client(transport=make_transport(budget, kind, base.rstrip('/') + suffix, model))))
            completions = QwenCompletions(clients[0].chat.completions)
            judge_completions = QwenCompletions(clients[2].chat.completions)
            llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            embed._client = clients[1]
            embed._client_settings = (config.EMBED_API_KEY, embed._remote_config(), config.EMBED_TIMEOUT,
                                      config.EMBED_RETRIES, config.EMBED_CONCURRENCY)
            embed._remote_gate = threading.BoundedSemaphore(config.EMBED_CONCURRENCY)
        else:
            completions = None
        user_id = 'locomo-conv-' + conversation['sample_id']
        raw_speakers, evidence_map = {}, {}
        checkpoint()
        with TestClient(api.app) as service:
            headers = {'Authorization': 'Bearer ' + config.AUTH_TOKEN}
            for number, session in enumerate(conversation['sessions'], 1):
                report['stage'] = 'build'
                report['current_session'] = session['session_index']
                before = len(store.get(user_id).items)
                phase_started = time.monotonic()
                request_id = f"{conversation['sample_id']}-session-{session['session_index']}"
                already_committed = store.request_seen(request_id, user_id)
                if already_committed:
                    digest = store.item_id(request_id, 'raw', 0, user_id).rsplit('-', 1)[0]
                    fresh = [item for item in store.get(user_id).items if item.id.startswith(digest + '-')]
                else:
                    response = service.post('/add', json=make_add(conversation, session, user_id), headers=headers)
                    if budget:
                        budget.check()
                    if response.status_code != 200 or not response.json().get('success'):
                        raise RuntimeError('Add failed')
                    fresh = store.get(user_id).items[before:]
                counts = Counter(item.kind for item in fresh)
                if counts['raw'] != len(session['messages']):
                    raise RuntimeError('Raw turn count mismatch')
                for position, message in enumerate(session['messages']):
                    item_id = store.item_id(request_id, 'raw', position, user_id)
                    evidence_map[message['dia_id']] = item_id
                    raw_speakers[item_id] = message['speaker']
                if not any(row['session_index'] == session['session_index'] for row in report['build']):
                    report['build'].append({'session_index': session['session_index'], 'http_status': 200,
                        'raw': counts['raw'], 'facts': counts['fact'], 'recovered_committed': already_committed,
                        'seconds': time.monotonic() - phase_started})
                checkpoint()
                print(f"Build {number}/{len(conversation['sessions'])}: raw={counts['raw']} fact={counts['fact']}"
                      + (' (previously committed)' if already_committed else ''), flush=True)
            index = store.get(user_id)
            counts = Counter(item.kind for item in index.items)
            report.update(stored_raw=counts['raw'], stored_facts=counts['fact'], vector_shape=list(index.matrix.shape),
                          updates=len(store.get_updates(user_id)), llm_counters_after_build=dict(llm.counters))
            # Export actual stored memories for framework/dataflow inspection.
            with (output / 'memories.jsonl').open('w') as file:
                for item in index.items:
                    file.write(json.dumps(vars(item), ensure_ascii=False) + '\n')
            save(output / 'evidence_id_map.json', evidence_map)
            for number, question in enumerate(questions, 1):
                report.update(stage='answer', current_qa_id=question['qa_id'])
                phase_started = time.monotonic()
                response = service.post('/search', json={'user_id': user_id,
                    'query': question['question'], 'top_k': args.top_k}, headers=headers)
                if budget:
                    budget.check()
                retrieval_seconds = time.monotonic() - phase_started
                if response.status_code != 200 or not response.json().get('data'):
                    raise RuntimeError('Search failed or empty')
                memories = response.json()['data']
                if len(memories) > args.top_k:
                    raise RuntimeError('Return limit exceeded')
                fields = reader_context(memories, conversation, raw_speakers)
                prompt = formatter(**fields, question=question['question'])
                phase_started = time.monotonic()
                if args.execute:
                    answer = completions.create(model=config.LLM_MODEL, temperature=0, max_tokens=512,
                        stop=['\n'], messages=[{'role': 'user', 'content': prompt}]).choices[0].message.content.strip()
                    budget.check()
                else:
                    answer = 'Offline fixture'
                gold_ids = set(question.get('evidence') or [])
                mapped = {evidence_map[dia_id] for dia_id in gold_ids if dia_id in evidence_map}
                hits = mapped & {m['id'] for m in memories}
                diagnostic = {'qa_id': question['qa_id'], 'question': question['question'],
                    'category': question['category'], 'answer': answer, 'returned': len(memories),
                    'raw_evidence_recall': len(hits) / len(gold_ids) if gold_ids else None,
                    'raw_evidence_success': bool(hits) if gold_ids else None,
                    'raw_evidence_all': len(hits) == len(gold_ids) if gold_ids else None,
                    'retrieval_seconds': retrieval_seconds, 'answer_seconds': time.monotonic() - phase_started}
                report['qa'].append(diagnostic)
                stem = question['qa_id'].replace('#', '_')
                save(output / 'retrieval' / (stem + '.json'), {'data': memories, **diagnostic})
                (output / 'reader_inputs' / (stem + '.txt')).write_text(prompt)
                with (output / 'predictions.jsonl').open('a') as file:
                    file.write(json.dumps({'qa_id': question['qa_id'], 'predicted_answer': answer}, ensure_ascii=False) + '\n')
                checkpoint()
                print(f"Answer {number}/{len(questions)}; cost~CNY {report.get('estimated_cny', 0):.4f}", flush=True)
            report['update_checks'] = len(store.get_checks(user_id))
            report['llm_counters_final'] = dict(llm.counters)
        evidence_rows = [q for q in report['qa'] if q['raw_evidence_recall'] is not None]
        report['raw_retrieval_summary'] = {'questions_with_evidence': len(evidence_rows),
            **{key: sum(q[key] for q in evidence_rows) / len(evidence_rows) if evidence_rows else None
               for key in ['raw_evidence_recall', 'raw_evidence_success', 'raw_evidence_all']}}
        save(output / 'retrieval_summary.json', report['raw_retrieval_summary'])
        checkpoint()
        if args.execute:
            report['stage'] = 'score'
            checkpoint()
            report['score_summary'] = score_official(args.dataset, output, judge_completions, report, checkpoint, budget)
        report.update(success=True, stage='complete', questions_completed=len(report['qa']))
        checkpoint()
        print(f"{'REAL' if args.execute else 'FAKE'} COMPLETE: {len(report['build'])} sessions, {len(report['qa'])} questions; {output}", flush=True)
    except Exception as exc:
        report.update(failure_type=type(exc).__name__, failure_stage=report['stage'],
                      stage='stopped', questions_completed=len(report['qa']))
        checkpoint()
        reason = budget.failed if budget and budget.failed else type(exc).__name__
        raise RuntimeError(f'Conversation run stopped: {reason}; inspect report.json') from None
    finally:
        checkpoint()
        for client in clients:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DATASET)
    parser.add_argument('--workspace', type=Path, default=DATASET.parents[1])
    parser.add_argument('--sample-id', default='conv-26')
    parser.add_argument('--top-k', type=int, default=30)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--max-cny', type=float, default=0.99)
    parser.add_argument('--no-cost-limit', action='store_true', help='Explicit authorization to remove monetary cap')
    parser.add_argument('--resume', action='store_true', help='Continue an interrupted build; skip committed Adds')
    parser.add_argument('--output', type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--execute', action='store_true')
    modes.add_argument('--fake', action='store_true')
    args = parser.parse_args()
    if args.sample_id != 'conv-26':
        parser.error('This entry point is scoped to the previously tested LightMem conv-26')
    if not 1 <= args.top_k <= 100 or not math.isfinite(args.max_cny) or not 0 < args.max_cny <= 0.99:
        parser.error('top_k must be 1-100; remaining authorized budget is at most CNY 0.99')
    if args.execute and args.env_file is None:
        parser.error('Paid execution requires an explicit --env-file')
    conversation, questions = load_conversation(args.dataset, args.sample_id)
    formatter, hashes = lightmem_reader(args.workspace)
    output = (args.output or ROOT / 'artifacts' / ('locomo-conv26-qwen-once' if args.execute else
               'locomo-conv26-fake' if args.fake else 'locomo-conv26-plan')).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for folder in ['retrieval', 'reader_inputs']:
        (output / folder).mkdir(exist_ok=True)
    with (output / 'questions.jsonl').open('w') as file:
        for question in questions:
            file.write(json.dumps(question, ensure_ascii=False) + '\n')
    paths = [args.dataset / 'data/public/conversations.jsonl', args.dataset / 'data/public/questions.jsonl',
             args.dataset / 'data/raw/locomo_refined.json']
    manifest = {'sample_id': conversation['sample_id'], 'sessions': len(conversation['sessions']),
                'messages': sum(len(s['messages']) for s in conversation['sessions']),
                'questions': len(questions), 'question_ids': [q['qa_id'] for q in questions],
                'categories': dict(Counter(q['category'] for q in questions)),
                'text_only': sum(not q['is_multi_modality'] for q in questions), 'top_k': args.top_k,
                'data_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                'reader_source_sha256': hashes, 'image_policy': 'existing blip_caption text; no image downloads',
                'qa_policy': 'all questions, original IDs; no filtering by evidence or modality',
                'gold_is_sent_to_memory_or_reader': False}
    if args.resume:
        old = json.loads((output / 'manifest.json').read_text())
        if any(old.get(key) != manifest[key] for key in manifest):
            parser.error('Dataset, reader prompt, or settings changed; refuse resume')
    save(output / 'manifest.json', manifest)
    if not args.execute and not args.fake:
        print(json.dumps({k: manifest[k] for k in ['sample_id', 'sessions', 'messages', 'questions', 'text_only', 'categories', 'top_k']}, indent=2))
        return
    run(args, output, conversation, questions, formatter, hashes)


if __name__ == '__main__':
    main()
