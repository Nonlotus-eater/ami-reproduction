"""Offline quickstart using repository-owned synthetic histories."""
from pathlib import Path

from ..benchmarks.native_contract import ROOT
from ..pipeline.prepare import prepare
from ..pipeline.runner import run
from .artifacts import read_json, write_json


def demo(output):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    suite = read_json(ROOT / "configs/suites/smoke.json")
    for config in suite["benchmarks"].values():
        for key in ("data_dir", "data_file"):
            if key in config:
                config[key] = str(ROOT / config[key])
    runtime = read_json(ROOT / "configs/runtime.json")
    runtime["env_file"] = "unused-in-offline-demo"
    prepare(suite, runtime, root / "prepared")
    report = run(root / "prepared", {"fixture_reference": {}}, root / "run", offline=True)
    summary = report["summaries"]["fixture_reference"]
    valid = (report["status"] == "complete" and report["calls"] == {} and
             summary["scored"] == summary["expected"] == 3 and
             summary["diagnostic_fixture_macro"] == 0 and
             summary["local_overall_macro_benchmark"] is None)
    result = {"status": "complete" if valid else "failed", "checks_passed": valid,
              "purpose": "offline_fixture_not_baseline_performance",
              "cases": 2, "questions": 3, "api_calls": 0,
              "report": str(root / "run/report.json")}
    write_json(root / "demo.json", result)
    return result
