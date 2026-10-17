import argparse
import json
from pathlib import Path

from .benchmarks.native_contract import ROOT
from .pipeline.aggregate import import_native, summarize
from .pipeline.prepare import prepare
from .pipeline.runner import run
from .runtime.artifacts import read_json, rows, write_json, write_rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog="memory-benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("demo", help="Bundled two-format offline example; no credentials or external datasets")
    p.add_argument("--output", required=True)
    p = sub.add_parser("prepare", help="Freeze data; no model calls")
    p.add_argument("--suite", default=str(ROOT / "configs/suites/text_v1.json"))
    p.add_argument("--runtime", default=str(ROOT / "configs/runtime.json"))
    p.add_argument("--output", required=True)
    p.add_argument("--benchmark", action="append", dest="benchmarks")
    p.add_argument("--episode", action="append", dest="episode_ids")
    p.add_argument("--question", action="append", dest="question_ids")
    for name in ("episode-limit", "session-limit", "question-limit"):
        p.add_argument("--" + name, type=int)
    p.add_argument("--require-supported", action="store_true")
    p = sub.add_parser("sample", help="Draw and freeze a capability subset; no model calls")
    p.add_argument("--bank", required=True, help="Preserved prepared question-bank directory")
    p.add_argument("--output", required=True)
    quota = p.add_mutually_exclusive_group()
    quota.add_argument("--max-per-capability", type=int)
    quota.add_argument("--max-per-family", type=int)
    p.add_argument("--beam-length", choices=["100K", "500K", "1M"])
    p.add_argument("--beam-histories", type=int)
    p.add_argument("--history-limit", action="append", default=[], metavar="DATASET=COUNT",
                   help="Retain only a few complete history instances for a dataset")
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--capability", action="append", dest="capabilities")
    p = sub.add_parser("run")
    p.add_argument("--prepared", required=True)
    p.add_argument("--methods-config", default=str(ROOT / "configs/methods.json"))
    p.add_argument("--method", action="append")
    p.add_argument("--output", required=True)
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--offline", action="store_true", help="Network-blocked fixtures, not native baselines")
    modes.add_argument("--execute", action="store_true", help="Execute native methods and real API calls")
    p = sub.add_parser("import-scores")
    for name in ("manifest", "native", "benchmark", "method", "run-id", "output"):
        p.add_argument("--" + name, required=True)
    p = sub.add_parser("aggregate")
    for name in ("manifest", "scores", "output"):
        p.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    if args.command == "demo":
        from .runtime.demo import demo
        result = demo(args.output)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "complete" else 1
    elif args.command == "sample":
        from .pipeline.sampling import sample
        per_family = args.max_per_capability is None
        maximum = args.max_per_family if args.max_per_family is not None else args.max_per_capability
        maximum = 20 if maximum is None else maximum
        if maximum <= 0 or (args.beam_histories is not None and args.beam_histories <= 0):
            parser.error("Quota and history count must be positive")
        history_limits = {}
        for value in args.history_limit:
            try:
                name, count = value.split("=", 1)
                count = int(count)
                if not name or count <= 0 or name in history_limits:
                    raise ValueError
            except ValueError:
                parser.error("Use unique --history-limit DATASET=POSITIVE_COUNT values")
            history_limits[name] = count
        result = sample(args.bank, args.output, maximum=maximum,
                        seed=args.seed, capabilities=args.capabilities, per_family=per_family,
                        beam_length=args.beam_length, beam_histories=args.beam_histories,
                        history_limits=history_limits)
    elif args.command == "prepare":
        selection = {name: getattr(args, name) for name in ("benchmarks", "episode_ids", "question_ids", "episode_limit",
                     "session_limit", "question_limit", "require_supported")}
        if any(selection[n] is not None and selection[n] <= 0 for n in ("episode_limit", "session_limit", "question_limit")):
            parser.error("Selection limits must be positive")
        result = prepare(read_json(args.suite), read_json(args.runtime), args.output, selection=selection)
    elif args.command == "run":
        methods = read_json(args.methods_config)
        if args.method:
            if len(set(args.method)) != len(args.method) or any(n not in methods for n in args.method):
                parser.error("Unknown or duplicate method selection")
            methods = {n: methods[n] for n in args.method}
        report = run(args.prepared, methods, args.output, offline=args.offline)
        result = {k: report[k] for k in ("status", "error_type", "offline_fixture", "estimated_cny", "calls")}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if report["status"] == "complete" else 1
    elif args.command == "import-scores":
        scores = import_native(read_json(args.manifest), list(rows(args.native)), args.benchmark, args.method, args.run_id)
        write_rows(args.output, scores)
        result = {"imported": len(scores)}
    else:
        result = summarize(read_json(args.manifest), list(rows(args.scores)))
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0
