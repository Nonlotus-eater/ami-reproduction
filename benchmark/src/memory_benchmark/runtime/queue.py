"""Serial, resumable case/method jobs; successful stages never run again.

Every job writes answers before opening its scoring targets. Worker failures
leave that job unscored and do not prevent the other baselines from progressing.
"""
from collections import defaultdict
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

from ..benchmarks.native_contract import ROOT
from ..pipeline.aggregate import import_native, summarize
from ..pipeline.evaluate import evaluate
from ..pipeline.prepare import native_identity, verify_prepared
from ..pipeline.reader import answer
from ..pipeline.reliability import report as reliability_report
from ..pipeline.context import GPTTokenizer
from ..schemas import History, Query, Target
from .artifacts import append_row, contract_hash, file_hash, read_json, rows, write_json, write_rows
from .model_client import Gateway
from .profile import public_identity, read_profile, validate_profile


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    write_json(temp, value)
    temp.replace(path)


def records(path):
    return list(rows(path)) if Path(path).exists() else []


def initialize(prepared, methods, output):
    manifest = verify_prepared(prepared)
    from ..pipeline.runner import validate_runtime
    validate_runtime(manifest["runtime"])
    from ..methods.compatibility import validate_method_inputs
    validate_method_inputs(prepared, methods)
    runtime = manifest["runtime"]
    identity = public_identity(validate_profile(read_profile(runtime["env_file"]), runtime["model"]), runtime)
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / "frozen_framework"
    for relative in manifest["framework"]["files"]:
        target = frozen / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    manifest = {**manifest, "prepared_contract_sha256": contract_hash(manifest),
        "execution": {"run_id": output.name, "offline_fixture": False, "provider_identity": identity,
                      "actual_reader_tokenizer": GPTTokenizer(runtime["model"]).name},
        "methods": {name: {"config": config, "native_code": native_identity(config)} for name, config in methods.items()}}
    write_json(output / "manifest.json", manifest)
    write_json(output / "launch.json", {"prepared": str(prepared), "output": str(output), "methods": list(methods)})
    return manifest


def check_manifest(output, prepared, methods):
    manifest = read_json(output / "manifest.json")
    # verify_prepared runs against this queue's frozen framework on resume.
    expected = verify_prepared(prepared)
    if contract_hash(expected) != manifest["prepared_contract_sha256"]:
        raise ValueError("Prepared contract changed")
    for name, config in methods.items():
        if manifest["methods"].get(name) != {"config": config, "native_code": native_identity(config)}:
            raise ValueError("Native method config or code changed")
    if set(methods) != set(manifest["methods"]):
        raise ValueError("Queue method set changed")
    identity = public_identity(validate_profile(read_profile(manifest["runtime"]["env_file"]), manifest["runtime"]["model"]), manifest["runtime"])
    if identity != manifest["execution"]["provider_identity"]:
        raise ValueError("Provider identity changed")
    return manifest


def ordered_jobs(prepared, methods):
    grouped = defaultdict(list)
    for case in rows(prepared / "cases.jsonl"):
        grouped[case["benchmark"]].append(case)
    for cases in grouped.values():
        cases.sort(key=lambda c: (sum(len(m["text"]) for s in read_json(prepared / c["history"])["sessions"]
                                    for m in s["messages"]), c["key"]))
    # First visit every dataset/method on its shortest selected COMPLETE history.
    for i in range(max(map(len, grouped.values()))):
        for cases in grouped.values():
            if i < len(cases):
                for method in methods:
                    yield method, cases[i]


