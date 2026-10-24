"""Resume a running experiment with direct model networking and intact checkpoints.

The running code/config/data contracts stay unchanged. Pause coordinators,
require every output checkpoint to be consistent, then resume the original
launcher under an explicitly proxy-free environment. Never repair or delete
answer/score records to make a checkpoint pass.
"""
import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from zoneinfo import ZoneInfo

from memory_benchmark.runtime.artifacts import file_hash, read_json, rows, write_json
if __package__:
    from .enable_memory_checkpoints import prefix, process
else:
    from enable_memory_checkpoints import prefix, process

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
SHELL_POLICY = """# Explicit direct model networking; independent of local proxy services.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY='*' no_proxy='*'
"""


def direct_environment(environment):
    return {**{k: v for k, v in environment.items() if k not in PROXY_KEYS}, "NO_PROXY": "*", "no_proxy": "*"}


def checked_process(pid, token, experiment):
    item = process(pid)
    if item and item["state"] != "Z" and item["token"] == token:
        if str(experiment) not in item["command"]:
            raise ValueError("Recorded PID belongs to another command")
        return item


def pause(item):
    current = process(item["pid"])
    if not current or current["state"] == "Z" or current["token"] != item["token"]:
        return False
    if current["state"] in ("T", "t"):
        raise ValueError("A coordinator was already paused")
    os.kill(item["pid"], signal.SIGSTOP)
    deadline = time.monotonic() + 3
    while True:
        current = process(item["pid"])
        if not current or current["state"] == "Z":
            return False
        if current["state"] in ("T", "t"):
            return True
        if time.monotonic() > deadline:
            raise TimeoutError("Coordinator did not pause")
        time.sleep(.01)


def unpause(items):
    for item in reversed(items):
        current = process(item["pid"])
        if current and current["state"] != "Z" and current["token"] == item["token"]:
            os.kill(item["pid"], signal.SIGCONT)


def snapshot_outputs(results):
    checkpoints = list(results.glob("*/seed-*/*/*/*/checkpoint.json"))
    for path in checkpoints:
        checkpoint = read_json(path)
        if not checkpoint.get("memory_complete"):
            raise ValueError("This rollout requires all memory retrieval to be complete")
        for relative, digest in checkpoint["files"].items():
            target = path.parent / relative
            if Path(relative).name != relative or file_hash(target) != digest:
                raise ValueError("Checkpoint is between writes or has changed")
    files = {}
    for name in ("answers.jsonl", "native_scores.jsonl"):
        for path in results.rglob(name):
            values = list(rows(path))
            identifiers = [v["id"] for v in values]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("Duplicate persisted question")
            checkpoint = read_json(path.parent / "checkpoint.json")
            if checkpoint["files"].get(name) != file_hash(path):
                raise ValueError("Persisted output not yet checkpointed")
            files[str(path)] = {**prefix(path), "rows": len(values)}
    return files


