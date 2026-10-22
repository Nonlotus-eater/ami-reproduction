"""Restore an Add prefix, including all local stores, before retrying a history.

Only the four synchronous, local-SQLite adapters are supported. No native calls
run while a snapshot is taken. SQLite's backup API includes committed WAL data;
the pointer advances only after every store and its checksums have been saved.
Failed or interrupted attempts are never a source of restored state.
"""
from contextlib import closing
import fcntl
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .artifacts import contract_hash, file_hash, read_json, write_json

POLICY = "add_chunks_sqlite_snapshot_v1"
METHODS = {"activememoryindex", "mem0", "invmem", "refind"}


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_json(path, value):
    write_json(path, value)
    with Path(path).open("rb") as handle:
        os.fsync(handle.fileno())
    sync_directory(Path(path).parent)


def sqlite_file(path):
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def snapshot(source, destination):
    """Copy quiescent adapter state; back up SQLite rather than copying WALs."""
    destination.mkdir()
    files = sorted(source.rglob("*"))
    databases = {p for p in files if p.is_file() and not p.is_symlink() and sqlite_file(p)}
    hashes = {}
    for path in files:
        if path.is_symlink():
            raise ValueError("Memory state must not contain symlinks")
        relative = str(path.relative_to(source))
        target = destination / relative
        if path.is_dir():
            target.mkdir(exist_ok=True)
            continue
        if not path.is_file():
            raise ValueError("Unsupported memory state file")
        if path.name == ".lock" or any(str(path) == str(db) + suffix
                for db in databases for suffix in ("-wal", "-shm", "-journal")):
            continue
        if path in databases:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as src, \
                    closing(sqlite3.connect(target)) as dst:
                deadline = time.monotonic() + 60
                def progress(*args):
                    if time.monotonic() > deadline:
                        raise TimeoutError("SQLite memory snapshot timed out")
                src.backup(dst, pages=256, progress=progress)
                if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("Invalid SQLite memory snapshot")
        else:
            shutil.copyfile(path, target)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
        hashes[relative] = file_hash(target)
    for directory in sorted((p for p in destination.rglob("*") if p.is_dir()), reverse=True):
        sync_directory(directory)
    sync_directory(destination)
    return hashes


def checkpoint_policy(job):
    policy = job["runtime"].get("memory_checkpoint_policy")
    if policy is None:
        # A recorded overlay enables only newly started workers in an existing
        # experiment. Already running processes have imported their old code.
        from .execution_overlay import checked_changes, find_overlay
        overlay = find_overlay(Path(job["output"]))
        if overlay:
            checked_changes(*overlay)
            policy = overlay[1].get("memory_checkpoint_policy")
    if policy not in (None, POLICY):
        raise ValueError("Unknown memory checkpoint policy")
    if policy and job["method"] not in METHODS:
        raise ValueError("Adapter does not support local memory checkpoints")
    if policy and job.get("offline"):
        raise ValueError("In-memory offline fixtures do not support persistent memory checkpoints")
    return policy


class MemoryCheckpoint:
    def __init__(self, job, chunk_audit, chunk_count):
        self.enabled = bool(checkpoint_policy(job))
        self.output = Path(job["output"])
        self.storage = self.output
        self.scope = job["scope"]
        self.completed = 0
        self.finalized = False
        self.lock = None
        self.chunk_count = chunk_count
        if not self.enabled:
            return
        self.storage = self.output / "memory_state"
        self.root = (self.output.parent if self.output.name.startswith("memory_attempt_")
                     else self.output) / "memory_resume"
        self.root.mkdir(exist_ok=True)
        self.lock = (self.root / "checkpoint.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            scope = job["scope"]
            if scope.endswith(":" + self.output.name):
                scope = scope[:-(len(self.output.name))] + "memory"
            manifest_path = self.output.parents[3] / "manifest.json" if len(self.output.parents) > 3 else None
            manifest = read_json(manifest_path) if manifest_path and manifest_path.exists() else {}
            adapter_file = Path(job["method_config"].get("adapter_file",
                str(Path(__file__).parents[1] / "methods" / (job["method"] + ".py"))))
            binding = {"policy": POLICY, "method": job["method"], "config": job["method_config"],
                "runtime": job["runtime"], "provider_identity": job.get("provider_identity"),
                "input_hashes": job["input_hashes"], "add_plan": chunk_audit, "scope": scope,
                "native_code": manifest.get("methods", {}).get(job["method"], {}).get("native_code"),
                "adapter_sha256": file_hash(adapter_file),
                "checkpoint_code_sha256": file_hash(Path(__file__))}
            self.contract = contract_hash(binding)
            identity_path = self.root / "identity.json"
            identity = {"contract_sha256": self.contract, "scope": scope, "binding": binding}
            if identity_path.exists():
                if read_json(identity_path) != identity:
                    raise ValueError("Memory checkpoint contract changed")
            else:
                durable_json(identity_path, identity)
            self.scope = scope
            self.restore()
        except BaseException:
            self.close()
            raise

    def restore(self):
        if self.storage.exists():
            raise ValueError("Attempt memory state already exists")
        pointer = self.root / "checkpoint.json"
        if not pointer.exists():
            self.storage.mkdir()
            return
        state = read_json(pointer)
        if state["contract_sha256"] != self.contract:
            raise ValueError("Memory checkpoint contract changed")
        count = state["completed_add_chunks"]
        if type(count) is not int or not 0 <= count <= self.chunk_count:
            raise ValueError("Invalid completed Add prefix")
        if type(state["finalized"]) is not bool or (state["finalized"] and count != self.chunk_count):
            raise ValueError("Invalid finalize checkpoint")
        generation = state["generation"]
        if Path(generation).name != generation or not generation.startswith("generation-"):
            raise ValueError("Invalid memory snapshot generation")
        source = self.root / generation
        if source.is_symlink():
            raise ValueError("Memory snapshot generation is a symlink")
        actual = {}
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("Memory snapshot contains a symlink")
            if path.is_file():
                actual[str(path.relative_to(source))] = file_hash(path)
        if not source.is_dir() or actual != state["files"]:
            raise ValueError("Memory snapshot changed")
        shutil.copytree(source, self.storage)
        self.completed, self.finalized = count, state["finalized"]

    def commit(self, completed, *, finalized=False):
        if not self.enabled:
            return
        if (type(completed) is not int or not self.completed <= completed <= self.chunk_count
                or completed > self.completed + 1 or (finalized and completed != self.chunk_count)):
            raise ValueError("Memory checkpoint must advance a contiguous Add prefix")
        generation = "generation-" + uuid.uuid4().hex
        target = self.root / generation
        hashes = snapshot(self.storage, target)
        durable_json(self.root / "checkpoint.json", {"policy": POLICY,
            "contract_sha256": self.contract, "generation": generation,
            "completed_add_chunks": completed, "total_add_chunks": self.chunk_count,
            "finalized": finalized, "files": hashes, "source_attempt": self.output.name,
            "updated_unix_seconds": time.time()})
        self.completed, self.finalized = completed, finalized
        # Only the pointer's generation is authoritative. A process killed
        # before publishing the pointer can leave an ignored orphan directory.
        for old in self.root.glob("generation-*"):
            if old != target:
                shutil.rmtree(old)

    def describe(self):
        return {"policy": POLICY, "completed_add_chunks": self.completed,
                "total_add_chunks": self.chunk_count, "finalized": self.finalized,
                "checkpoint": str(self.root / "checkpoint.json")}

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None
