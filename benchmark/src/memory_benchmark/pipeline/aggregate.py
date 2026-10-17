from __future__ import annotations
from collections import Counter
from statistics import mean
import math
from ..runtime.artifacts import contract_hash

FAMILIES = ("A", "B", "C", "D", "E", "G", "H")
LEAVES = {
    "A": ("A1", "A2", "A3"), "B": ("B1", "B2", "B3", "B4"),
    "C": ("C1", "C2", "C3"), "D": ("D1", "D2", "D3", "F1"),
    "E": ("E1", "E2", "E3"), "G": ("G1", "G2", "G3", "G4", "G5"),
    "H": ("H1", "H2"),
}
LEAF_FAMILY = {leaf: family for family, leaves in LEAVES.items() for leaf in leaves}
METRIC_FIELDS = {
    "judged_accuracy": "is_correct", "mcq_accuracy": "is_correct",
    "strict_accuracy": "rubric_clbench_score", "rubric_mean": "llm_judge_score",
    "preference_adherence": "score", "exact_match_accuracy": "score",
}

def validate_manifest(manifest):
    if manifest.get("aggregation") != "macro_benchmark":
        raise ValueError("aggregation must be macro_benchmark")
    if not manifest.get("suite_id") or manifest.get("scope") not in {"pilot", "expanded"}:
        raise ValueError("suite_id and pilot/expanded scope are required")
    benchmarks = manifest.get("benchmarks")
    if not isinstance(benchmarks, dict) or not benchmarks:
        raise ValueError("a nonempty frozen benchmark suite is required")
    for name, benchmark in benchmarks.items():
        if benchmark.get("primary_metric") not in METRIC_FIELDS:
            raise ValueError(f"unsupported primary_metric for {name}")
        items = benchmark.get("items")
        if not isinstance(items, dict) or not items:
            raise ValueError(f"{name}: freeze nonempty question IDs before scoring")
        for ident, item in items.items():
            if not isinstance(ident, str) or not ident or not isinstance(item, dict):
                raise ValueError("question IDs must be nonempty strings with object metadata")
            leaves = item.get("capability_leaves", [])
            if not isinstance(leaves, list) or len(set(leaves)) != len(leaves):
                raise ValueError(f"{ident}: capability_leaves must be a unique list")
            if any(leaf not in LEAF_FAMILY for leaf in leaves):
                raise ValueError(f"{ident}: unknown capability leaf")
    return manifest

def unit_score(value, *, binary=False):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("scores must be finite numbers in [0,1]")
    score = float(value)
    if binary and score not in {0.0, 1.0}:
        raise ValueError("accuracy metrics must use binary per-question scores")
    return score

def import_native(manifest, native, benchmark, method, run_id):
    validate_manifest(manifest)
    if benchmark not in manifest["benchmarks"]:
        raise ValueError("benchmark is outside the frozen suite")
    config = manifest["benchmarks"][benchmark]
    metric = config["primary_metric"]
    digest = contract_hash(manifest)
    seen = set()
    output = []
    for row in native:
        # AML CLBench emits `idx`; other public scorers emit `id`.
        identifiers = [row[key] for key in ("id", "qa_id", "idx") if row.get(key) is not None]
        if not identifiers or any(ident != identifiers[0] for ident in identifiers[1:]):
            raise ValueError("missing or conflicting native question IDs")
        ident = identifiers[0]
        if not isinstance(ident, str) or ident not in config["items"]:
            raise ValueError(f"unknown or invalid question ID: {ident}")
        if ident in seen:
            raise ValueError(f"duplicate question ID: {ident}")
        seen.add(ident)
        field = METRIC_FIELDS[metric]
        if field in row:
            value = row[field]
        elif metric == "judged_accuracy" and row.get("label") in {"CORRECT", "WRONG"}:
            value = int(row["label"] == "CORRECT")
        else:
            raise ValueError(f"{ident}: missing native {field}; lexical F1 is not judged accuracy")
        score = unit_score(value, binary=metric.endswith("accuracy"))
        if metric == "judged_accuracy" and "label" in row:
            label = row["label"]
            if label not in {"CORRECT", "WRONG"} or score != float(label == "CORRECT"):
                raise ValueError(f"{ident}: inconsistent judge label and is_correct")
        output.append({"benchmark": benchmark, "qa_id": ident, "method": method,
                       "run_id": run_id, "contract_sha256": digest, "metric": metric,
                       "status": "scored", "score": score})
    return output