def apply(experiment):
    experiment = Path(experiment).resolve()
    with (experiment / "direct_network_rollout.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        results = experiment / "results"
        plan = read_json(results / "launch_plan.json")
        frozen = plan["file_sha256"]
        for path, digest in frozen.items():
            if file_hash(path) != digest:
                raise ValueError("Frozen launch input changed")
        manifest_paths = list(results.glob("*/seed-*/manifest.json"))
        expected_histories = 0
        for path in manifest_paths:
            cases = sum(1 for _ in rows(Path(plan["prepared"]) / path.parent.name / "cases.jsonl"))
            expected_histories += cases
            complete = list(path.parent.glob("*/*/*/checkpoint.json"))
            if len(complete) != cases or any(not read_json(p).get("memory_complete") for p in complete):
                raise ValueError("Memory retrieval is still active; do not interrupt a memory worker")
        if len(manifest_paths) != len(plan["methods"]) * len(plan["seeds"]):
            raise ValueError("Expected all frozen queue manifests")
        meta = read_json(experiment / "experiment.json")
        supervisor = read_json(results / "supervisor.json")
        launcher = process(meta["launcher_pid"])
        if not launcher or str(experiment / "launch.sh") not in launcher["command"]:
            raise ValueError("Recorded launcher is no longer running")
        items, stopped, terminated = [launcher], [], False
        try:
            if pause(launcher):
                stopped.append(launcher)
            item = checked_process(supervisor["pid"], supervisor["process_token"], experiment)
            if not item:
                raise ValueError("Recorded supervisor is no longer running")
            items.append(item)
            if pause(item):
                stopped.append(item)
            progress = read_json(results / "progress.json")
            for task in progress["tasks"].values():
                if task.get("status") != "running":
                    continue
                item = checked_process(task["pid"], task["process_token"], experiment)
                if item:
                    items.append(item)
                    if pause(item):
                        stopped.append(item)
            # Refuse to interrupt an output/checkpoint write. The caller may
            # retry after all original processes have been resumed by finally.
            saved = snapshot_outputs(results)
            stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S%z")
            audit = experiment / ("direct-network-rollout-" + stamp)
            audit.mkdir(mode=0o700)
            write_json(audit / "preserved_outputs.json", saved)
            write_json(audit / "stopped_processes.json", stopped)
            for name in ("progress.json", "supervisor.json", "launch_plan.json"):
                shutil.copyfile(results / name, audit / name)
            shutil.copyfile(experiment / "launch.sh", audit / "launch.original.sh")
            for path in manifest_paths:
                value = read_json(path)
                for relative, digest in value["framework"]["files"].items():
                    if file_hash(path.parent / "frozen_framework" / relative) != digest:
                        raise ValueError("Frozen framework changed")
            # Every preserved record is fully written and matches its existing
            # checkpoint; in-flight unsaved model calls are simply unfinished.
            for item in reversed(stopped):
                os.kill(item["pid"], signal.SIGTERM)
                os.kill(item["pid"], signal.SIGCONT)
            terminated = True
            deadline = time.monotonic() + 10
            while any((current := process(i["pid"])) and current["state"] != "Z" and current["token"] == i["token"] for i in stopped):
                if time.monotonic() > deadline:
                    raise TimeoutError("Old experiment processes did not exit")
                time.sleep(.02)
            launcher_path = experiment / "launch.sh"
            body = launcher_path.read_text()
            if SHELL_POLICY not in body:
                first, rest = body.split("\n", 1)
                launcher_path.write_text(first + "\n" + SHELL_POLICY + rest)
            for path, fingerprint in saved.items():
                if prefix(Path(path)) != {k: fingerprint[k] for k in ("bytes", "sha256")}:
                    raise ValueError("Saved output changed during handoff")
            for path, digest in frozen.items():
                if file_hash(path) != digest:
                    raise ValueError("Frozen launch input changed")
            with (experiment / "supervisor.log").open("ab", buffering=0) as log:
                child = subprocess.Popen(["bash", str(launcher_path), "--resume"], cwd=experiment,
                    env=direct_environment(os.environ), stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
            receipt = {"status": "resumed_direct", "at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "launcher_pid": child.pid, "proxy_environment_removed": list(PROXY_KEYS),
                "NO_PROXY": "*", "preserved_answer_score_files": len(saved),
                "preserved_answer_rows": sum(v["rows"] for p, v in saved.items() if p.endswith("answers.jsonl")),
                "preserved_score_rows": sum(v["rows"] for p, v in saved.items() if p.endswith("native_scores.jsonl")),
                "completed_memory_histories_reused": expected_histories, "saved_outputs_unchanged": True,
                "frozen_framework_config_and_data_unchanged": True, "automatic_merge_preserved": True,
                "audit": str(audit)}
            write_json(audit / "activation_receipt.json", receipt)
            meta.update(status="running", launcher_pid=child.pid, network_policy="direct_no_environment_proxy",
                        network_policy_changed_at=receipt["at"], direct_network_audit=str(audit))
            write_json(experiment / "experiment.json", meta)
            with (experiment / "operations.jsonl").open("a") as handle:
                handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
            return receipt
        finally:
            if not terminated:
                unpause(stopped)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(apply(args.experiment), ensure_ascii=False, indent=2))
