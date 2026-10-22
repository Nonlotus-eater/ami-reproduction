from __future__ import annotations
import json
import math
import re
import threading
import time

from . import retry


SAFE_CODES = retry.FATAL_CODES | {'rate_limit_exceeded'}


class CallBudget:
    """Record costs and unknown-usage reservations; terminal failures stop the stage."""
    def __init__(self, profile, *, on_change=None, max_retries=0):
        self.lock = threading.RLock()
        self.rows = []
        self.reserved = 0.0
        self.failed = None
        self.on_change = on_change
        self.peak_reserved = 0.0
        self.request_count = 0
        self.retry_policy = retry.RetryPolicy.from_profile(profile, max_retries)
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


def make_transport(budget, kind, endpoint, model, *,
                   disable_thinking_required=True, allowed_response_models=None, timeout=30,
                   embedding_dimensions=None, embedding_batch=10, request_pacing=None):
    import httpx
    allowed_response_models = frozenset(allowed_response_models or {model})

    class GuardedTransport(httpx.BaseTransport):
        def __init__(self):
            self.client = httpx.Client(timeout=timeout, follow_redirects=False)
            owner = self

            class Sender(httpx.BaseTransport):
                def handle_request(self, request):
                    response = owner.client.send(request, stream=False, follow_redirects=False)
                    response.read()
                    return response

                def close(self):
                    pass

            self.sender = Sender()
            if request_pacing:
                from .pacing import paced_transport
                self.sender = paced_transport(self.sender, request_pacing)

        def close(self):
            self.sender.close()
            self.client.close()

        def record_usage(self, body, row, *, required=False):
            usage = body.get('usage') if isinstance(body, dict) else None
            if not isinstance(usage, dict):
                if required:
                    raise ValueError('Missing token usage')
                return
            input_tokens, output_tokens = usage.get('prompt_tokens'), usage.get('completion_tokens', 0)
            if any(type(v) is not int or v < 0 for v in [input_tokens, output_tokens]):
                if required:
                    raise ValueError('Invalid token usage')
                return
            row.update(input_tokens=input_tokens, output_tokens=output_tokens,
                       estimated_cny=budget.cost(kind, input_tokens, output_tokens), usage_status='known')

        def validate_body(self, body, payload, row):
            if not isinstance(body, dict) or body.get('error'):
                raise ValueError('Provider error response')
            self.record_usage(body, row, required=True)
            returned_model = body.get('model')
            if isinstance(returned_model, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}', returned_model):
                row['returned_model'] = returned_model
            if returned_model not in allowed_response_models:
                row['validation_error'] = 'response_model_missing' if returned_model is None else 'response_model_not_allowed'
                budget.failed = 'Response model missing or outside frozen allowlist'
                raise ValueError('Response model does not match selected model or allowed snapshot')
            if kind == 'embedding':
                from .embedding import normalized
                data = body.get('data')
                if not isinstance(data, list) or len(data) != len(payload['input']):
                    raise ValueError('Incomplete embedding response')
                indices = [item.get('index') for item in data if isinstance(item, dict)]
                if (len(indices) != len(data) or any(type(i) is not int for i in indices)
                        or sorted(indices) != list(range(len(data)))):
                    raise ValueError('Invalid embedding response indices')
                for item in data:
                    normalized(item.get('embedding'), embedding_dimensions or len(item.get('embedding', [])))
                row['dimensions'], row['inputs'] = embedding_dimensions, len(payload['input'])
            if row['input_tokens'] > row['input_token_bound'] or row['output_tokens'] > row['output_token_bound']:
                raise ValueError('Provider usage exceeds reserved bounds')
            if kind in {'llm', 'judge'} and not body['choices'][0]['message']['content'].strip():
                raise ValueError('Empty completion')

        def handle_request(self, request):
            with budget.lock:
                budget.check()
                payload = json.loads(request.content)
                if request.method != 'POST' or str(request.url) != endpoint or payload.get('model') != model:
                    budget.failed = 'Unexpected provider endpoint or model'
                    budget.check()
                if request.headers.get('x-stainless-retry-count', '0') != '0':
                    budget.failed = 'SDK retry forbidden; retries are owned by transport'
                    budget.check()
                if kind in {'llm', 'judge'}:
                    if (disable_thinking_required and payload.get('enable_thinking') is not False) or payload.get('stream'):
                        budget.failed = 'Contract requires nonthinking, nonstreaming calls'
                        budget.check()
                    messages = payload['messages']
                    input_bound = sum(len(m['content'].encode('utf-8')) + 256 for m in messages) + 256
                    output_bound = payload['max_tokens']
                else:
                    inputs = payload.get('input')
                    if (not isinstance(inputs, list) or not 1 <= len(inputs) <= embedding_batch
                            or any(not isinstance(t, str) or not t for t in inputs)
                            or (embedding_dimensions is not None and payload.get('dimensions') != embedding_dimensions)):
                        budget.failed = 'Invalid embedding input, batch size or dimensions'
                        budget.check()
                    input_bound = sum(len(text.encode('utf-8')) + 256 for text in inputs)
                    output_bound = 0
                budget.request_count += 1
                request_id = budget.request_count
                policy = budget.retry_policy
                last_index = policy.effective_max_retries
                reservation = budget.cost(kind, input_bound, output_bound)
                for index in range(last_index + 1):
                    budget.reserved += reservation
                    budget.peak_reserved = max(budget.peak_reserved, budget.reserved)
                    row = {'kind': kind, 'input_token_bound': input_bound,
                           'output_token_bound': output_bound, 'reserved_cny': reservation,
                           'requested_model': payload['model'], 'request_id': request_id,
                           'phase': getattr(budget, 'phase', 'unspecified'),
                           'started_unix_seconds': time.time(), 'retry_count': index,
                           'sdk_retry_count': 0, 'usage_status': 'pending'}
                    budget.rows.append(row)
                    budget.notify()
                    response, keep_response, delay = None, False, None
                    started = time.perf_counter()
                    try:
                        try:
                            response = self.sender.handle_request(request)
                        except httpx.TransportError as error:
                            row.update(error_type=type(error).__name__, failure_kind='transport',
                                       retryable=retry.retryable_transport_error(error))
                            if row['retryable'] and index < last_index:
                                delay, source = policy.delay(index, {})
                                row.update(retry_delay_seconds=delay, retry_delay_source=source)
                                continue
                            budget.failed = f'{kind} transport failure ({type(error).__name__}); attempts={index + 1}'
                            row['retry_exhausted'] = row['retryable']
                            raise
                        row['http_status'] = response.status_code
                        if response.status_code != 200:
                            try:
                                body = response.json()
                            except ValueError:
                                body = None
                            self.record_usage(body, row)
                            error = body.get('error', {}) if isinstance(body, dict) else {}
                            code = error.get('code') if isinstance(error, dict) else None
                            code = code if isinstance(code, str) else None
                            provider_message = error.get('message') if isinstance(error, dict) else None
                            if isinstance(code, str) and code in SAFE_CODES:
                                row['provider_error_code'] = code
                            if hasattr(budget, 'provider_error_redactions') and isinstance(error, dict):
                                message = error.get('message')
                                if isinstance(message, str):
                                    for secret in budget.provider_error_redactions:
                                        message = message.replace(secret, '[REDACTED]')
                                    row['provider_error_message'] = message[:1600]
                            row.update(error_type='HTTPStatusError', failure_kind='http',
                                retryable=retry.retryable_http_error(response.status_code, code,
                                    provider_message, response.headers))
                            if row['retryable'] and code == 'model_not_found':
                                row['retry_reason'] = 'provider_channel_unavailable'
                            if row['retryable'] and index < last_index:
                                delay, source = policy.delay(index, response.headers)
                                row['retry_delay_source'] = source
                                if delay is not None:
                                    row['retry_delay_seconds'] = delay
                                    continue
                            budget.failed = f'{kind} HTTP {response.status_code}' + (
                                f' ({code})' if row.get('provider_error_code') else '')
                            row['retry_exhausted'] = row['retryable'] and index == last_index
                            # Preserve HTTP status semantics even when diagnostic text is disabled.
                            keep_response = True
                            return response
                        self.validate_body(response.json(), payload, row)
                        keep_response = True
                        return response
                    except Exception as error:
                        row.setdefault('error_type', type(error).__name__)
                        row.setdefault('failure_kind', 'validation')
                        if budget.failed is None:
                            budget.failed = 'Provider failure; no further requests allowed'
                        raise
                    finally:
                        # Exactly one settlement per wire attempt. Unknown usage keeps its
                        # conservative reservation; it is never presented as known zero cost.
                        budget.reserved += row.get('estimated_cny', reservation) - reservation
                        row['usage_status'] = 'known' if 'estimated_cny' in row else 'unknown'
                        row['settled'] = True
                        row['seconds'] = time.perf_counter() - started
                        if response is not None and not keep_response:
                            response.close()
                        budget.notify()
                        if delay is not None:
                            retry.sleep(delay)
                raise AssertionError('Unreachable retry loop exit')

    return GuardedTransport()