def publish(output, manifest, tasks, active=None):
    digest = contract_hash(manifest)
    summaries, reliability = {}, {}
    for method in manifest["methods"]:
        scored = {}
        for path in (output / method).rglob("native_scores.jsonl"):
            checkpoint = read_json(path.parent / "checkpoint.json")
            if file_hash(path) != checkpoint["files"].get("native_scores.jsonl"):
                raise ValueError("Persisted score checkpoint changed")
            benchmark = path.parent.parent.name
            for native in rows(path):
                normalized = import_native(manifest, [native], benchmark, method, output.name)[0]
                key = (benchmark, normalized["qa_id"])
                if key in scored:
                    raise ValueError("Duplicate checkpoint score")
                scored[key] = normalized
        values = []
        for name, config in manifest["benchmarks"].items():
            for ident in config["items"]:
                values.append(scored.get((name, ident), {"benchmark": name, "qa_id": ident, "method": method,
                    "run_id": output.name, "contract_sha256": digest, "metric": config["primary_metric"],
                    "status": "unscored", "score": None}))
        write_rows(output / f"{method}.scores.jsonl", values)
        summaries[method] = summarize(manifest, values)
        reliability[method] = reliability_report(manifest, values)
        atomic_json(output / f"{method}.summary.json", summaries[method])
        atomic_json(output / f"{method}.reliability.json", reliability[method])
    complete = all(s["complete"] for s in summaries.values())
    state = {"status": "complete" if complete else "running" if active else "incomplete",
        "pid": os.getpid(), "active": active, "completed_jobs": sum(t["status"] == "complete" for t in tasks.values()),
        "failed_jobs": sum(t["status"] == "failed" for t in tasks.values()), "tasks": tasks,
        "summaries": summaries, "updated_unix_seconds": time.time(), "official_aml_comparable": False}
    atomic_json(output / "progress.json", state)
    atomic_json(output / "report.json", {key: state[key] for key in ("status", "summaries", "completed_jobs", "failed_jobs", "active")})
    return state


def execute_task(prepared, output, manifest, method, case, retry_failed=False):
    runtime = manifest["runtime"]
    folder = output / method / case["benchmark"] / case["key"]
    folder.mkdir(parents=True, exist_ok=True)
    queries = [Query(**q) for q in rows(prepared / case["queries"])]
    expected = {q.qa_id for q in queries}
    client = None
    try:
        checkpoints = read_json(folder / "checkpoint.json") if (folder / "checkpoint.json").exists() else {}
        for relative, digest in checkpoints.get("files", {}).items():
            if file_hash(folder / relative) != digest:
                raise ValueError("Completed stage checkpoint changed")
        if not checkpoints.get("memory_complete"):
            existing = sorted(folder.glob("memory_attempt_*"))
            if existing and not retry_failed:
                raise ValueError("Failed memory attempt requires explicit retry_failed")
            stage = folder / f"memory_attempt_{len(existing) + 1:03d}"
            stage.mkdir()
            job = {"method": method, "method_config": manifest["methods"][method]["config"],
                "runtime": runtime, "offline": False, "provider_identity": manifest["execution"]["provider_identity"],
                "scope": f"{output.name}:{method}:{case['benchmark']}:{case['key']}:{stage.name}",
                "history": str(prepared / case["history"]), "queries": str(prepared / case["queries"]),
                "input_hashes": {field: manifest["inputs"][case[field]] for field in ("history", "queries")},
                "output": str(stage)}
            write_json(stage / "job.json", job)
            env = {**os.environ, "PYTHONPATH": str(output / "frozen_framework/src"), "PYTHONDONTWRITEBYTECODE": "1"}
            with (stage / "worker.log").open("w") as log:
                process = subprocess.run([manifest["methods"][method]["config"]["python"],
                    "-m", "memory_benchmark.runtime.worker", str(stage / "job.json")], env=env,
                    stdout=log, stderr=subprocess.STDOUT, timeout=runtime["worker_timeout_seconds"])
            worker = read_json(stage / "report.json") if (stage / "report.json").exists() else {}
            if process.returncode or worker.get("status") != "complete":
                return {"status": "failed", "phase": "memory", "error_type": worker.get("error_type", "WorkerProcessExit"),
                        "returncode": process.returncode, "worker_report": str(stage / "report.json")}
            retrievals = records(stage / "retrievals.jsonl")
            if len(retrievals) != len(expected) or {r["id"] for r in retrievals} != expected:
                raise ValueError("Retrieval question set differs from frozen set")
            shutil.copyfile(stage / "retrievals.jsonl", folder / "retrievals.jsonl")
            checkpoints = {"memory_complete": True, "files": {"retrievals.jsonl": file_hash(folder / "retrievals.jsonl")}}
            atomic_json(folder / "checkpoint.json", checkpoints)
        retrievals = {r["id"]: r["evidence"] for r in rows(folder / "retrievals.jsonl")}
        completed_answers = {r["id"]: r for r in records(folder / "answers.jsonl")}
        native_scores = {r["id"]: r for r in records(folder / "native_scores.jsonl")}
        if len(completed_answers) != len(records(folder / "answers.jsonl")) or len(native_scores) != len(records(folder / "native_scores.jsonl")):
            raise ValueError("Duplicate persisted question")
        if set(completed_answers) - expected or set(native_scores) - expected:
            raise ValueError("Foreign persisted question")
        client = Gateway(runtime, folder / f"reader_usage_{time.time_ns()}.json",
                         expected_identity=manifest["execution"]["provider_identity"])
        history = History.from_wire(read_json(prepared / case["history"]))
        tokenizer = GPTTokenizer(runtime["model"])
        if tokenizer.name != runtime["reader_tokenizer"]:
            raise ValueError("Tokenizer differs from frozen contract")
        for query in queries:
            if query.qa_id not in completed_answers:
                value = answer(query, retrievals[query.qa_id], history.speakers, client, tokenizer, runtime)
                append_row(folder / "answers.jsonl", value)
                completed_answers[query.qa_id] = value
                checkpoints["files"]["answers.jsonl"] = file_hash(folder / "answers.jsonl")
                atomic_json(folder / "checkpoint.json", checkpoints)
        # No target is read before every answer of this isolated job has been persisted.
        targets = {r["qa_id"]: Target(**r) for r in rows(prepared / case["targets"])}
        for query in queries:
            if query.qa_id not in native_scores:
                value = evaluate(query, targets[query.qa_id], completed_answers[query.qa_id], client, runtime)
                import_native(manifest, [value], case["benchmark"], method, output.name)
                append_row(folder / "native_scores.jsonl", value)
                checkpoints["files"]["native_scores.jsonl"] = file_hash(folder / "native_scores.jsonl")
                atomic_json(folder / "checkpoint.json", checkpoints)
        return {"status": "complete", "questions": len(queries), "folder": str(folder)}
    except Exception as error:
        return {"status": "failed", "error_type": type(error).__name__,
                "stack": [{"file": f.filename, "line": f.lineno, "function": f.name}
                          for f in traceback.extract_tb(error.__traceback__)]}
    finally:
        if client:
            client.close()


