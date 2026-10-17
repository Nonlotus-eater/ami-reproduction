"""Method-side AMI input adapter; native hybrid retrieval and shared API vectors."""
import os
from pathlib import Path
import sys



class SharedEmbedder:
    def __init__(self, gateway):
        self.gateway = gateway
        from memory_benchmark.runtime.artifacts import contract_hash
        self.identity = 'shared-api:' + contract_hash(gateway.identity)

    def encode_documents(self, texts):
        import numpy as np
        return np.asarray(self.gateway.embed(texts), dtype=np.float32)

    def encode_queries(self, texts):
        return self.encode_documents(texts)


class Adapter:
    def __init__(self, config, history, output, scope, gateway):
        sys.path.insert(0, str(Path(config['source_root']) / 'src'))
        from fastapi.testclient import TestClient
        from vanilla_rag.api import app, get_store
        from vanilla_rag.memory_store import SQLiteMemoryStore
        self.scope, self.history = scope, history
        self.gateway = gateway
        self.store = SQLiteMemoryStore(Path(output) / 'memory.sqlite3', SharedEmbedder(gateway),
            chunk_tokens=320, overlap_tokens=40, retrieval_mode='hybrid', rrf_k=60,
            lexical_weight=0.5, context_messages=0, option_view_weight=0,
            result_window=1, result_window_seed_k=20)
        os.environ['SKIP_MODEL_STARTUP'] = 'true'
        os.environ['ALLOW_UNAUTHENTICATED'] = 'true'
        os.environ.pop('MEMORY_API_KEY', None)
        self.app, self.get_store = app, get_store
        app.dependency_overrides[get_store] = lambda: self.store
        self.client = TestClient(app)
        self.client.__enter__()

    def add(self, session):
        from datetime import datetime
        messages = []
        for message in session.messages:
            date = message.timestamp or session.timestamp
            messages.append({'role': message.role, 'content': f'{message.speaker}: {message.text}',
                'timestamp': int(datetime.fromisoformat(date).timestamp() * 1000) if date else None})
        response = self.client.post('/add', json={'request_id': f'{self.scope}:{session.session_id}',
            'user_id': self.scope, 'session_id': session.session_id, 'messages': messages})
        self.gateway.budget.check()
        if response.status_code != 200 or not response.json().get('success'):
            raise RuntimeError(f'InvMem native Add failed: HTTP {response.status_code}')

    def finalize(self):
        pass

    def search(self, query, top_k):
        from memory_benchmark.schemas import Evidence
        response = self.client.post('/search', json={'user_id': self.scope, 'query': query.text, 'top_k': top_k})
        self.gateway.budget.check()
        if response.status_code != 200:
            raise RuntimeError(f'InvMem native Search failed: HTTP {response.status_code}')
        return [Evidence(r['id'], r['content']) for r in response.json()['data']]

    def close(self):
        self.client.__exit__(None, None, None)
        self.app.dependency_overrides.pop(self.get_store, None)
