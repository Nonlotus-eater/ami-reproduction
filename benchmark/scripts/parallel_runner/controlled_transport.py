"""Shared pre-request reservations around the unchanged upstream validator.

No successful outputs are checked against stop after transport: in-flight work
may settle and persist. Stop is enforced before every new provider attempt.
"""
import json, os, threading, time, itertools, random, math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import httpx
from .shared_control import SharedControl

def retry_delay(response, attempt, recorded_retry_after=None):
    base=float(os.environ.get('AML_RETRY_BASE_SECONDS','2'));ceiling=float(os.environ.get('AML_RETRY_MAX_SECONDS','60'))
    delay=min(ceiling,base*2**min(attempt,10))+random.uniform(0,min(1,base))
    if response is not None or recorded_retry_after:
        value=response.headers.get('retry-after') if response is not None else recorded_retry_after
        if value:
            try: server=max(0,float(value))
            except ValueError:
                try: server=max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
                except (ValueError,TypeError,OverflowError): server=0
            if math.isfinite(server):delay=max(delay,min(ceiling,server))
    return delay

def wrap(inner, budget, kind, *, sleeper=time.sleep):
    path = os.environ.get('AML_SHARED_LEDGER')
    if not path:
        raise RuntimeError('Shared ledger required; unbudgeted network forbidden')
    control = SharedControl(path)
    scope = os.environ.get('AML_RUN_SCOPE', 'unspecified')
    gate = threading.RLock()
    class Controlled(httpx.BaseTransport):
        def handle_request(self, request):
            with gate:
                payload = json.loads(request.content)
                if kind == 'embedding':
                    bound = sum(len(t.encode('utf-8')) + 256 for t in payload['input'])
                    maximum = 0
                else:
                    bound = sum(len(m['content'].encode('utf-8')) + 256 for m in payload['messages']) + 256
                    maximum = payload['max_tokens']
                for attempt in itertools.count():
                    try:
                        ident = control.reserve(kind, getattr(budget, 'phase', 'unspecified'), scope, bound, maximum)
                    except Exception:
                        budget.failed = 'Shared control denied new request'
                        raise
                    sent = False
                    before = len(budget.rows)
                    response = None
                    error = None
                    try:
                        with control.admit(ident, 'embedding' if kind == 'embedding' else 'llm', bound + maximum):
                            sent = True
                            try:
                                response = inner.handle_request(request)
                            except Exception as exc:
                                error = exc
                            row = budget.rows[-1] if len(budget.rows) > before else {}
                            status = row.get('http_status')
                            if 'estimated_cny' in row:
                                control.settle(ident, row['input_tokens'], row.get('output_tokens', 0), status or 200)
                            else:
                                control.unknown(ident, status)
                    except BaseException:
                        budget.failed = 'Shared control or accounting failure'
                        if sent:
                            control.unknown(ident)
                        else:
                            control.cancel_unsent(ident)
                        raise
                    network_error=row.get('error_type') in {'ConnectTimeout','ReadTimeout','WriteTimeout','PoolTimeout','ConnectError','ReadError','WriteError','RemoteProtocolError','ProxyError'}
                    retryable = status in {408,409,425,429,500,502,503,504,524} or isinstance(error,httpx.TransportError) or network_error
                    if row.get('failure_class') == 'context_length_exceeded':retryable = False
                    maximum_attempts=int(os.environ.get('AML_RETRY_MAX_ATTEMPTS','8'))
                    if retryable and (maximum_attempts==0 or attempt+1<maximum_attempts):
                        delay=retry_delay(response,attempt,row.get('retry_after'))
                        row['retry_delay_seconds']=delay
                        row['retry_policy']='configurable-transport-retry-v1'
                        if response is not None:
                            response.close()
                        budget.failed = None
                        # admit context has exited: backoff holds no provider slot.
                        while delay > 0:
                            control.check()
                            step=min(1,delay);sleeper(step);delay-=step
                        control.check()
                        continue
                    if error is not None:
                        raise error
                    return response
        def close(self):
            inner.close()
    return Controlled()
