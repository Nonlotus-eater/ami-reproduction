"""Freeze sanitized inputs, scoring-only targets and their exact contracts."""
from collections import Counter
from pathlib import Path

from ..benchmarks import LOADERS
from ..benchmarks.native_contract import ROOT, contract
from ..runtime.artifacts import (append_row, contract_hash, file_hash, read_json,
                                 rows, write_json, write_rows)
from .aggregate import validate_manifest
from ..runtime.native_freeze import native_identity


def code_identity(root=ROOT):
    files = sorted([*(root / "src").rglob("*.py"), *(root / "src").rglob("*.json"),
                    *(root / "sources").rglob("*.py"), *(root / "sources").rglob("*.json")])
    hashes = {str(p.relative_to(root)): file_hash(p) for p in files}
    return {"sha256": contract_hash(hashes), "files": hashes}


def data_paths(name, config):
    if name == "locomo_refined":
        return [Path(config["data_dir"]) / n for n in ("conversations.jsonl", "questions.jsonl")]
    if name == "beam":
        return sorted(Path(config["data_dir"]).glob("*.parquet"))
    if name == "personamem_v1":
        return [Path(config["data_dir"]) / n for n in ("questions_32k.csv", "shared_contexts_32k.jsonl")]
    if name == "personamem_v2":
        from ..benchmarks.public_datasets import csv_rows, safe_link
        root = Path(config["data_dir"])
        csv_path = root / "benchmark/text/benchmark.csv"
        return [csv_path, *sorted({safe_link(root, r["chat_history_32k_link"]) for r in csv_rows(csv_path)})]
    return [Path(config["data_file"])]


def prepare(suite, runtime, output, *, selection=None):
    # Retired limits in older user configs must not re-enable spending gates.
    runtime = {key: value for key, value in runtime.items()
               if key not in {"request_limits", "estimated_cny_cap", "per_memory_worker_cny_cap"}}
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    selection = dict(selection or {})
    selected_benchmarks = selection.pop("benchmarks", None)
    manifest = {"suite_id": suite["suite_id"], "scope": suite["scope"],
                "purpose": suite["purpose"], "aggregation": suite["aggregation"],
                "official_aml_comparable": False, "runtime": runtime,
                "selection": {"benchmarks": selected_benchmarks, **selection},
                "capability_mapping_source": "local-coarse-dataset-category-v1",
                "benchmarks": {}, "framework": code_identity(), "inputs": {}}
    index, audit = [], {}
    for name, config in suite["benchmarks"].items():
        if selected_benchmarks and name not in selected_benchmarks:
            continue
        paths = data_paths(name, config)
        source = {str(p): file_hash(p) for p in paths}
        if config.get("source_manifest"):
            release = read_json(config["source_manifest"])
            expected_hashes = {r["path"]: r["sha256"] for r in release["files"]}
            snapshot = Path(config["source_snapshot"]).resolve()
            for p in paths:
                relative = str(p.resolve().relative_to(snapshot))
                if source[str(p)] != expected_hashes.get(relative):
                    raise ValueError("Pinned public source file changed")
        items, sessions, messages, anomalies = {}, 0, 0, Counter()
        for case in LOADERS[name](config, **selection):
            key = contract_hash([name, case.history.episode_id])[:24]
            folder = output / "inputs" / name / key
            folder.mkdir(parents=True)
            history_path, query_path, target_path = [folder / n for n in
                ("history.json", "queries.jsonl", "targets.eval.jsonl")]
            write_json(history_path, case.history.to_wire())
            write_rows(query_path, (q.to_wire() for q in case.queries))
            write_rows(target_path, (t.to_wire() for t in case.targets))
            for p in (history_path, query_path, target_path):
                manifest["inputs"][str(p.relative_to(output))] = file_hash(p)
            index.append({"benchmark": name, "episode_id": case.history.episode_id,
                          "key": key, "history": str(history_path.relative_to(output)),
                          "queries": str(query_path.relative_to(output)),
                          "targets": str(target_path.relative_to(output)),
                          "dataset_audit": case.dataset_audit})
            for target in case.targets:
                if target.qa_id in items:
                    raise ValueError("Duplicate question ID across dataset cases")
                stratum = target.question_type
                if name == "longmemeval_s" and target.qa_id.endswith("_abs"):
                    stratum = "abstention"
                items[target.qa_id] = {"source_unit_id": case.history.episode_id,
                    "cluster_id": str(case.dataset_audit.get("cluster_id", case.history.episode_id)),
                    "stratum": stratum, "length_split": case.dataset_audit.get("length_split"),
                    "capability_leaves": list(target.capability_leaves)}
            sessions += len(case.history.sessions)
            messages += sum(len(s.messages) for s in case.history.sessions)
            anomalies["cases_with_duplicate_source_session_ids"] += bool(case.dataset_audit.get("duplicate_source_session_ids"))
            anomalies["cases_with_sessions_after_question_time"] += bool(case.dataset_audit.get("sessions_after_question_time"))
        if not items:
            raise ValueError(f"Selection produces no questions for {name}")
        if source != {str(p): file_hash(p) for p in paths}:
            raise ValueError("Dataset changed during preparation")
        from ..benchmarks.public_datasets import METRICS
        manifest["benchmarks"][name] = {"primary_metric": METRICS[name], "items": items,
            "loader_config": config, "source_sha256": source,
            "native_scorer_sha256": contract(name).source_sha256}
        audit[name] = {"selected_questions": len(items), "sessions": sessions,
                       "messages": messages, **anomalies}
        if name == "locomo_refined":
            original = list(rows(Path(config["data_dir"]) / "questions.jsonl"))
            audit[name].update(source_questions=len(original),
                source_multimodal_questions=sum(bool(q.get("is_multi_modality")) for q in original),
                source_text_only_questions=sum(not q.get("is_multi_modality") for q in original),
                media_policy=config.get("media_policy", "text_only"))
    validate_manifest(manifest)
    write_rows(output / "cases.jsonl", index)
    manifest["inputs"]["cases.jsonl"] = file_hash(output / "cases.jsonl")
    write_json(output / "manifest.json", manifest)
    write_json(output / "dataset_audit.json", audit)
    write_json(output / "prepared.json", {"status": "prepared", "contract_sha256": contract_hash(manifest),
        "cases": len(index), "questions": sum(len(b["items"]) for b in manifest["benchmarks"].values())})
    return read_json(output / "prepared.json")


def verify_prepared(output):
    output = Path(output)
    manifest = validate_manifest(read_json(output / "manifest.json"))
    if contract_hash(manifest) != read_json(output / "prepared.json")["contract_sha256"]:
        raise ValueError("Prepared manifest was modified")
    actual_framework = code_identity()
    if manifest["framework"] != actual_framework:
        from ..runtime.execution_overlay import verify_framework_overlay
        verify_framework_overlay(ROOT, manifest["framework"], actual_framework)
    for relative, digest in manifest["inputs"].items():
        if file_hash(output / relative) != digest:
            raise ValueError("Prepared input changed")
    for config in manifest["benchmarks"].values():
        for path, digest in config["source_sha256"].items():
            if file_hash(path) != digest:
                raise ValueError("Source dataset changed")
    return manifest
