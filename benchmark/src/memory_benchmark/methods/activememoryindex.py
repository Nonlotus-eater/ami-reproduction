from datetime import datetime, timezone
from math import inf
import os
import sys
from contextlib import ExitStack
from unittest.mock import patch

from .base import MemoryAdapter
from ..schemas import Evidence
from ..runtime.profile import read_profile


class Adapter(MemoryAdapter):
    def __init__(self, *args):
        super().__init__(*args)
        from pathlib import Path
        from .compatibility import validate_ami_roles
        validate_ami_roles(self.config, {m.role for s in self.history.sessions for m in s.messages})
        root = Path(self.config["source_root"])
        if self.config.get("embedding_backend") != "shared_api":
            raise ValueError("AMI requires embedding_backend=shared_api")
        sys.path.insert(0, str(root))
        settings = read_profile(root / ".env.example")
        settings.update(self.gateway.profile)
        settings.update(self.config.get("feature_flags", {}))
        settings.update(AMI_LLM_MODEL=self.gateway.runtime["model"], AMI_DB_PATH=str(self.output / "memory.sqlite3"),
            AMI_REQUEST_LOG="", AMI_AUTH_SCHEME="none", AMI_AUTH_TOKEN="", AMI_LLM_RETRIES="0", AMI_EMBED_RETRIES="0",
            AMI_EMBED_BACKEND="openai", AMI_REPRO_EMBED_PROTOCOL="openai", AMI_EMBED_MODEL="text-embedding-v4",
            AMI_EMBED_DIMENSIONS=str(self.gateway.embedding_dimensions), AMI_EMBED_BATCH=str(self.gateway.embedding_batch))
        os.environ.update({k: str(v) for k, v in settings.items()})
        from fastapi.testclient import TestClient
        from app.main import app
        from app import embed
        import numpy as np
        self.embedding_bindings = ExitStack()
        self.embedding_bindings.enter_context(patch.object(embed, "_encode_remote",
            lambda texts: np.asarray(self.gateway.embed(texts), dtype=np.float32).reshape(-1, self.gateway.embedding_dimensions)))
        self.manager = TestClient(app)
        self.service = self.manager.__enter__()

    def add(self, session):
        when = int(datetime.fromisoformat(session.timestamp).timestamp() * 1000) if session.timestamp else None
        prefix = "[Recording date unknown; preserve source order; do not infer dates from ingestion time] " if when is None else ""
        logical_id = getattr(session, "chunk_id", None) or session.session_id
        response = self.service.post("/add", json={"request_id": f"{self.scope}:{logical_id}",
            "user_id": self.scope, "session_id": session.session_id,
            "messages": [{"role": m.role, "content": prefix + f"{m.speaker}: {m.text}", "timestamp": when} for m in session.messages]})
        self.gateway.budget.check()
        if response.status_code != 200 or not response.json().get("success"):
            raise RuntimeError("AMI synchronous Add failed")

    def finalize(self):
        pass

    def search(self, query, top_k):
        with ExitStack() as bindings:
            if top_k is None:
                from app import config, store
                top_k = len(store.get(self.scope).items)
                if not top_k:
                    return []
                # The native endpoint requires an integer. Use the actual scoped
                # index size, and remove its return-count cap for this request.
                bindings.enter_context(patch.object(config, "RETURN_LIMIT", top_k))
                bindings.enter_context(patch.object(config, "RETURN_CHAR_BUDGET", inf))
            response = self.service.post("/search", json={"user_id": self.scope, "query": query.text, "top_k": top_k})
        self.gateway.budget.check()
        if response.status_code != 200:
            raise RuntimeError("AMI Search failed")
        evidence = []
        for r in response.json()["data"]:
            raw = r.get("timestamp")
            source_dated = any(s.timestamp for s in self.history.sessions)
            when = datetime.fromtimestamp(raw / 1000, tz=timezone.utc).isoformat() if source_dated and isinstance(raw, (int, float)) else None
            evidence.append(Evidence(str(r["id"]), r["content"], (), when))
        return evidence

    def close(self):
        try:
            self.manager.__exit__(None, None, None)
        finally:
            self.embedding_bindings.close()
