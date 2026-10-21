"""Shared orchestration across independent native method environments."""
from collections import Counter
from pathlib import Path
import math
import os
import shutil
import subprocess
import sys

from ..benchmarks.native_contract import ROOT
from ..runtime.artifacts import (append_row, contract_hash, file_hash, read_json,
                                 rows, write_json, write_rows)
from ..runtime.model_client import FixtureClient, Gateway
from ..runtime.profile import MODEL_OUTPUT_TOKEN_LIMITS, public_identity, read_profile, validate_profile
from ..schemas import History, Query, Target
from .aggregate import import_native, summarize
from .context import FixtureTokenizer, GPTTokenizer
from .evaluate import evaluate
from .prepare import native_identity, verify_prepared
from .reader import answer


def validate_runtime(runtime):
    for name in ("judge_max_tokens", "memory_default_max_tokens", "worker_timeout_seconds"):
        if type(runtime.get(name)) is not int or runtime[name] <= 0:
            raise ValueError(f"Invalid positive integer runtime field: {name}")
    for name in ("top_k", "reader_token_budget", "answer_input_token_limit"):
        if (name not in runtime or (runtime[name] is not None and
                (type(runtime[name]) is not int or runtime[name] <= 0))):
            raise ValueError(f"{name} must be a positive integer or null for no benchmark cap")
    answer_limit = runtime.get("answer_max_tokens")
    if ("answer_max_tokens" not in runtime or
            (answer_limit is not None and (type(answer_limit) is not int or answer_limit <= 0))):
        raise ValueError("answer_max_tokens must be a positive integer or null for the model maximum")
    if answer_limit is None and runtime.get("model") not in MODEL_OUTPUT_TOKEN_LIMITS:
        raise ValueError("Unknown model output ceiling for unrestricted Answer")
    for name in ("request_timeout_seconds",):
        value = runtime.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid positive runtime field: {name}")
    if runtime.get("answer_context_policy") not in {None, "model_window_v1", "model_window_v2"}:
        raise ValueError("Unknown answer context policy")
    if runtime.get("answer_part_truncation_policy") not in {None, "split_retry_v1", "split_retry_v2"}:
        raise ValueError("Unknown evidence truncation policy")
    from .ingestion import POLICY as ADD_POLICY
    if runtime.get("add_chunk_policy") not in {None, ADD_POLICY}:
        raise ValueError("Unknown Add chunk policy")
    from ..benchmarks.clbench_judge import JUDGE_PROTOCOL, STRICT_JUDGE_PROTOCOL
    if runtime.get("clbench_judge_protocol") not in {None, JUDGE_PROTOCOL, STRICT_JUDGE_PROTOCOL}:
        raise ValueError("Unknown CLBench Judge protocol")
    if "answer_min_output_tokens" in runtime:
        value = runtime["answer_min_output_tokens"]
        if type(value) is not int or not 1 <= value <= MODEL_OUTPUT_TOKEN_LIMITS[runtime["model"]]:
            raise ValueError("Invalid minimum Answer output space")
    if "judge_protocol_max_attempts" in runtime:
        value = runtime["judge_protocol_max_attempts"]
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError("Invalid Judge protocol attempt count")


def usage(root):
    calls, spent = Counter(), 0.0
    for path in Path(root).rglob("usage.json"):
        report = read_json(path)
        # A failed request retains its conservative reservation when actual usage is unknown.
        spent += max(report["reserved_cny"], report["estimated_cny"])
        calls.update(row["kind"] for row in report["calls"])
    return spent, calls


