"""Explicit fake/academic launch modes. Never import the app before configuring it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def read_profile(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise ValueError('Invalid environment file assignment')
        values[key] = value.split(' #', 1)[0].strip().strip('"').strip("'")
    return values


def clear_model_environment():
    for key in list(os.environ):
        if key.startswith(('AMI_', 'OPENAI_', 'DASHSCOPE_')):
            del os.environ[key]


def install_fakes():
    """Replace SDK transport only; execute upstream prompts/parsers/embed adapter.

    These deterministic fixtures are not language models or quality evidence.
    Update positive/negative cases are exercised by the upstream fake tests.
    """
    import numpy as np
    import openai
    from app import llm

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = self.completions = self.embeddings = self

        def create(self, **kwargs):
            if 'input' in kwargs:
                entries = []
                for index, text in enumerate(kwargs['input']):
                    vec = np.zeros(kwargs['dimensions'], dtype=np.float32)
                    for word in re.findall(r'\w+', text.lower()) or [text]:
                        slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], 'little') % len(vec)
                        vec[slot] += 1
                    entries.append(SimpleNamespace(index=index, embedding=vec.tolist()))
                return SimpleNamespace(data=entries)
            system, user = (msg['content'] for msg in kwargs['messages'])
            if system.startswith(llm.EXTRACT_SYSTEM[:60]):
                facts = [re.sub(r'^.*?\buser:\s*', '', line) for line in user.splitlines() if 'user:' in line]
                reply = json.dumps({'facts': facts[:24]})
            elif system.startswith(llm.UPDATE_INTENT_SYSTEM[:60]):
                reply = '{"statements": []}'
            elif system.startswith(llm.QUESTION_SCOPE2_SYSTEM[:60]):
                reply = '{"needs_past_value": true, "time_scoped": true}'
            elif system.startswith(llm.UPDATE_VERIFY3_SYSTEM[:60]):
                reply = '{"memories": []}'
            else:
                reply = 'What did I say about ' + user.splitlines()[0] + '?'
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    openai.OpenAI = FakeClient
    # All provider calls should be fakes. Any accidentally added network path fails.
    def no_connect(*args, **kwargs):
        raise RuntimeError('Offline reproduction forbids outbound socket connections')
    socket.socket.connect = no_connect
    socket.socket.connect_ex = no_connect
    socket.create_connection = no_connect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['fake', 'academic'], required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--port', type=int, default=8012, help='0 chooses a free loopback port')
    parser.add_argument('--db', type=Path)
    parser.add_argument('--allow-paid-api', action='store_true')
    args = parser.parse_args()
    if args.mode == 'academic' and not args.allow_paid_api:
        parser.error('Academic startup makes paid embedding calls. Obtain approval, then use --allow-paid-api.')
    os.chdir(ROOT)
    if args.mode == 'fake':
        token = os.environ.get('AMI_REPRO_TEST_TOKEN') or secrets.token_urlsafe(32)
        clear_model_environment()
        os.environ.update(read_profile(ROOT / '.env.example'))
        os.environ.update(AMI_AUTH_TOKEN=token, OPENAI_API_KEY='offline-fixture-not-a-credential',
                          AMI_EMBED_API_KEY='offline-fixture-not-a-credential',
                          OPENAI_BASE_URL='http://offline.invalid/v1',
                          AMI_EMBED_BASE_URL='http://offline.invalid/v1')
        db = args.db or ROOT / 'data' / 'fake.sqlite3'
    else:
        if args.env_file is None:
            parser.error('Academic mode requires an explicit --env-file; no implicit secret discovery.')
        profile = read_profile(args.env_file.resolve())
        clear_model_environment()
        os.environ.update(profile)
        required = ['OPENAI_API_KEY', 'AMI_EMBED_API_KEY', 'AMI_AUTH_TOKEN', 'AMI_EMBED_BASE_URL']
        if any(not os.environ.get(key, '').strip() for key in required):
            parser.error('Missing academic credentials/configuration; see .env.example (values are never printed).')
        if profile.get('AMI_EMBED_MODEL') != 'text-embedding-v4' or profile.get('AMI_LLM_MODEL') != 'gpt-4o-mini':
            parser.error('Reproduction academic mode requires text-embedding-v4 and gpt-4o-mini.')
        db = args.db or Path(profile.get('AMI_DB_PATH', './data/memory-v4.sqlite3'))
    db = db.resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    os.environ['AMI_DB_PATH'] = str(db)
    if args.mode == 'fake':
        install_fakes()
    import uvicorn
    from app.main import app
    if args.mode == 'fake':
        app.title = 'AMI reproduction — OFFLINE FAKE, not evaluation eligible'
    print(f'MODE={args.mode}; bind=127.0.0.1; port={args.port}; db={db}', flush=True)
    # One worker: upstream in-memory indices are not multi-worker coherent.
    uvicorn.run(app, host='127.0.0.1', port=args.port, workers=1)


if __name__ == '__main__':
    main()
