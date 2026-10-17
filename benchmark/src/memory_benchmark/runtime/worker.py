"""Memory-only subprocess: scoring targets never appear in its job file."""
import argparse
from importlib import metadata
from pathlib import Path
import socket
import os
import resource
import sys
import time
import traceback

from ..methods import create
from ..pipeline.context import normalize
from ..schemas import History, Query
from .artifacts import append_row, file_hash, read_json, rows, write_json
from .model_client import Gateway
from .resources import limits


def deny_network():
    def blocked(*args, **kwargs):
        raise RuntimeError("Network forbidden in offline fixture mode")
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked


def environment():
    packages = {}
    for name in ("openai", "httpx", "tiktoken", "qdrant-client", "sentence-transformers", "torch"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return {"python": sys.version, "executable": sys.executable, "packages": packages,
            "cpu_affinity": sorted(os.sched_getaffinity(0)), "logical_cpu_count": os.cpu_count(),
            "container_limits": limits()}


def execute(job):
    output = Path(job["output"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "attempt.lock").open("x").close()
    if job["offline"]:
        deny_network()
    gateway, adapter = None, None
    report = {"status": "running", "method": job["method"], "scope": job["scope"],
              "offline_fixture": job["offline"], "environment": environment(), "phase_seconds": {}}
    write_json(output / "report.json", report)
    started = time.monotonic()
    def measured(phase, function, *args, **kwargs):
        if gateway:
            gateway.budget.phase = phase
        before = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            report["phase_seconds"][phase] = report["phase_seconds"].get(phase, 0) + time.perf_counter() - before
            write_json(output / "report.json", report)
    try:
        for field in ("history", "queries"):
            if file_hash(job[field]) != job["input_hashes"][field]:
                raise ValueError("Worker input hash mismatch")
        history = History.from_wire(read_json(job["history"]))
        queries = [Query(**q) for q in rows(job["queries"])]
        if not job["offline"]:
            gateway = Gateway(job["runtime"], output / "usage.json",
                              expected_identity=job["provider_identity"])
            gateway.install_native()
            report["embedding"] = {key: value for key, value in gateway.identity.items()
                                   if key.startswith("embedding_")}
            write_json(output / "report.json", report)
        adapter = measured("init", create, job["method"], job["method_config"], history, output,
                         job["scope"], gateway, offline=job["offline"])
        for session in history.sessions:
            before = report["phase_seconds"].get("add", 0)
            measured("add", adapter.add, session)
            append_row(output / "events.jsonl", {"stage": "add", "session_id": session.session_id,
                "timestamp": session.timestamp, "messages": len(session.messages),
                "seconds": report["phase_seconds"]["add"] - before})
        measured("finalize", adapter.finalize)
        append_row(output / "events.jsonl", {"stage": "finalize"})
        for query in queries:
            if query.benchmark != history.benchmark or query.episode_id != history.episode_id:
                raise ValueError("Query is outside worker history scope")
            before = report["phase_seconds"].get("search", 0)
            evidence = normalize(measured("search", adapter.search, query, job["runtime"]["top_k"]), job["runtime"]["top_k"])
            if gateway:
                gateway.budget.check()
            append_row(output / "retrievals.jsonl", {"id": query.qa_id,
                "evidence": [e.to_wire() for e in evidence],
                "seconds": report["phase_seconds"]["search"] - before})
        report.update(status="complete", questions=len(queries), sessions=len(history.sessions))
    except Exception as error:
        # Native exceptions can carry credentials or response bodies. Keep safe type only.
        report.update(status="failed", error_type=type(error).__name__,
            stack=[{"file": f.filename, "line": f.lineno, "function": f.name}
                   for f in traceback.extract_tb(error.__traceback__)])
    finally:
        try:
            if adapter:
                measured("close", adapter.close)
        except Exception as error:
            report.update(status="failed", close_error_type=type(error).__name__)
        if gateway:
            gateway.close()
            if gateway.budget.failed:
                report.update(status="failed", provider_failure=gateway.budget.failed)
        report["seconds"] = round(time.monotonic() - started, 3)
        consumed = resource.getrusage(resource.RUSAGE_SELF)
        report["resources"] = {"user_cpu_seconds": consumed.ru_utime, "system_cpu_seconds": consumed.ru_stime,
            "peak_rss_bytes": int(consumed.ru_maxrss * (1024 if sys.platform.startswith("linux") else 1))}
        write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    report = execute(read_json(parser.parse_args().job))
    print({"method": report["method"], "status": report["status"]}, flush=True)
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
