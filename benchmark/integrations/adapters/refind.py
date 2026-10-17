"""Method-side AMI adapter; preserve the native ReFind four-action retrieval agent."""
import os
from pathlib import Path
import sys



class SharedPlanner:
    def __init__(self, gateway, maximum):
        self.gateway, self.maximum = gateway, maximum
        self.traces = []

    def complete(self, messages):
        from app.llm import LLMReply
        start = len(self.gateway.budget.rows)
        reply = self.gateway.complete(messages, stage='memory_retrieval_planner',
            max_tokens=self.maximum, temperature=0)
        calls = self.gateway.budget.rows[start:]
        self.traces.append({'messages': [dict(m) for m in messages], 'reply': reply.text})
        return LLMReply(reply.text, sum(c.get('input_tokens', 0) for c in calls),
                        sum(c.get('output_tokens', 0) for c in calls))


class Adapter:
    def __init__(self, config, history, output, scope, gateway):
        sys.path.insert(0, str(Path(config['source_root'])))
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.store import MemoryStore
        from app.retriever import ReFindRetriever
        self.scope, self.history, self.output, self.gateway = scope, history, Path(output), gateway
        path = self.output / 'memory.sqlite3'
        # The upstream module creates an app at import time. Keep that app's
        # side-effect database inside this job, never in the benchmark data tree.
        os.environ['DATABASE_PATH'] = str(path)
        os.environ['RETRIEVAL_MODE'] = 'agent'
        os.environ.pop('OPENROUTER_API_KEY', None)
        from app.main import create_app
        settings = Settings(path, 'agent', gateway.profile['OPENAI_API_KEY'],
            gateway.profile['OPENAI_BASE_URL'], gateway.runtime['model'],
            gateway.runtime['request_timeout_seconds'], 900, 4, 5, 2, True, 30)
        self.store = MemoryStore(path, retention_days=30)
        self.planner = SharedPlanner(gateway, settings.llm_max_tokens)
        self.retriever = ReFindRetriever(self.store, settings, planner=self.planner)
        self.client = TestClient(create_app(settings, self.store, self.retriever))
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
        if response.status_code != 200 or not response.json().get('success'):
            raise RuntimeError(f'ReFind native Add failed: HTTP {response.status_code}')

    def finalize(self):
        pass

    def search(self, query, top_k):
        from memory_benchmark.schemas import Evidence
        from memory_benchmark.runtime.artifacts import contract_hash, write_json
        before = len(self.planner.traces)
        response = self.client.post('/search', json={'user_id': self.scope, 'query': query.text, 'top_k': top_k})
        self.gateway.budget.check()
        if response.status_code != 200:
            raise RuntimeError(f'ReFind native agent Search failed: HTTP {response.status_code}')
        write_json(self.output / f'planner-{contract_hash(query.qa_id)[:20]}.json',
            {'qa_id': query.qa_id, 'agent_mode': True, 'trace': self.planner.traces[before:]})
        return [Evidence(r['id'], r['content']) for r in response.json()['data']]

    def close(self):
        self.client.__exit__(None, None, None)
