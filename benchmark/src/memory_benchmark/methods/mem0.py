"""Native OSS hybrid memory; recording time reaches the extraction input."""
import os
import sys
from unittest.mock import patch

from .base import MemoryAdapter
from ..schemas import Evidence
from ..runtime.artifacts import append_row
from ..runtime.embedding import provider


class Adapter(MemoryAdapter):
    def __init__(self, *args):
        super().__init__(*args)
        cfg, p = self.config, self.gateway.profile
        if cfg.get("embedding_backend") != "shared_api":
            raise ValueError("Mem0 requires embedding_backend=shared_api")
        sys.path.insert(0, cfg["source_root"])
        os.environ["FASTEMBED_CACHE_PATH"] = cfg["fastembed_cache"]
        os.environ["MEM0_DIR"] = str(self.output / "mem0_home")
        from mem0 import Memory
        dimensions = self.gateway.embedding_dimensions
        memory_config = {"version": "v1.1", "vector_store": {"provider": "qdrant",
            "config": {"collection_name": "benchmark", "path": str(self.output / "qdrant"), "embedding_model_dims": dimensions}},
            "llm": {"provider": "openai", "config": {"api_key": p["OPENAI_API_KEY"],
                "openai_base_url": p["OPENAI_BASE_URL"], "model": self.gateway.runtime["model"],
                "max_tokens": cfg["extraction_max_tokens"], "temperature": 0}},
            "embedder": {"provider": "openai", "config": {"model": "text-embedding-v4",
                "embedding_dims": dimensions}},
            "history_db_path": str(self.output / "history.db")}
        with patch("mem0.embeddings.openai.OpenAIEmbedding", provider(self.gateway)):
            self.memory = Memory.from_config(memory_config)

    def add(self, session):
        body = [{"role": m.role, "content": (f"[{m.timestamp}] " if m.timestamp else "") +
                 f"{m.speaker}: {m.text}"} for m in session.messages]
        prompt = (f"The messages were recorded at {session.timestamp}. Resolve relative dates against this recording time, "
                  "rather than the current evaluation date. Preserve explicitly stated absolute dates.") if session.timestamp else (
                  "Recording dates are unknown. Preserve the supplied message order and explicitly stated dates. "
                  "Do not resolve relative dates against today's date or invent recording dates.")
        result = self.memory.add(body, user_id=self.scope,
            metadata={"source_session_id": session.session_id, "observation_timestamp": session.timestamp},
            prompt=prompt)
        self.gateway.budget.check()
        append_row(self.output / "native_add.jsonl", {"session_id": session.session_id, "result": result})

    def finalize(self):
        pass

    def search(self, query, top_k):
        if top_k is None:
            store = self.memory.vector_store
            top_k = store.client.count(collection_name=store.collection_name,
                count_filter=store._create_filter({"user_id": self.scope}), exact=True).count
            if not top_k:
                return []
        results = self.memory.search(query.text, filters={"user_id": self.scope}, top_k=top_k)["results"]
        return [Evidence(str(r["id"]), r["memory"], (), r.get("metadata", {}).get("observation_timestamp")) for r in results]

    def close(self):
        self.memory.close()
        self.memory.vector_store.client.close()
