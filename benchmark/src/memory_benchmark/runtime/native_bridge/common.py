"""Frozen adapter loading and embedding pacing for native memory workers."""
from datetime import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def process_token(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def local_environment(setup):
    audit = Path(setup['audit_directory'])
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
        'PIP_CACHE_DIR': str(audit / 'cache/pip'), 'TMPDIR': str(audit / 'tmp'),
        'XDG_CACHE_HOME': str(audit / 'cache'), 'HF_HOME': str(audit / 'cache/hf'),
        'TORCH_HOME': str(audit / 'cache/torch'), 'MPLCONFIGDIR': str(audit / 'cache/matplotlib'),
        'TIKTOKEN_CACHE_DIR': str(audit / 'cache/tiktoken'), 'TOKENIZERS_PARALLELISM': 'false',
        'OMP_NUM_THREADS': '2', 'CUDA_VISIBLE_DEVICES': ''}
    identity = setup['provider_identity']
    hosts = [urlsplit(identity[k]).hostname for k in ('llm_base_url', 'embedding_base_url')]
    bypass = ','.join(filter(None, [env.get('NO_PROXY', ''), *hosts]))
    env.update(NO_PROXY=bypass, no_proxy=bypass)
    return env


def adapter_class(path, method):
    spec = importlib.util.spec_from_file_location('native_adapter_' + method, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Adapter


def pace_method_embeddings(gateway, policy):
    """Time method-side requests; retain the original validation and transform."""
    if not policy:
        return
    import httpx
    original = gateway.transports['embedding']
    folder = Path(policy['state_directory'])
    folder.mkdir(parents=True, exist_ok=True)
    class MethodPacedTransport(httpx.BaseTransport):
        def handle_request(self, request):
            with (folder / 'request.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = read(folder / 'state.json')
                wait = state.get('next_start', 0) - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                state['next_start'] = time.monotonic() + policy['minimum_interval_seconds']
                state['requests'] = state.get('requests', 0) + 1
                try:
                    return original.handle_request(request)
                finally:
                    if str(gateway.budget.failed or '').startswith('embedding HTTP 429'):
                        state['next_start'] = time.monotonic() + policy['cooldown_seconds']
                        state['rate_limit_events'] = state.get('rate_limit_events', 0) + 1
                    write(folder / 'state.json', state)
        def close(self):
            original.close()
    # Gateway's own SDK transport closes over this same dictionary. No source,
    # payload, model, response validation or embedding preprocessing is replaced.
    gateway.transports['embedding'] = MethodPacedTransport()
