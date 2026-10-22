"""Crash recovery must roll back unconfirmed writes across every local store."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json
from memory_benchmark.runtime.memory_checkpoint import MemoryCheckpoint, POLICY
from memory_benchmark.runtime.execution_overlay import verify_framework_overlay, verify_frozen_file
from memory_benchmark.pipeline.prepare import code_identity


def job(root, attempt=1, **overrides):
    output = root / f"memory_attempt_{attempt:03d}"
    output.mkdir()
    return {"output": str(output), "method": "mem0", "scope": f"run:mem0:history:{output.name}",
        "method_config": {}, "runtime": {"memory_checkpoint_policy": POLICY},
        "input_hashes": {"history": "history-hash", "queries": "query-hash"}, **overrides}


def insert(path, value):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS items (value TEXT)")
    connection.execute("INSERT INTO items VALUES (?)", (value,))
    connection.commit()
    return connection


def values(path):
    with sqlite3.connect(path) as connection:
        return [r[0] for r in connection.execute("SELECT value FROM items")]


class MemoryCheckpointTests(unittest.TestCase):
    def test_resume_restores_all_stores_wal_data_and_stable_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = MemoryCheckpoint(job(root), {"plan": "fixed"}, 3)
            vector = insert(first.storage / "vectors.sqlite", "confirmed")
            history = insert(first.storage / "history.db", "confirmed")
            (first.storage / "meta.json").write_text('{"collections": ["benchmark"]}')
            first.commit(1)
            # Mimic a failure after one store has persisted part of chunk 2.
            vector.execute("INSERT INTO items VALUES ('unconfirmed')")
            vector.commit()
            vector.close()
            history.close()
            scope = first.scope
            first.close()
            resumed = MemoryCheckpoint(job(root, 2), {"plan": "fixed"}, 3)
            self.assertEqual(resumed.completed, 1)
            self.assertEqual(resumed.scope, scope)
            self.assertEqual(values(resumed.storage / "vectors.sqlite"), ["confirmed"])
            self.assertEqual(values(resumed.storage / "history.db"), ["confirmed"])
            self.assertTrue((resumed.storage / "meta.json").exists())
            resumed.close()

    def test_snapshot_failure_leaves_last_committed_pointer_authoritative(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = MemoryCheckpoint(job(root), {}, 2)
            connection = insert(first.storage / "db.sqlite", "one")
            first.commit(1)
            saved = (first.root / "checkpoint.json").read_bytes()
            connection.execute("INSERT INTO items VALUES ('two')")
            connection.commit()
            with patch("memory_benchmark.runtime.memory_checkpoint.durable_json", side_effect=OSError):
                with self.assertRaises(OSError):
                    first.commit(2)
            self.assertEqual((first.root / "checkpoint.json").read_bytes(), saved)
            connection.close()
            first.close()
            resumed = MemoryCheckpoint(job(root, 2), {}, 2)
            self.assertEqual(resumed.completed, 1)
            self.assertEqual(values(resumed.storage / "db.sqlite"), ["one"])
            resumed.close()

    def test_process_exit_after_native_commit_before_checkpoint_replays_only_failed_chunk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_job = job(root)
            code = '''
import os, sqlite3, sys
from pathlib import Path
from memory_benchmark.runtime.artifacts import read_json
from memory_benchmark.runtime.memory_checkpoint import MemoryCheckpoint
c = MemoryCheckpoint(read_json(sys.argv[1]), {}, 2)
v = sqlite3.connect(c.storage / 'db.sqlite')
v.execute('CREATE TABLE items (value TEXT)')
v.execute("INSERT INTO items VALUES ('one')")
v.commit()
c.commit(1)
v.execute("INSERT INTO items VALUES ('two')")
v.commit()
os._exit(73)
'''
            write_json(root / "job.json", first_job)
            result = subprocess.run([sys.executable, "-c", code, str(root / "job.json")], check=False)
            self.assertEqual(result.returncode, 73)
            resumed = MemoryCheckpoint(job(root, 2), {}, 2)
            self.assertEqual(values(resumed.storage / "db.sqlite"), ["one"])
            connection = insert(resumed.storage / "db.sqlite", "two")
            resumed.commit(2, finalized=True)
            connection.close()
            resumed.close()
            final = MemoryCheckpoint(job(root, 3), {}, 2)
            self.assertEqual(values(final.storage / "db.sqlite"), ["one", "two"])
            self.assertTrue(final.finalized)
            final.close()

    def test_rejects_contract_changes_tampered_files_and_simultaneous_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = MemoryCheckpoint(job(root), {}, 2)
            (first.storage / "data").write_text("saved")
            first.commit(1)
            with self.assertRaises(BlockingIOError):
                MemoryCheckpoint(job(root, 2), {}, 2)
            first.close()
            for index, changed in enumerate(({"runtime": {"memory_checkpoint_policy": POLICY, "model": "changed"}},
                    {"input_hashes": {"history": "changed"}}, {"scope": "foreign-user"}), 3):
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "contract changed"):
                    MemoryCheckpoint(job(root, index, **changed), {}, 2)
            pointer = read_json(first.root / "checkpoint.json")
            (first.root / pointer["generation"] / "data").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                MemoryCheckpoint(job(root, 8), {}, 2)

    def test_no_policy_preserves_legacy_storage_and_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            current = job(Path(temp), runtime={})
            checkpoint = MemoryCheckpoint(current, {}, 3)
            self.assertFalse(checkpoint.enabled)
            self.assertEqual(checkpoint.storage, Path(current["output"]))
            self.assertEqual(checkpoint.scope, current["scope"])
            self.assertFalse((Path(temp) / "memory_resume").exists())

    def test_recorded_overlay_enables_new_workers_without_editing_frozen_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = job(root, runtime={"model": "unchanged"})
            write_json(root / "execution_overlay.json", {"version": 1,
                "activation": "new_memory_workers", "memory_checkpoint_policy": POLICY, "changes": {}})
            checkpoint = MemoryCheckpoint(current, {}, 2)
            self.assertTrue(checkpoint.enabled)
            self.assertEqual(current["runtime"], {"model": "unchanged"})
            self.assertEqual(checkpoint.completed, 0)
            checkpoint.close()


class ExecutionOverlayTests(unittest.TestCase):
    def test_only_declared_and_archived_changes_are_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            framework = root / "frozen_framework"
            path = framework / "src/worker.py"
            path.parent.mkdir(parents=True)
            path.write_text("original")
            expected = code_identity(framework)
            backup = root / "original-worker.py"
            backup.write_bytes(path.read_bytes())
            before = file_hash(path)
            path.write_text("updated")
            receipt = {"version": 1, "activation": "new_memory_workers",
                "baseline_framework_sha256": expected["sha256"], "memory_checkpoint_policy": POLICY,
                "changes": {"frozen_framework/src/worker.py": {
                    "before_sha256": before, "after_sha256": file_hash(path), "original": backup.name}}}
            write_json(root / "execution_overlay.json", receipt)
            verify_framework_overlay(framework, expected, code_identity(framework))
            verify_frozen_file(path, before)
            (framework / "src/reader.py").write_text("undeclared")
            with self.assertRaisesRegex(ValueError, "outside the recorded"):
                verify_framework_overlay(framework, expected, code_identity(framework))
            path.write_text("tampered")
            with self.assertRaisesRegex(ValueError, "overlay file changed"):
                verify_frozen_file(path, before)


if __name__ == "__main__":
    unittest.main()
