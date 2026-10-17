"""Pointwise uncertainty of local scores, with history clustering made explicit.

The target is an absolute 10 percentage point margin at 95% confidence.
Intervals describe generalization over source histories, not official AML scores.
"""
from collections import defaultdict
import math
from statistics import mean

Z95 = 1.959963984540054
# Two-sided 95% Student critical values, df 1..30.
T95 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
       2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
       2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def wilson(p, n):
    z2 = Z95 ** 2
    denominator = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denominator
    margin = Z95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n ** 2)) / denominator
    return max(0, center - margin), min(1, center + margin)


def interval(records, expected=None):
    """records are (cluster_id, score) pairs; unequal cluster sizes are allowed."""
    expected = len(records) if expected is None else expected
    if not records:
        return {"scored": 0, "expected": expected, "score": None, "ci95": None,
                "precision_status": "unscored", "intraclass_correlation": None}
    groups = defaultdict(list)
    for cluster, score in records:
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Invalid reliability score")
        groups[str(cluster)].append(score)
    n, k = len(records), len(groups)
    values = [score for _, score in records]
    p = mean(values)
    binary = all(v in {0, 1} for v in values)
    sizes = [len(v) for v in groups.values()]
    independent = all(size == 1 for size in sizes)
    ss_within = sum(sum((v - mean(g)) ** 2 for v in g) for g in groups.values())
    ss_between = sum(len(g) * (mean(g) - p) ** 2 for g in groups.values())
    icc = None
    effective_n = float(n)
    if k > 1 and n > k:
        msb, msw = ss_between / (k - 1), ss_within / (n - k)
        n0 = (n - sum(s ** 2 for s in sizes) / n) / (k - 1)
        denominator = msb + (n0 - 1) * msw
        if denominator > 0:
            icc = (msb - msw) / denominator
            design_effect = 1 + (sum(s ** 2 for s in sizes) / n - 1) * max(0, icc)
            effective_n = n / max(1, design_effect)
        else:
            # Constant observations do not establish independent trials.
            effective_n = n ** 2 / sum(s ** 2 for s in sizes)
    elif k == 1 and n > 1:
        effective_n = 1.0
    if independent and binary:
        low, high = wilson(p, n)
        method = "wilson_independent_binary"
    elif k > 1:
        # Cluster sandwich SE for the question-weighted mean, finite-cluster correction.
        se = math.sqrt(k / (k - 1) * sum((sum(g) - len(g) * p) ** 2 for g in groups.values())) / n
        df = k - 1
        critical = T95[df - 1] if df <= 30 else Z95 + (Z95 ** 3 + Z95) / (4 * df)
        low, high = max(0, p - critical * se), min(1, p + critical * se)
        # Boundary guards prevent spuriously zero-width intervals on constant scores.
        if binary or len(set(values)) == 1:
            guard_low, guard_high = wilson(p, effective_n)
            low, high = min(low, guard_low), max(high, guard_high)
        method = "cluster_sandwich_student_t_with_boundary_guard"
    else:
        low, high, method = 0.0, 1.0, "insufficient_independent_histories"
    margin = max(p - low, high - p)
    complete = n == expected
    status = "meets_target" if complete and k >= 30 and margin <= .1 else "precision_not_met"
    if not complete:
        status = "incomplete"
    elif k < 30:
        status = "few_independent_histories"
    return {"expected": expected, "scored": n, "complete": complete, "score": p * 100,
            "ci95": [low * 100, high * 100], "max_margin_pp": margin * 100,
            "ci_method": method, "source_clusters": k, "effective_n_diagnostic": effective_n,
            "intraclass_correlation": icc, "largest_cluster_questions": max(sizes),
            "precision_status": status, "target_margin_pp": 10, "minimum_clusters_policy": 30}


def report(manifest, scores):
    expected, observed = defaultdict(int), defaultdict(list)
    from .aggregate import LEAF_FAMILY
    for leaf in LEAF_FAMILY:
        expected[("local_capability_proxies", leaf)] = 0
    for name, config in manifest["benchmarks"].items():
        for ident, item in config["items"].items():
            keys = [("benchmarks", name), ("native_strata", name + ":" + item.get("stratum", "unknown"))]
            if item.get("length_split"):
                keys.append(("length_strata", name + ":" + item["length_split"] + ":" + item["stratum"]))
            keys += [("local_capability_proxies", leaf) for leaf in item.get("capability_leaves", [])]
            for key in keys:
                expected[key] += 1
    for row in scores:
        if row["status"] != "scored":
            continue
        name = row["benchmark"]
        item = manifest["benchmarks"][name]["items"][row["qa_id"]]
        cluster = name + ":" + item.get("cluster_id", item.get("source_unit_id", row["qa_id"]))
        keys = [("benchmarks", name), ("native_strata", name + ":" + item.get("stratum", "unknown"))]
        if item.get("length_split"):
            keys.append(("length_strata", name + ":" + item["length_split"] + ":" + item["stratum"]))
        keys += [("local_capability_proxies", leaf) for leaf in item.get("capability_leaves", [])]
        for key in keys:
            observed[key].append((cluster, row["score"]))
    result = {"confidence": .95, "intervals_are_pointwise_not_simultaneous": True,
              "correlation_assumption": "source clusters assumed independent; ICC measures questions within a cluster",
              "official_aml_comparable": False, "mapping": "local_proxies_not_official_question_annotations",
              "D3_H2_scope": "response_adherence_only; no storage-deletion or access-control evaluation",
              "selection_estimand": "fixed selected suite; no reweighting to full source distribution"}
    for section in ["benchmarks", "native_strata", "length_strata", "local_capability_proxies"]:
        result[section] = {name: interval(observed[(section, name)], count)
                           for (kind, name), count in expected.items() if kind == section}
    return result