def summarize(manifest, scores):
    validate_manifest(manifest)
    digest = contract_hash(manifest)
    expected = {(name, ident) for name, b in manifest["benchmarks"].items() for ident in b["items"]}
    observed = {}
    identity = set()
    for row in scores:
        key = (row.get("benchmark"), row.get("qa_id"))
        if key not in expected or key in observed:
            raise ValueError(f"unknown or duplicate scored item: {key}")
        if row.get("contract_sha256") != digest:
            raise ValueError("score belongs to a different frozen contract")
        if not row.get("method") or not row.get("run_id"):
            raise ValueError("method and run_id are required")
        identity.add((row["method"], row["run_id"]))
        metric = manifest["benchmarks"][key[0]]["primary_metric"]
        if row.get("metric") != metric:
            raise ValueError("primary metric mismatch")
        status = row.get("status")
        if status == "scored":
            value = unit_score(row.get("score"), binary=metric.endswith("accuracy"))
        elif status == "unscored" and row.get("score") is None:
            value = None
        else:
            raise ValueError("status must be scored, or unscored with null score")
        observed[key] = value
    if len(identity) > 1:
        raise ValueError("aggregate one method and one run at a time")
    benchmark_scores = {}
    family_values = {name: [] for name in FAMILIES}
    leaf_values = {name: [] for name in LEAF_FAMILY}
    family_expected = Counter()
    leaf_expected = Counter()
    missing = []
    for name, benchmark in manifest["benchmarks"].items():
        values = []
        for ident, item in benchmark["items"].items():
            value = observed.get((name, ident))
            item_leaves = item.get("capability_leaves", [])
            item_families = {LEAF_FAMILY[leaf] for leaf in item_leaves}
            family_expected.update(item_families)
            leaf_expected.update(item_leaves)
            if value is None:
                missing.append({"benchmark": name, "qa_id": ident})
                continue
            values.append(value)
            for leaf in item_leaves:
                leaf_values[leaf].append(value)
            for family in item_families:
                family_values[family].append(value)
        complete = len(values) == len(benchmark["items"])
        benchmark_scores[name] = {"metric": benchmark["primary_metric"], "expected": len(benchmark["items"]),
                                 "scored": len(values), "complete": complete,
                                 "score": 100 * mean(values) if complete else None,
                                 "observed_score": 100 * mean(values) if values else None}
    complete = not missing
    def breakdown(values, counts):
        return {name: {"expected": counts[name], "scored": len(v),
                       "score": 100 * mean(v) if v and len(v) == counts[name] else None,
                       "observed_score": 100 * mean(v) if v else None}
                for name, v in values.items()}
    method, run_id = next(iter(identity), (None, None))
    return {"suite_id": manifest["suite_id"], "scope": manifest["scope"],
            "contract_sha256": digest, "method": method, "run_id": run_id,
            "official_aml_comparable": False, "complete": complete,
            "expected": len(expected), "scored": len(expected) - len(missing),
            "local_overall_macro_benchmark": mean([v["score"] for v in benchmark_scores.values()]) if complete else None,
            "benchmarks": benchmark_scores, "capabilities": breakdown(family_values, family_expected),
            "capability_leaves": breakdown(leaf_values, leaf_expected),
            "uncovered_leaves": [name for name in LEAF_FAMILY if not leaf_expected[name]],
            "missing_or_unscored": missing}
