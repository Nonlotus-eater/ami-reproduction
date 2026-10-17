"""Draw a reproducible capability subset from a preserved prepared question bank.

Sampling uses metadata only. Source histories and causal prefixes are copied
unchanged; answers, rubrics and method results never determine the selection.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import shutil

from ..benchmarks.native_contract import contract
from ..runtime.artifacts import contract_hash, file_hash, read_json, rows, write_json, write_rows
from .aggregate import FAMILIES, LEAVES, LEAF_FAMILY, validate_manifest
from .prepare import code_identity


def priority(seed, *parts):
    return hashlib.sha256(str(seed).encode() + b":" +
                          contract_hash(list(parts)).encode()).hexdigest()


def input_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Question-bank input leaves its prepared directory")
    return path


def verify_bank(bank):
    """Allow new orchestration code while requiring identical data semantics."""
    manifest = validate_manifest(read_json(bank / "manifest.json"))
    if contract_hash(manifest) != read_json(bank / "prepared.json")["contract_sha256"]:
        raise ValueError("Question-bank manifest changed")
    current = code_identity()
    for relative, digest in manifest["framework"]["files"].items():
        if (relative.startswith("src/memory_benchmark/benchmarks/") or
                relative.startswith("sources/") or relative == "src/memory_benchmark/schemas.py"):
            if current["files"].get(relative) != digest:
                raise ValueError("Question-bank schema, loader, mapping or scorer changed")
    for relative, digest in manifest["inputs"].items():
        if file_hash(input_path(bank, relative)) != digest:
            raise ValueError("Question-bank input changed")
    for name, config in manifest["benchmarks"].items():
        if contract(name).source_sha256 != config["native_scorer_sha256"]:
            raise ValueError("Question-bank native scoring contract changed")
        for path, digest in config["source_sha256"].items():
            if file_hash(path) != digest:
                raise ValueError("Question-bank source dataset changed")
    return manifest


def select(manifest, maximum=20, seed=17, capabilities=None):
    if type(maximum) is not int or maximum <= 0 or type(seed) is not int:
        raise ValueError("Use a positive integer cap and integer seed")
    leaves = list(LEAF_FAMILY) if capabilities is None else list(capabilities)
    if not leaves or len(set(leaves)) != len(leaves) or set(leaves) - set(LEAF_FAMILY):
        raise ValueError("Unknown or duplicate capability selection")
    candidates = defaultdict(list)
    for benchmark, config in manifest["benchmarks"].items():
        for ident, item in config["items"].items():
            for leaf in set(item.get("capability_leaves", [])) & set(leaves):
                candidates[leaf].append((benchmark, ident, item))
    chosen, counts = set(), Counter()
    for leaf in leaves:
        buckets = defaultdict(list)
        for benchmark, ident, item in candidates[leaf]:
            buckets[(benchmark, item.get("length_split") or "", item.get("stratum", "unknown"))].append(
                (benchmark, ident, item))
        while counts[leaf] < maximum:
            bucket_counts, cluster_counts = Counter(), Counter()
            for benchmark, ident, item in candidates[leaf]:
                if (benchmark, ident) in chosen:
                    bucket_counts[(benchmark, item.get("length_split") or "", item.get("stratum", "unknown"))] += 1
                    cluster_counts[(benchmark, item.get("cluster_id", item.get("source_unit_id", ident)))] += 1
            options = []
            for bucket, records in buckets.items():
                eligible = [(benchmark, ident, item) for benchmark, ident, item in records
                            if (benchmark, ident) not in chosen and
                            all(counts[tag] < maximum for tag in item.get("capability_leaves", []))]
                if not eligible:
                    continue
                # History priority is shared across leaves: this permits several
                # capabilities to reuse a build without concentrating one leaf.
                record = min(eligible, key=lambda r: (
                    cluster_counts[(r[0], r[2].get("cluster_id", r[2].get("source_unit_id", r[1])))],
                    priority(seed, "history", r[0], r[2].get("cluster_id", r[2].get("source_unit_id", r[1]))),
                    priority(seed, "question", r[0], r[1])))
                benchmark, ident, item = record
                cluster = (benchmark, item.get("cluster_id", item.get("source_unit_id", ident)))
                options.append(((cluster_counts[cluster] > 0, bucket_counts[bucket],
                                 priority(seed, "stratum", bucket[0], bucket[1]), bucket[2]), record))
            if not options:
                break
            _, (benchmark, ident, item) = min(options, key=lambda value: value[0])
            chosen.add((benchmark, ident))
            counts.update(item.get("capability_leaves", []))
    details = {}
    for leaf in leaves:
        selected = [(benchmark, ident, item) for benchmark, ident, item in candidates[leaf]
                    if (benchmark, ident) in chosen]
        details[leaf] = {"available": len(candidates[leaf]), "selected": len(selected),
            "source_clusters": len({(benchmark, item.get("cluster_id", item.get("source_unit_id", ident)))
                                    for benchmark, ident, item in selected}),
            "by_benchmark": dict(sorted(Counter(benchmark for benchmark, _, _ in selected).items())),
            "shortfall": max(0, maximum - len(selected))}
    if not chosen:
        raise ValueError("Selected capabilities contain no questions")
    return chosen, details


def restrict_beam(manifest, bank, *, length=None, histories=None, seed=17):
    if length is None and histories is None:
        return manifest, None
    if length not in {None, "100K", "500K", "1M"}:
        raise ValueError("Unknown BEAM length split")
    if histories is not None and (type(histories) is not int or histories <= 0):
        raise ValueError("BEAM history count must be positive")
    if "beam" not in manifest["benchmarks"]:
        raise ValueError("Question bank has no BEAM data")
    inventory = {}
    for ident, item in manifest["benchmarks"]["beam"]["items"].items():
        if length is not None and item.get("length_split") != length:
            continue
        entry = inventory.setdefault(item["source_unit_id"], {"strata": set(), "items": {}})
        entry["strata"].add(item["stratum"])
        entry["items"][ident] = item
    if not inventory:
        raise ValueError("No BEAM histories match the length selection")
    for case in rows(bank / "cases.jsonl"):
        if case["benchmark"] == "beam" and case["episode_id"] in inventory:
            history = read_json(bank / case["history"])
            inventory[case["episode_id"]]["body_chars"] = sum(
                len(m["text"]) for s in history["sessions"] for m in s["messages"])
    wanted = len(inventory) if histories is None else min(histories, len(inventory))
    selected, covered = [], set()
    while len(selected) < wanted:
        # Prefer additional native coverage, then smaller COMPLETE histories.
        # No rubric/gold or method outcome determines the selected sources.
        episode = min((e for e in inventory if e not in selected), key=lambda e: (
            -len(inventory[e]["strata"] - covered), inventory[e]["body_chars"],
            priority(seed, "beam-source", e)))
        selected.append(episode)
        covered.update(inventory[episode]["strata"])
    restricted = deepcopy(manifest)
    restricted["benchmarks"]["beam"]["items"] = {
        ident: item for episode in selected for ident, item in inventory[episode]["items"].items()}
    return restricted, {"length_split": length, "history_limit": histories,
        "selected_episodes": selected, "eligible_histories": len(inventory),
        "available_questions_in_selected_histories": len(restricted["benchmarks"]["beam"]["items"]),
        "available_native_strata": sorted(covered),
        "policy": "greedy-native-coverage-then-shortest-complete-history-v1",
        "questions_restricted_to_selected_histories": True, "history_truncated": False}


def restrict_histories(manifest, bank, history_limits=None, *, seed=17):
    limits = dict(history_limits or {})
    if set(limits) - set(manifest["benchmarks"]):
        raise ValueError("History limit names a dataset outside the question bank")
    if "beam" in limits:
        raise ValueError("Use beam_length/beam_histories for BEAM")
    if any(type(count) is not int or count <= 0 for count in limits.values()):
        raise ValueError("History limits must be positive integers")
    inventories = {name: {} for name in limits}
    for name in limits:
        for ident, item in manifest["benchmarks"][name]["items"].items():
            entry = inventories[name].setdefault(item["source_unit_id"], {"strata": set(), "items": {}})
            entry["strata"].add(item["stratum"])
            entry["items"][ident] = item
            entry["cluster_id"] = item.get("cluster_id", item["source_unit_id"])
    for case in rows(bank / "cases.jsonl"):
        name, episode = case["benchmark"], case["episode_id"]
        if name not in inventories or episode not in inventories[name]:
            continue
        history = read_json(bank / case["history"])
        entry = inventories[name][episode]
        entry["body_chars"] = sum(len(m["text"]) for s in history["sessions"] for m in s["messages"])
        # CLBench's source reference can be in the final query, so count it too.
        entry["max_query_chars"] = max((len(q["text"]) + len(q.get("all_options", ""))
                                        for q in rows(bank / case["queries"])), default=0)
    restricted, filters = deepcopy(manifest), {}
    for name, limit in limits.items():
        inventory = inventories[name]
        selected, covered, clusters = [], set(), set()
        while len(selected) < min(limit, len(inventory)):
            episode = min((e for e in inventory if e not in selected), key=lambda e: (
                -len(inventory[e]["strata"] - covered), inventory[e]["cluster_id"] in clusters,
                inventory[e]["body_chars"] + inventory[e]["max_query_chars"],
                priority(seed, "source", name, e)))
            selected.append(episode)
            covered.update(inventory[episode]["strata"])
            clusters.add(inventory[episode]["cluster_id"])
        restricted["benchmarks"][name]["items"] = {
            ident: item for episode in selected for ident, item in inventory[episode]["items"].items()}
        all_strata = {stratum for entry in inventory.values() for stratum in entry["strata"]}
        filters[name] = {"history_limit": limit, "eligible_histories": len(inventory),
            "selected_episodes": selected, "selected_source_clusters": len(clusters),
            "available_questions_in_selected_histories": len(restricted["benchmarks"][name]["items"]),
            "available_native_strata": sorted(covered), "uncovered_native_strata": sorted(all_strata - covered),
            "policy": "native-coverage-then-source-diversity-then-short-complete-input-v1",
            "questions_restricted_to_selected_histories": True, "history_truncated": False,
            "history_unit": {"longmemeval_s": "question-specific complete haystack",
                             "personamem_v1": "source context with its native causal prefix",
                             "clbench": "complete context/task instance"}.get(name, "complete conversation")}
    return restricted, filters


def select_families(manifest, maximum=20, seed=17, capabilities=None):
    if type(maximum) is not int or maximum <= 0 or type(seed) is not int:
        raise ValueError("Use a positive integer cap and integer seed")
    leaves = list(LEAF_FAMILY) if capabilities is None else list(capabilities)
    if not leaves or len(set(leaves)) != len(leaves) or set(leaves) - set(LEAF_FAMILY):
        raise ValueError("Unknown or duplicate capability selection")
    candidates = defaultdict(list)
    for benchmark, config in manifest["benchmarks"].items():
        for ident, item in config["items"].items():
            for leaf in set(item.get("capability_leaves", [])) & set(leaves):
                candidates[leaf].append((benchmark, ident, item))
    chosen, family_counts, leaf_counts = set(), Counter(), Counter()
    for family in FAMILIES:
        while family_counts[family] < maximum:
            possible = []
            for leaf in LEAVES[family]:
                if leaf not in leaves:
                    continue
                records = candidates[leaf]
                used_clusters, used_buckets = Counter(), Counter()
                for benchmark, ident, item in records:
                    if (benchmark, ident) in chosen:
                        used_clusters[(benchmark, item.get("cluster_id", item.get("source_unit_id", ident)))] += 1
                        used_buckets[(benchmark, item.get("length_split") or "", item.get("stratum", "unknown"))] += 1
                eligible = [(b, q, i) for b, q, i in records if (b, q) not in chosen and
                    all(family_counts[f] < maximum for f in {LEAF_FAMILY[tag] for tag in i.get("capability_leaves", [])})]
                if not eligible:
                    continue
                def rank(record):
                    benchmark, ident, item = record
                    cluster = (benchmark, item.get("cluster_id", item.get("source_unit_id", ident)))
                    bucket = (benchmark, item.get("length_split") or "", item.get("stratum", "unknown"))
                    return (used_clusters[cluster] > 0, used_buckets[bucket], used_clusters[cluster],
                            priority(seed, "history", *cluster), priority(seed, "question", benchmark, ident))
                record = min(eligible, key=rank)
                possible.append(((leaf_counts[leaf], priority(seed, "leaf", family, leaf)), record))
            if not possible:
                break
            _, (benchmark, ident, item) = min(possible, key=lambda option: option[0])
            chosen.add((benchmark, ident))
            leaf_counts.update(item.get("capability_leaves", []))
            family_counts.update({LEAF_FAMILY[tag] for tag in item.get("capability_leaves", [])})
    if not chosen:
        raise ValueError("Selected capabilities contain no questions")
    details, families = {}, {}
    for leaf in leaves:
        values = [(b, q, i) for b, q, i in candidates[leaf] if (b, q) in chosen]
        details[leaf] = {"available": len(candidates[leaf]), "selected": len(values),
            "source_clusters": len({(b, i.get("cluster_id", i.get("source_unit_id", q))) for b, q, i in values}),
            "by_benchmark": dict(sorted(Counter(b for b, _, _ in values).items())),
            "per_leaf_quota": None, "covered": bool(values)}
    for family in FAMILIES:
        all_records = {(b, q): i for leaf in LEAVES[family] for b, q, i in candidates[leaf]}
        values = [(b, q, i) for (b, q), i in all_records.items() if (b, q) in chosen]
        families[family] = {"available": len(all_records), "selected": len(values),
            "shortfall": max(0, maximum - len(values)),
            "source_clusters": len({(b, i.get("cluster_id", i.get("source_unit_id", q))) for b, q, i in values}),
            "by_benchmark": dict(sorted(Counter(b for b, _, _ in values).items())),
            "leaf_counts": {leaf: details[leaf]["selected"] for leaf in LEAVES[family] if leaf in details}}
    return chosen, details, families


def sample(bank, output, *, maximum=20, seed=17, capabilities=None,
           per_family=False, beam_length=None, beam_histories=None, history_limits=None):
    bank, output = Path(bank).resolve(), Path(output).resolve()
    parent = verify_bank(bank)
    eligible, beam_filter = restrict_beam(parent, bank, length=beam_length, histories=beam_histories, seed=seed)
    eligible, history_filters = restrict_histories(eligible, bank, history_limits, seed=seed)
    if per_family:
        chosen, details, family_details = select_families(eligible, maximum, seed, capabilities)
    else:
        chosen, details = select(eligible, maximum, seed, capabilities)
        family_details = None
    output.mkdir(parents=True, exist_ok=False)
    manifest = deepcopy(parent)
    mode = "family" if per_family else "capability"
    manifest.update(suite_id=f"{mode}-sample-{maximum}-seed-{seed}", scope="pilot",
        purpose="sampled_capability_diagnostics_not_official_aml_ranking",
        framework=code_identity(), inputs={})
    manifest["selection"] = {"sampling": "family-balanced-source-diverse-v1" if per_family else "stratified-source-diverse-with-shared-history-priority-v1",
        "seed": seed, "quota_unit": mode, f"max_questions_per_{mode}": maximum,
        "capabilities": list(details), "without_replacement_within_round": True,
        "beam_source_filter": beam_filter,
        "dataset_history_filters": history_filters,
        "score_blind": True, "history_truncated": False,
        "question_bank": str(bank), "question_bank_contract_sha256": contract_hash(parent),
        "question_bank_framework_sha256": parent["framework"]["sha256"]}
    index, audit, association_checks = [], {}, []
    for case in rows(bank / "cases.jsonl"):
        name = case["benchmark"]
        selected_ids = {ident for benchmark, ident in chosen if benchmark == name}
        queries = [q for q in rows(bank / case["queries"]) if q["qa_id"] in selected_ids]
        if not queries:
            continue
        ids = {q["qa_id"] for q in queries}
        targets = [t for t in rows(bank / case["targets"]) if t["qa_id"] in ids]
        if len(ids) != len(queries) or len(targets) != len(queries) or {t["qa_id"] for t in targets} != ids:
            raise ValueError("Sample query and target IDs differ")
        history = read_json(bank / case["history"])
        if history["benchmark"] != name or history["episode_id"] != case["episode_id"]:
            raise ValueError("History does not belong to its dataset/case")
        for query in queries:
            if query["benchmark"] != name or query["episode_id"] != history["episode_id"]:
                raise ValueError("Question does not belong to the selected history")
            if parent["benchmarks"][name]["items"][query["qa_id"]]["source_unit_id"] != history["episode_id"]:
                raise ValueError("Question-bank source association differs from history")
            if name in history_filters and history["episode_id"] not in history_filters[name]["selected_episodes"]:
                raise ValueError("Question belongs to an excluded dataset history")
            if name == "beam" and beam_filter and history["episode_id"] not in beam_filter["selected_episodes"]:
                raise ValueError("Question belongs to an excluded BEAM history")
        for target in targets:
            if list(target["capability_leaves"]) != parent["benchmarks"][name]["items"][target["qa_id"]]["capability_leaves"]:
                raise ValueError("Question-bank target and capability mapping differ")
        folder = input_path(output, case["history"]).parent
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(input_path(bank, case["history"]), input_path(output, case["history"]))
        write_rows(input_path(output, case["queries"]), queries)
        write_rows(input_path(output, case["targets"]), targets)
        for field in ("history", "queries", "targets"):
            manifest["inputs"][case[field]] = file_hash(input_path(output, case[field]))
        if manifest["inputs"][case["history"]] != parent["inputs"][case["history"]]:
            raise ValueError("Sample must preserve the complete source history")
        index.append(case)
        source_ids = {m["source_id"] for s in history["sessions"] for m in s["messages"]}
        source_sessions = {s.get("source_session_id") or s["session_id"] for s in history["sessions"]}
        missing_evidence = {}
        evidence_reference_count = 0
        for target in targets:
            if name in {"locomo_refined", "longmemeval_s"}:
                references = target.get("evidence_source_ids", [])
                # Native LoCoMo annotations can put several dia_ids in one
                # list entry, e.g. "D8:6; D9:17". Audit atomic references while
                # preserving the original target bytes and labels.
                references = {part.strip() for reference in references
                              for part in (re.split(r"[;,]", reference) if name == "locomo_refined" else [reference])
                              if part.strip()}
                evidence_reference_count += len(references)
                missing = references - (source_ids if name == "locomo_refined" else source_sessions)
                if missing:
                    missing_evidence[target["qa_id"]] = sorted(missing)
        if missing_evidence:
            raise ValueError("Source evidence IDs are outside the retained complete history")
        association_checks.append({"benchmark": name, "episode_id": case["episode_id"],
            "qa_ids": sorted(ids), "question_history_scope_matches": True,
            "history_sha256": manifest["inputs"][case["history"]],
            "question_bank_history_sha256": parent["inputs"][case["history"]],
            "history_unchanged": True,
            "evidence_id_membership": "verified" if name in {"locomo_refined", "longmemeval_s"} else "native_source_association_only",
            "evidence_source_references_checked": evidence_reference_count,
            "native_prefix_preserved": name == "personamem_v1"})
        stats = audit.setdefault(name, {"selected_questions": 0, "cases": 0, "sessions": 0, "messages": 0})
        stats["selected_questions"] += len(queries)
        stats["cases"] += 1
        stats["sessions"] += len(history["sessions"])
        stats["messages"] += sum(len(s["messages"]) for s in history["sessions"])
    manifest["benchmarks"] = {name: {**config,
        "items": {ident: item for ident, item in config["items"].items() if (name, ident) in chosen},
        "loader_config": {**config["loader_config"],
                          "question_ids": sorted(ident for benchmark, ident in chosen if benchmark == name)}}
        for name, config in parent["benchmarks"].items() if name in audit}
    frozen_ids = {(q["benchmark"], q["qa_id"]) for case in index for q in rows(output / case["queries"])}
    if frozen_ids != chosen:
        raise ValueError("Sampled questions are missing from question-bank cases")
    validate_manifest(manifest)
    write_rows(output / "cases.jsonl", index)
    manifest["inputs"]["cases.jsonl"] = file_hash(output / "cases.jsonl")
    write_json(output / "manifest.json", manifest)
    write_json(output / "dataset_audit.json", audit)
    result = {"status": "prepared", "contract_sha256": contract_hash(manifest),
              "cases": len(index), "questions": len(chosen), "api_calls": 0}
    write_json(output / "prepared.json", result)
    write_json(output / "sampling.json", {**manifest["selection"], "by_capability": details, "by_family": family_details,
        "by_benchmark": audit, "selected_ids": {name: sorted(config["items"])
                                                for name, config in manifest["benchmarks"].items()}})
    write_json(output / "question_history_audit.json", {"status": "passed", "api_calls": 0,
        "questions": len(chosen), "history_cases": len(index), "all_question_scopes_match": True,
        "all_history_hashes_preserved": True,
        "scope": "source associations and available evidence IDs; not semantic proof that every native gold fact is present",
        "cases": association_checks})
    write_json(output / "suite.json", {key: manifest[key] for key in ("suite_id", "scope", "purpose", "aggregation")} |
               {"benchmarks": {name: config["loader_config"] for name, config in manifest["benchmarks"].items()}})
    write_json(output / "runtime.json", manifest["runtime"])
    return result
