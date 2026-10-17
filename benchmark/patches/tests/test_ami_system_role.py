"""Offline regression checks for the isolated AMI input-schema patch.

Run with the AMI Python environment. AMI_TEST_SOURCE_ROOT can select the
unpatched repository to reproduce the rejected system-role request.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get(
    "AMI_TEST_SOURCE_ROOT", ROOT / "baselines/ami-reproduction-system-role"
)).resolve()
sys.path.insert(0, str(SOURCE))

import numpy as np
from fastapi.testclient import TestClient
from app import main, store


class SystemRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        settings = {
            "DB_PATH": str(Path(self.temp.name) / "memory.sqlite3"),
            "AUTH_SCHEME": "none", "REQUEST_LOG": "", "RERANK_MODEL": "",
            "EMBED_BACKEND": "openai", "EMBED_MODEL": "text-embedding-v4",
            "EMBED_DIMENSIONS": 1024,
            "EMBED_BASE_URL": "https://embedding.invalid/v1",
            "EMBED_BATCH": 10, "EMBED_CONCURRENCY": 4,
            "EMBED_TIMEOUT": 20, "EMBED_RETRIES": 0,
            "UPDATE_DETECT": False,
        }
        for name, value in settings.items():
            self.stack.enter_context(patch.object(main.config, name, value))
        self.stack.enter_context(patch.object(main.config, "llm_available", return_value=False))
        self.stack.enter_context(patch.object(main.embed, "warm_up"))
        self.stack.enter_context(patch.object(main.llm, "extract_facts", return_value=[]))

        def encode(texts, **kwargs):
            vectors = np.zeros((len(texts), 1024), dtype=np.float32)
            vectors[:, 0] = 1.0
            return vectors

        self.stack.enter_context(patch.object(main, "encode_or_unavailable", side_effect=encode))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in regression test")))
        self.client = self.stack.enter_context(TestClient(main.app))

    def tearDown(self):
        self.stack.close()
        if store._conn is not None:
            store._conn.close()
            store._conn = None
        store._cache.clear()
        store._cached_items = 0
        store._embedding_dimensions = None
        self.temp.cleanup()

    def add(self, messages):
        return self.client.post("/add", json={
            "request_id": "regression", "user_id": "regression",
            "session_id": "session", "messages": messages,
        })

    def test_system_history_is_written_and_searchable_without_invented_date(self):
        response = self.add([{
            "role": "system", "content": "The user prefers concise answers.",
            "timestamp": None,
        }])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        item = store._conn.execute(
            "SELECT content, created_at FROM items WHERE kind='raw'"
        ).fetchone()
        self.assertEqual(item[0], "System: The user prefers concise answers.")
        self.assertIsNone(item[1])
        response = self.client.post("/search", json={
            "user_id": "regression", "query": "What answer style is preferred?",
            "top_k": 100,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("The user prefers concise answers." in row["content"]
                            for row in response.json()["data"]))

    def test_existing_user_and_assistant_labels_are_preserved(self):
        response = self.add([
            {"role": "user", "content": "I live in Kyoto.", "timestamp": None},
            {"role": "assistant", "content": "Understood.", "timestamp": None},
        ])
        self.assertEqual(response.status_code, 200)
        contents = {row[0] for row in store._conn.execute(
            "SELECT content FROM items WHERE kind='raw'")}
        self.assertEqual(contents, {"I: I live in Kyoto.", "Assistant: Understood."})

    def test_unknown_roles_are_still_rejected_before_persistence(self):
        response = self.add([{"role": "tool", "content": "Unexpected role."}])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