def run(prepared, methods, output, *, offline=False):
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    manifest = verify_prepared(prepared)
    runtime = manifest["runtime"]
    validate_runtime(runtime)
    if not offline:
        from ..methods.compatibility import validate_method_inputs
        validate_method_inputs(prepared, methods)
    if not methods:
        raise ValueError("Select at least one method")
    tokenizer = FixtureTokenizer() if offline else GPTTokenizer(runtime["model"])
    if not offline and tokenizer.name != runtime["reader_tokenizer"]:
        raise ValueError("Tokenizer differs from frozen contract")
    identity = None if offline else public_identity(validate_profile(read_profile(runtime["env_file"]), runtime["model"]), runtime)
    output.mkdir(parents=True, exist_ok=False)
    (output / "attempt.lock").open("x").close()
    manifest = {**manifest, "prepared_contract_sha256": contract_hash(manifest),
        "execution": {"run_id": output.name, "offline_fixture": offline,
            "provider_identity": identity, "actual_reader_tokenizer": tokenizer.name},
        "methods": {name: {"config": config, "native_code": {} if offline else native_identity(config)}
                    for name, config in methods.items()}}
    write_json(output / "manifest.json", manifest)
    # Workers execute this run's exact source copy, including fixed scoring sources.
    frozen_root = output / "frozen_framework"
    for relative in manifest["framework"]["files"]:
        source, target = ROOT / relative, frozen_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    cases = list(rows(prepared / "cases.jsonl"))
    status, error_type, client = "running", None, None
    worker_failures = []
    method_scores = {name: [] for name in methods}
    try:
        # Complete all memory retrieval before the shared answering and scoring stages.
        for name, config in methods.items():
            for case in cases:
                stage_output = output / name / case["benchmark"] / case["key"]
                stage_output.mkdir(parents=True)
                job = {"method": name, "method_config": config, "runtime": runtime,
                    "offline": offline, "provider_identity": identity,
                    "scope": f"{output.name}:{name}:{case['benchmark']}:{case['key']}",
                    "history": str(prepared / case["history"]), "queries": str(prepared / case["queries"]),
                    "input_hashes": {field: manifest["inputs"][case[field]] for field in ("history", "queries")},
                    "output": str(stage_output)}
                job_path = stage_output / "job.json"
                write_json(job_path, job)
                env = {**os.environ, "PYTHONPATH": str(frozen_root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
                executable = sys.executable if offline else config["python"]
                print(f"memory {name} {case['benchmark']} {case['episode_id']}", flush=True)
                with (stage_output / "worker.log").open("w") as log:
                    process = subprocess.run([executable, "-m", "memory_benchmark.runtime.worker", str(job_path)],
                        env=env, stdout=log, stderr=subprocess.STDOUT, timeout=runtime["worker_timeout_seconds"])
                report_path = stage_output / "report.json"
                if process.returncode or not report_path.exists() or read_json(report_path)["status"] != "complete":
                    worker_failures.append({"method": name, "benchmark": case['benchmark'],
                        "episode_id": case['episode_id'], "returncode": process.returncode,
                        "report": str(report_path)})
                    if report_path.exists():
                        worker_report = read_json(report_path)
                        if worker_report.get('status') == 'running':
                            worker_report.update(status='failed', error_type='WorkerProcessExit',
                                                 process_returncode=process.returncode)
                            write_json(report_path, worker_report)
                    raise RuntimeError("Memory worker failed; see worker report and log")
        client = FixtureClient() if offline else Gateway(runtime, output / "reader_judge" / "usage.json",
                                                       expected_identity=identity)
        # Answers are fully saved before any scoring target is loaded.
        for case in cases:
            history = History.from_wire(read_json(prepared / case["history"]))
            queries = [Query(**q) for q in rows(prepared / case["queries"])]
            for name in methods:
                folder = output / name / case["benchmark"] / case["key"]
                retrieved = {r["id"]: r["evidence"] for r in rows(folder / "retrievals.jsonl")}
                if set(retrieved) != {q.qa_id for q in queries}:
                    raise ValueError("Retrieval question set differs from frozen query set")
                for query in queries:
                    record = answer(query, retrieved[query.qa_id], history.speakers, client, tokenizer, runtime)
                    append_row(folder / "answers.jsonl", record)
        for case in cases:
            queries = {q["qa_id"]: Query(**q) for q in rows(prepared / case["queries"])}
            targets = {t["qa_id"]: Target(**t) for t in rows(prepared / case["targets"])}
            for name in methods:
                folder = output / name / case["benchmark"] / case["key"]
                for record in rows(folder / "answers.jsonl"):
                    native = evaluate(queries[record["id"]], targets[record["id"]], record, client, runtime)
                    append_row(folder / "native_scores.jsonl", native)
                    normalized = import_native(manifest, [native], case["benchmark"], name, output.name)[0]
                    method_scores[name].append(normalized)
                    append_row(output / f"{name}.scores.jsonl", normalized)
        status = "complete"
    except Exception as error:
        status, error_type = "failed", type(error).__name__
    finally:
        if client:
            client.close()
        summaries = {}
        for name, scores in method_scores.items():
            observed = {(s["benchmark"], s["qa_id"]) for s in scores}
            for benchmark, config in manifest["benchmarks"].items():
                for ident in config["items"]:
                    if (benchmark, ident) not in observed:
                        row = {"benchmark": benchmark, "qa_id": ident, "method": name, "run_id": output.name,
                            "contract_sha256": contract_hash(manifest), "metric": config["primary_metric"],
                            "status": "unscored", "score": None}
                        scores.append(row)
                        append_row(output / f"{name}.scores.jsonl", row)
            summary = summarize(manifest, scores)
            if offline:
                summary.update(purpose="offline_fixture_not_baseline_performance",
                    diagnostic_fixture_macro=summary["local_overall_macro_benchmark"], local_overall_macro_benchmark=None)
            summaries[name] = summary
            write_json(output / f"{name}.summary.json", summary)
        spent, calls = usage(output)
        report = {"status": status, "error_type": error_type, "offline_fixture": offline,
            "worker_failures": worker_failures,
            "contract_sha256": contract_hash(manifest), "estimated_cny": spent,
            "calls": dict(calls), "summaries": summaries}
        write_json(output / "report.json", report)
    return report