def run_queue(prepared, methods, output, *, resume=False, retry_failed=False, max_jobs=None):
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    if not resume:
        initialize(prepared, methods, output)
        # Execute the coordinator itself from the same immutable copy as its workers.
        env = {**os.environ, "PYTHONPATH": str(output / "frozen_framework/src"), "PYTHONDONTWRITEBYTECODE": "1"}
        command = [sys.executable, "-m", "memory_benchmark.runtime.queue", "--prepared", str(prepared),
                   "--methods-config", str(output / "methods.frozen.json"), "--output", str(output), "--resume"]
        write_json(output / "methods.frozen.json", methods)
        if max_jobs:
            command += ["--max-jobs", str(max_jobs)]
        os.execvpe(command[0], command, env)
    manifest = check_manifest(output, prepared, methods)
    lock = (output / "queue.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    tasks = read_json(output / "progress.json")["tasks"] if (output / "progress.json").exists() else {}
    completed = 0
    for method, case in ordered_jobs(prepared, methods):
        ident = ":".join((method, case["benchmark"], case["key"]))
        previous = tasks.get(ident, {})
        if previous.get("status") == "complete":
            continue
        if previous.get("status") == "failed" and not retry_failed:
            continue
        active = {"method": method, "benchmark": case["benchmark"], "episode_id": case["episode_id"], "job_id": ident}
        publish(output, manifest, tasks, active)
        print(f"start {method} {case['benchmark']} {case['episode_id']}", flush=True)
        result = execute_task(prepared, output, manifest, method, case, retry_failed=retry_failed)
        tasks[ident] = {**result, "finished_unix_seconds": time.time()}
        atomic_json(output / "progress.json", {"tasks": tasks, "active": None, "status": "running", "pid": os.getpid()})
        print(f"finish {method} {case['benchmark']} {result['status']}", flush=True)
        publish(output, manifest, tasks)
        completed += 1
        if max_jobs and completed >= max_jobs:
            break
    return publish(output, manifest, tasks)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--methods-config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-jobs", type=int)
    args = parser.parse_args()
    state = run_queue(args.prepared, read_json(args.methods_config), args.output,
                      resume=args.resume, retry_failed=args.retry_failed, max_jobs=args.max_jobs)
    return 0 if state["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
