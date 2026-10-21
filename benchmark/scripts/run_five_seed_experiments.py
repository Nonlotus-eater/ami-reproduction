"""Run selected methods on shared frozen seeds with bounded concurrency.

Each queue freezes its framework/adapter, saves answers before judging, and
resumes successful stages. Uniform recovery only retries unfinished stages;
there is no selection by score. Progress and final statistics are local files.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

from memory_benchmark.pipeline.prepare import native_identity, verify_prepared
from memory_benchmark.runtime.artifacts import file_hash

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("activememoryindex", "mem0", "invmem", "refind")
LABELS = dict(zip(METHODS, ["AMI", "Mem0", "InvMem", "ReFind"]))
SEEDS = (17, 18, 19, 20, 21)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def process_token(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def same_process(pid, expected_token):
    return expected_token is not None and process_token(pid) == expected_token


class AdoptedQueue:
    """Observe an existing queue without restarting it or inventing an exit code."""
    def __init__(self, pid, token):
        self.pid, self.token, self.returncode = pid, token, None

    def poll(self):
        if same_process(self.pid, self.token):
            return None
        self.returncode = 'unknown-adopted-process'
        return self.returncode


def validate_resume_plan(previous, current, resize=False):
    previous, current = (json.loads(json.dumps(p)) for p in (previous, current))
    if resize:
        for field in ('parallel', 'parallel_per_method'):
            previous.pop(field, None)
            current.pop(field, None)
    if previous != current:
        raise ValueError('Frozen launch plan changed')


def recover_queues(state, output, *, adopt=False, max_passes=3):
    """The exclusive supervisor lock must be held before adopting any queue."""
    jobs = {}
    for key, task in state.items():
        if task.get('status') != 'running':
            continue
        if same_process(task.get('pid'), task.get('process_token')):
            if not adopt:
                raise ValueError('Existing queue still running; use --adopt-running for supervisor handoff')
            jobs[key] = AdoptedQueue(task['pid'], task['process_token']), None
            task['adopted_at'] = now()
        else:
            complete = round_summary(output / task['method'] / f"seed-{task['seed']}", task['method']).get('complete', False)
            task.update(status='complete' if complete else 'pending' if task['attempts'] < max_passes else 'incomplete',
                        next_start=0)
    return jobs


def round_summary(folder, method):
    summary = read(folder / f"{method}.summary.json", {})
    groups = summary.get("capabilities", {})
    scores = [groups.get(family, {}).get("score") for family in ("A", "B", "C", "D", "E", "G", "H")]
    return {**summary, "local_overall_macro_capability": statistics.mean(scores)
            if summary.get("complete") and all(v is not None for v in scores) else None}


def export(output, state, expected, limits=None, *, methods=METHODS):
    rounds = {m: {} for m in methods}
    for method in methods:
        for seed in SEEDS:
            rounds[method][str(seed)] = round_summary(output / method / f"seed-{seed}", method)
    complete = sum(v.get("complete", False) for r in rounds.values() for v in r.values())
    active = [key for key, v in state.items() if v["status"] == "running"]
    terminal = all(v["status"] in {"complete", "incomplete", "failed"} for v in state.values())
    total_rounds = len(methods) * len(SEEDS)
    status = "complete" if complete == total_rounds else "incomplete" if terminal else "running"
    progress = {"status": status, "pid": os.getpid(), "updated_at": now(),
                "active": active, "active_count": len(active), "completed_rounds": complete,
                "total_rounds": total_rounds, "expected_scores": len(methods) * sum(expected.values()),
                "scored": sum(v.get("scored", 0) for r in rounds.values() for v in r.values()),
                "tasks": state, "rounds": rounds}
    if limits:
        progress['execution_limits'] = limits
    write(output / "progress.json", progress)
    overall = {}
    for method, r in rounds.items():
        ok = all(v.get("complete") for v in r.values())
        overall[method] = {"method": LABELS[method], "complete": ok,
                           "expected": sum(expected.values()), "scored": sum(v.get("scored", 0) for v in r.values())}
        for scheme in ["benchmark", "capability"]:
            values = [v.get(f"local_overall_macro_{scheme}") for v in r.values()]
            overall[method][f"overall_macro_{scheme}"] = {
                "mean": statistics.mean(values), "sample_sd": statistics.stdev(values),
                "sample_variance": statistics.variance(values), "rounds": values,
            } if ok and all(v is not None for v in values) else None
    write(output / "summary.json", {"status": status, "updated_at": progress["updated_at"],
                                     "primary_overall": "seven capability families equally weighted per seed; five seeds equally weighted",
                                     "overall": overall, "rounds": rounds})
    with (output / "overall_comparison.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["method", "complete", "expected", "scored", "capability_mean", "capability_sample_sd", "dataset_mean", "dataset_sample_sd"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for method, item in overall.items():
            cap = item["overall_macro_capability"] or {}
            dataset = item["overall_macro_benchmark"] or {}
            writer.writerow(dict(zip(fields, [LABELS[method], item["complete"], item["expected"], item["scored"],
                                               cap.get("mean"), cap.get("sample_sd"), dataset.get("mean"), dataset.get("sample_sd")])))
    lines = ["# 方法五轮实验", "", f"状态：{status}；更新：{progress['updated_at']}。",
             f"已评分 {progress['scored']}/{progress['expected_scores']}；完整方法/seed 组合 {complete}/{total_rounds}；当前并发 {len(active)}。", "",
             "运行方法：" + "、".join(LABELS[m] for m in methods) + "。",
             "seed 17–21 共用冻结输入；各方法独立冷启动。实际题目与历史数量以本次 prepared manifest 为准。",
             "抽样策略见输入清单；检索、Reader、Answer、Judge、超时等运行条件以本次冻结 runtime 为准。",
             "每轮先计算七能力大类等权 Overall，再对五轮取均值与样本标准差；六数据集等权作为对照。未完整时不发布替代 Overall。", "",
             "| 方法 | 已评分/总题次 | 完整轮数 | 七能力 Overall | 六数据集对照 |", "|---|---:|---:|---:|---:|"]
    for method, item in overall.items():
        def formatted(value):
            return f"{value['mean']:.2f} ± {value['sample_sd']:.2f}" if value else "—"
        lines.append(f"| {LABELS[method]} | {item['scored']}/{item['expected']} | {sum(v.get('complete', False) for v in rounds[method].values())}/5 | {formatted(item['overall_macro_capability'])} | {formatted(item['overall_macro_benchmark'])} |")
    lines += ["", "每个方法/seed 的队列执行次数上限见冻结启动计划；仅重试失败或未完成阶段，已有答案与有效评分不重生成，不按高分挑选。",
              "[进度](progress.json) · [冻结启动计划](launch_plan.json) · [汇总](summary.json) · [两口径 CSV](overall_comparison.csv)", ""]
    temp = output / "report.md.tmp"
    temp.write_text("\n".join(lines))
    temp.replace(output / "report.md")
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--method", choices=METHODS, action="append",
                        help="Select a method; repeat to select several (default: all four)")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument("--parallel-per-method", type=int, default=2)
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--adopt-running", action="store_true",
                        help="Resume only the supervisor, preserving live queue PIDs and attempt counts")
    parser.add_argument("--resize-concurrency", action="store_true",
                        help="Record a scheduling-only concurrency override; all frozen inputs must still match")
    args = parser.parse_args()
    if min(args.parallel, args.parallel_per_method, args.passes) < 1:
        parser.error("Concurrency and passes must be positive")
    if (args.adopt_running or args.resize_concurrency) and not args.resume:
        parser.error('Adoption and concurrency changes require --resume')
    requested = args.method or METHODS
    if len(set(requested)) != len(requested):
        parser.error('Select methods without duplicates')
    selected = tuple(method for method in METHODS if method in requested)
    output, config, prepared = (p.resolve() for p in [args.output, args.config, args.prepared])
    output.mkdir(parents=True, exist_ok=True)
    lock = (output / "supervisor.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    available = read(config / "methods.json")
    if not isinstance(available, dict) or set(selected) - set(available):
        parser.error('Selected methods must be present in methods.json')
    methods = {method: available[method] for method in selected}
    manifests = {seed: verify_prepared(prepared / f"seed-{seed}") for seed in SEEDS}
    expected = {seed: sum(len(b["items"]) for b in m["benchmarks"].values()) for seed, m in manifests.items()}
    hashes = {str(p): file_hash(p) for p in [*config.glob("*.json"),
              *(prepared / f"seed-{s}" / "manifest.json" for s in SEEDS),
              *Path(ROOT / "src/memory_benchmark/runtime/native_bridge").glob("*.py"),
              *(Path(methods[m]["adapter_file"]) for m in ["invmem", "refind"] if m in methods)]}
    plan = {"methods": methods, "seeds": list(SEEDS), "prepared": str(prepared), "config": str(config),
            "expected_per_seed": expected, "parallel": args.parallel, "parallel_per_method": args.parallel_per_method,
            "max_queue_passes": args.passes, "file_sha256": hashes,
            "native_identity": {m: native_identity(c) for m, c in methods.items()},
            "retry_policy": "only unfinished stages; never replace a saved valid answer/score"}
    if args.resume:
        previous = read(output / "launch_plan.json")
        validate_resume_plan(previous, plan, args.resize_concurrency)
        state = read(output / "progress.json")["tasks"]
        jobs = recover_queues(state, output, adopt=args.adopt_running, max_passes=args.passes)
        if args.resize_concurrency:
            previous_supervisor = read(output / 'supervisor.json', {})
            event = {'timestamp': now(), 'pid': os.getpid(), 'adopted_tasks': list(jobs),
                     'old_parallel': previous_supervisor.get('parallel'), 'parallel': args.parallel,
                     'old_parallel_per_method': previous_supervisor.get('parallel_per_method'),
                     'parallel_per_method': args.parallel_per_method,
                     'initial_plan_preserved': True, 'max_queue_passes': args.passes,
                     'script_sha256': file_hash(Path(__file__))}
            with (output / 'scheduling_changes.jsonl').open('a') as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        shutil.copyfile(__file__, output / f"run_five_seed_experiments.resumed-{time.time_ns()}.py")
    else:
        if (output / "launch_plan.json").exists():
            raise ValueError("Use --resume for an existing experiment")
        write(output / "launch_plan.json", plan)
        shutil.copyfile(__file__, output / "run_five_seed_experiments.executed.py")
        state = {f"{method}:seed-{seed}": {"method": method, "seed": seed, "attempts": 0,
                 "status": "pending", "next_start": 0} for seed in SEEDS for method in methods}
        jobs = {}
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "TOKENIZERS_PARALLELISM": "false"}
    write(output / "supervisor.json", {"pid": os.getpid(), "process_token": process_token(os.getpid()), "started_at": now(),
                                      "parallel": args.parallel, "parallel_per_method": args.parallel_per_method})
    while True:
        for key, (child, log) in list(jobs.items()):
            if child.poll() is None:
                continue
            if log is not None:
                log.close()
            del jobs[key]
            task = state[key]
            folder = output / task["method"] / f"seed-{task['seed']}"
            complete = round_summary(folder, task["method"]).get("complete", False)
            task.update(returncode=child.returncode, finished_at=now(),
                        status="complete" if complete else "pending" if task["attempts"] < args.passes else "incomplete",
                        next_start=time.time() + 60)
            print(json.dumps({"task": key, "event": "queue_exit", "status": task["status"], "returncode": child.returncode}), flush=True)
        counts = Counter(state[key]["method"] for key in jobs)
        for key, task in sorted(state.items(), key=lambda item: (item[1]["attempts"], item[1]["seed"], METHODS.index(item[1]["method"]))):
            if len(jobs) >= args.parallel:
                break
            method, seed = task["method"], task["seed"]
            if task["status"] != "pending" or task["next_start"] > time.time() or counts[method] >= args.parallel_per_method:
                continue
            for path, expected_hash in hashes.items():
                if file_hash(path) != expected_hash:
                    raise ValueError("Frozen experiment input changed")
            folder = output / method / f"seed-{seed}"
            folder.parent.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, "-u"]
            command += ["-m", "memory_benchmark.runtime.native_bridge.runner" if method in {"invmem", "refind"} else "memory_benchmark.runtime.queue"]
            command += ["--prepared", str(prepared / f"seed-{seed}"), "--methods-config", str(config / f"methods.{method}.json"), "--output", str(folder)]
            if method in {"invmem", "refind"}:
                command += ["--policy", str(config / "embedding_policy.json")]
            if (folder / "manifest.json").exists():
                command += ["--resume", "--retry-failed"]
            task["attempts"] += 1
            log_path = output / f"{method}-seed-{seed}-pass-{task['attempts']:02d}.log"
            log = log_path.open("a")
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            jobs[key] = child, log
            counts[method] += 1
            task.update(status="running", pid=child.pid, process_token=process_token(child.pid), started_at=now(),
                        command=command, log=str(log_path))
            print(json.dumps({"task": key, "event": "queue_start", "pid": child.pid, "pass": task["attempts"]}), flush=True)
        progress = export(output, state, expected,
                          limits={'parallel': args.parallel, 'parallel_per_method': args.parallel_per_method,
                                  'max_queue_passes': args.passes}, methods=tuple(methods))
        if not jobs and all(t["status"] != "pending" for t in state.values()):
            write(output / "acceptance.json", {"status": "passed" if progress["status"] == "complete" else "incomplete",
                                               "finished_at": now(), "scored": progress["scored"], "expected": progress["expected_scores"]})
            return 0 if progress["status"] == "complete" else 1
        time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
