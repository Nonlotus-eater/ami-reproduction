"""Offline sampling with native question/history associations and DOCX table 3."""
import argparse
import ast
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import socket
import shlex
import sys
import time
from statistics import mean
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from memory_benchmark.benchmarks.public_datasets import (
    BEAM_LEAVES, CL_LEAVES, V1_LEAVES, V2_LEAVES, csv_rows, safe_link,
)
from memory_benchmark.pipeline.aggregate import FAMILIES, LEAF_FAMILY
from memory_benchmark.runtime.artifacts import contract_hash, file_hash, json_array_items, read_json, rows, write_json, write_rows

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAMES = {"LoCoMo": "locomo_refined", "LME": "longmemeval_s", "P1": "personamem_v1",
                "P2": "personamem_v2", "CLBench": "clbench", "BEAM": "beam"}


def block_network(*args, **kwargs):
    raise RuntimeError("Network disabled: sampling and counts only")


def table_three(path):
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(path) as archive:
        body = ET.fromstring(archive.read("word/document.xml")).find("w:body", ns)
    caption = ""
    for element in body:
        if element.tag.endswith("}p"):
            text = "".join(t.text or "" for t in element.findall(".//w:t", ns))
            if text.strip():
                caption = text.strip()
        elif element.tag.endswith("}tbl") and re.match(r"表\s*3(?:\s|[：:])", caption):
            result = []
            for row in element.findall("w:tr", ns)[1:]:
                cells = ["".join(t.text or "" for t in c.findall(".//w:t", ns)).strip()
                         for c in row.findall("w:tc", ns)]
                leaf, label, number, source_text = cells
                sources = {}
                if source_text != "N/A":
                    for part in re.split(r"[；;]", source_text):
                        match = re.fullmatch(r"\s*(LoCoMo|LME|P1|P2|CLBench|BEAM)\s+(\d+)\s*", part)
                        if not match:
                            raise ValueError(f"Unrecognized table-3 source: {part}")
                        sources[SOURCE_NAMES[match[1]]] = int(match[2])
                if leaf not in LEAF_FAMILY or sum(sources.values()) != int(number):
                    raise ValueError(f"Invalid table-3 row: {cells}")
                result.append({"leaf": leaf, "family": LEAF_FAMILY[leaf], "label": label,
                               "reference_count": int(number), "reference_sources": sources})
            if len(result) != 24 or {x["leaf"] for x in result} != set(LEAF_FAMILY):
                raise ValueError("Table 3 must define all 24 distinct capability leaves")
            return {"document": str(path), "sha256": file_hash(path), "caption": caption, "rows": result}
    raise ValueError("Table 3 caption/table not found in document")


def question_index(prepared, bank_suite):
    old = read_json(prepared / "manifest.json")
    suite = read_json(bank_suite)
    result, seen = [], set()
    source_files = set()
    for benchmark, base in suite["benchmarks"].items():
        local = old["benchmarks"][benchmark]["loader_config"]
        wanted = set(base["question_ids"]) if "question_ids" in base else None
        before = len(result)

        def add(ident, episode, cluster, kind, leaves, locator, length=None):
            if wanted is not None and ident not in wanted:
                return
            key = (benchmark, ident)
            if key in seen or len(leaves) != 1 or leaves[0] not in LEAF_FAMILY:
                raise ValueError(f"Duplicate question or unsupported mapping: {key}")
            seen.add(key)
            result.append({"benchmark": benchmark, "qa_id": ident, "source_unit_id": str(episode),
                           "cluster_id": str(cluster), "native_type": str(kind), "length_split": length,
                           "leaf": leaves[0], "family": LEAF_FAMILY[leaves[0]], "history_locator": locator})

        if benchmark == "locomo_refined":
            folder = Path(local["data_dir"])
            source_files.update([folder / "conversations.jsonl", folder / "questions.jsonl"])
            mapping = read_json(ROOT / "src/memory_benchmark/benchmarks/locomo_refined/capability_map.json")["categories"]
            episodes = {x["sample_id"] for x in rows(folder / "conversations.jsonl")}
            for q in rows(folder / "questions.jsonl"):
                if local.get("media_policy", "text_only") == "text_only" and q.get("is_multi_modality"):
                    continue
                if q["sample_id"] not in episodes:
                    raise ValueError("LoCoMo question has no source conversation")
                add(q["qa_id"], q["sample_id"], q["sample_id"], q["category"],
                    (mapping[str(q["category"])],), {"file": str(folder / "conversations.jsonl"), "sample_id": q["sample_id"]})
        elif benchmark == "longmemeval_s":
            path = Path(local["data_file"])
            source_files.add(path)
            mapping = read_json(ROOT / "src/memory_benchmark/benchmarks/longmemeval/capability_map.json")
            for q in json_array_items(path):
                ident = q["question_id"]
                leaf = mapping["abstention_leaf"] if ident.endswith(mapping["abstention_suffix"]) else mapping["question_types"][q["question_type"]]
                if not len(q["haystack_session_ids"]) == len(q["haystack_sessions"]) == len(q["haystack_dates"]):
                    raise ValueError("LongMemEval history/session/date mismatch")
                kind = "abstention" if ident.endswith(mapping["abstention_suffix"]) else q["question_type"]
                add(ident, ident, ident, kind, (leaf,), {"file": str(path), "question_id": ident,
                    "history_policy": "own_complete_haystack_preserve_all_sessions_sort_chronologically"})
        elif benchmark == "clbench":
            path = Path(local["data_file"])
            source_files.add(path)
            for q in rows(path):
                meta = q["metadata"]
                if not q["messages"] or q["messages"][-1]["role"] != "user":
                    raise ValueError("CLBench lacks native final user task")
                add("clbench:" + str(meta["task_id"]), meta["task_id"], meta["context_id"],
                    meta["context_category"], CL_LEAVES.get(meta["context_category"], ()),
                    {"file": str(path), "task_id": meta["task_id"], "history": "messages[:-1]",
                     "query": "messages[-1]; retain native embedded context"})
        elif benchmark == "personamem_v1":
            folder = Path(local["data_dir"])
            source_files.update([folder / "questions_32k.csv", folder / "shared_contexts_32k.jsonl"])
            contexts = {}
            for row in rows(folder / "shared_contexts_32k.jsonl"):
                contexts.update(row)
            for q in csv_rows(folder / "questions_32k.csv"):
                end = int(q["end_index_in_shared_context"])
                if q["shared_context_id"] not in contexts or not 0 < end <= len(contexts[q["shared_context_id"]]):
                    raise ValueError("PersonaMem v1 source or native causal prefix missing")
                episode = f"{q['persona_id']}:{q['shared_context_id']}:prefix:{end}"
                add("personamem_v1:" + q["question_id"], episode, q["persona_id"], q["question_type"],
                    V1_LEAVES.get(q["question_type"], ()), {"file": str(folder / "shared_contexts_32k.jsonl"),
                    "shared_context_id": q["shared_context_id"], "end_index_exclusive": end})
        elif benchmark == "personamem_v2":
            folder = Path(local["data_dir"])
            source_files.add(folder / "benchmark/text/benchmark.csv")
            for i, q in enumerate(csv_rows(folder / "benchmark/text/benchmark.csv")):
                ident = f"personamem_v2:{i}"
                if wanted is not None and ident not in wanted:
                    continue
                path = safe_link(folder, q["chat_history_32k_link"])
                if not path.is_file():
                    raise ValueError("PersonaMem v2 original history missing")
                source_files.add(path)
                episode = q["persona_id"] + ":" + q["chat_history_32k_link"]
                add(ident, episode, q["persona_id"], q["pref_type"], V2_LEAVES.get(q["pref_type"], ()),
                    {"file": str(path), "persona_id": q["persona_id"], "history_policy": "complete_32k_source_history"})
        elif benchmark == "beam":
            import pyarrow.parquet as pq
            for path in sorted(Path(local["data_dir"]).glob("*.parquet")):
                source_files.add(path)
                split = path.name.split("-")[0]
                for batch in pq.ParquetFile(path).iter_batches(columns=["conversation_id", "probing_questions"]):
                    for q in batch.to_pylist():
                        episode = split + ":" + str(q["conversation_id"])
                        probes = ast.literal_eval(q["probing_questions"]) if isinstance(q["probing_questions"], str) else q["probing_questions"]
                        for kind, values in probes.items():
                            for i, value in enumerate(values):
                                if value.get("question") and value.get("rubric"):
                                    add(f"beam:{episode}:{kind}:{i}", episode, q["conversation_id"], kind,
                                        BEAM_LEAVES.get(kind, ()), {"file": str(path), "conversation_id": q["conversation_id"],
                                        "length_split": split, "history_policy": "complete_chat_of_matching_length_split"}, split)
        else:
            raise ValueError(f"Unsupported dataset: {benchmark}")
        selected_ids = {x["qa_id"] for x in result[before:]}
        if wanted is not None and selected_ids != wanted:
            raise ValueError(f"Question-bank configured IDs missing for {benchmark}")
        print(json.dumps({"indexed": benchmark, "questions": len(result) - before}, ensure_ascii=False), flush=True)
    counts = dict(Counter(x["benchmark"] for x in result))
    expected = suite.get("question_bank_expected_counts")
    if expected is not None and counts != expected:
        raise ValueError(f"Question-bank source counts differ: expected {expected}, got {counts}")
    if set(counts) != set(suite["benchmarks"]):
        raise ValueError("Question bank is missing a configured dataset")
    return sorted(result, key=lambda x: (x["benchmark"], x["qa_id"])), source_files


def verify_reference(index, old, reference):
    lookup = {(x["benchmark"], x["qa_id"]): x for x in index}
    counts, sources = Counter(), {}
    for name, config in old["benchmarks"].items():
        for ident, original in config["items"].items():
            current = lookup[(name, ident)]
            if current["source_unit_id"] != original["source_unit_id"] or [current["leaf"]] != original["capability_leaves"]:
                raise ValueError("Recovered bank differs from an original question/history/mapping association")
            counts[current["leaf"]] += 1
            sources.setdefault(current["leaf"], Counter())[name] += 1
    for row in reference["rows"]:
        if counts[row["leaf"]] != row["reference_count"] or dict(sources.get(row["leaf"], {})) != row["reference_sources"]:
            raise ValueError(f"Original 99-question distribution differs from DOCX table 3: {row['leaf']}")


def leaf_requirements(index, minimum):
    return {leaf: min(minimum, count) for leaf, count in Counter(x["leaf"] for x in index).items()}


def check_capacity(index, maximum, requirements, min_questions):
    required_by_family = Counter()
    for leaf, number in requirements.items():
        required_by_family[LEAF_FAMILY[leaf]] += number
    if any(number > maximum for number in required_by_family.values()):
        raise ValueError("Family cap cannot accommodate the requested leaf minimums")
    capacity = sum(min(maximum, count) for count in Counter(x["family"] for x in index).values())
    if capacity < min_questions:
        raise ValueError(f"Question pool can supply only {capacity} questions under the family caps")


def select(index, maximum, seed, requirements=None):
    requirements = requirements or {}
    check_capacity(index, maximum, requirements, 0)
    selected = []
    for family in FAMILIES:
        candidates = [x for x in index if x["family"] == family]
        def rank(record):
            key = json.dumps([seed, family, record["benchmark"], record["qa_id"]], ensure_ascii=False, separators=(",", ":"))
            return hashlib.sha256(key.encode()).digest(), record["benchmark"], record["qa_id"]
        ordered = sorted(candidates, key=rank)
        reserved, leaf_counts = [], Counter()
        for record in ordered:
            if leaf_counts[record["leaf"]] < requirements.get(record["leaf"], 0):
                reserved.append(record)
                leaf_counts[record["leaf"]] += 1
        for leaf, required in requirements.items():
            if LEAF_FAMILY[leaf] == family and leaf_counts[leaf] < required:
                raise ValueError(f"Selected histories cannot supply {required} questions for {leaf}")
        reserved_ids = {(x["benchmark"], x["qa_id"]) for x in reserved}
        extra = [x for x in ordered if (x["benchmark"], x["qa_id"]) not in reserved_ids]
        selected.extend(sorted(reserved + extra[:maximum - len(reserved)], key=rank))
    return selected


def select_histories(index, limits, seed):
    """Select source units before looking at which questions will be drawn."""
    available = {}
    for question in index:
        key = (question["benchmark"], question["source_unit_id"])
        if key not in available:
            available[key] = {k: question[k] for k in (
                "benchmark", "source_unit_id", "cluster_id", "length_split", "history_locator")}
    selected = []
    for benchmark, maximum in limits.items():
        candidates = [h for h in available.values() if h["benchmark"] == benchmark]
        def rank(history):
            key = json.dumps(["history", seed, benchmark, history["source_unit_id"]],
                             ensure_ascii=False, separators=(",", ":"))
            return hashlib.sha256(key.encode()).digest(), history["source_unit_id"]
        selected.extend(sorted(candidates, key=rank)[:maximum])
    return selected


def expand_histories(index, initial, seed, maximum, requirements, min_questions, max_histories):
    """Add randomly ranked native histories until the question quotas are feasible."""
    check_capacity(index, maximum, requirements, min_questions)
    inventory, history_rows = {}, defaultdict(list)
    for record in index:
        key = (record["benchmark"], record["source_unit_id"])
        history_rows[key].append(record)
        inventory[key] = {k: record[k] for k in (
            "benchmark", "source_unit_id", "cluster_id", "length_split", "history_locator")}
    selected = list(initial)
    if len(selected) > max_histories:
        raise ValueError("Initial history selection exceeds the total history cap")
    chosen = {(h["benchmark"], h["source_unit_id"]) for h in selected}
    eligible = [x for x in index if (x["benchmark"], x["source_unit_id"]) in chosen]
    counts = Counter(x["leaf"] for x in eligible)
    additions = []

    def add(keys, reason):
        if len(selected) >= max_histories:
            raise ValueError(f"Cannot satisfy {reason} within the {max_histories}-history cap")
        def rank(key):
            data = json.dumps(["history-supplement", seed, reason, *key], ensure_ascii=False, separators=(",", ":"))
            return hashlib.sha256(data.encode()).digest(), key
        key = min(keys, key=rank)
        chosen.add(key)
        selected.append(inventory[key])
        eligible.extend(history_rows[key])
        counts.update(x["leaf"] for x in history_rows[key])
        additions.append({"benchmark": key[0], "source_unit_id": key[1], "reason": reason})

    for leaf in LEAF_FAMILY:
        while counts[leaf] < requirements.get(leaf, 0):
            candidates = [key for key in inventory if key not in chosen and
                          any(x["leaf"] == leaf for x in history_rows[key])]
            if not candidates:
                raise ValueError(f"No native history can satisfy the {leaf} minimum")
            add(candidates, f"leaf-minimum:{leaf}")
    while sum(min(maximum, n) for n in Counter(x["family"] for x in eligible).values()) < min_questions:
        family_counts = Counter(x["family"] for x in eligible)
        candidates = [key for key in inventory if key not in chosen and
                      any(family_counts[x["family"]] < maximum for x in history_rows[key])]
        if not candidates:
            raise ValueError("No native history can satisfy the total question minimum")
        add(candidates, "total-question-minimum")
    return selected, additions


def write_csv(path, fieldnames, records):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def workload_audit(old, eligible_by_seed, draws, output):
    """Use native loaders for association checks and full history token accounting."""
    import tiktoken
    from memory_benchmark.benchmarks import LOADERS
    encoding = tiktoken.get_encoding("o200k_base")
    expected = {(x["benchmark"], x["qa_id"]): x for pool in eligible_by_seed.values() for x in pool}
    history_stats, found = {}, set()
    for benchmark in sorted(old["benchmarks"]):
        ids = {q for b, q in expected if b == benchmark}
        if not ids:
            continue
        config = old["benchmarks"][benchmark]["loader_config"]
        for case in LOADERS[benchmark](config, question_ids=ids):
            key = (benchmark, case.history.episode_id)
            messages = [m for s in case.history.sessions for m in s.messages]
            history_stats[key] = {"benchmark": benchmark, "source_unit_id": key[1],
                "history_body_tokens": sum(len(encoding.encode_ordinary(m.text)) for m in messages),
                "sessions": len(case.history.sessions), "messages": len(messages),
                "history_content_sha256": contract_hash(case.history.to_wire())}
            targets = {t.qa_id: t for t in case.targets}
            for query in case.queries:
                ident = (benchmark, query.qa_id)
                record = expected[ident]
                if ident in found or query.episode_id != record["source_unit_id"] or query.episode_id != key[1]:
                    raise ValueError("Native loader question/history association mismatch")
                if list(targets[query.qa_id].capability_leaves) != [record["leaf"]]:
                    raise ValueError("Native loader capability mapping mismatch")
                found.add(ident)
        print(json.dumps({"audited_native_histories": benchmark,
                          "count": sum(b == benchmark for b, _ in history_stats)}), flush=True)
    if found != set(expected):
        raise ValueError("Native loaders did not recover all eligible question IDs")
    write_rows(output / "history_workload.jsonl", (history_stats[k] for k in sorted(history_stats)))
    result = {}
    for seed, selected in draws.items():
        involved = {(x["benchmark"], x["source_unit_id"]) for x in selected}
        planned = {(x["benchmark"], x["source_unit_id"]) for x in eligible_by_seed[seed]}
        by_benchmark = {}
        for benchmark in sorted(old["benchmarks"]):
            stats = [history_stats[k] for k in involved if k[0] == benchmark]
            number = sum(x["benchmark"] == benchmark for x in selected)
            by_benchmark[benchmark] = {"history_instances": len(stats), "questions": number,
                "history_body_tokens": sum(x["history_body_tokens"] for x in stats),
                "sessions": sum(x["sessions"] for x in stats), "messages": sum(x["messages"] for x in stats),
                "answer_calls": number, "judge_calls": 0 if benchmark == "personamem_v1" else number}
        tokens = sum(x["history_body_tokens"] for x in by_benchmark.values())
        result[str(seed)] = {"tokenizer": "o200k_base", "by_benchmark": by_benchmark,
            "history_body_tokens_per_method": tokens,
            "planned_history_body_tokens_per_method": sum(history_stats[k]["history_body_tokens"] for k in planned),
            "history_instances_per_method": len(involved),
            "history_jobs_mem0_and_ami": 2 * len(involved),
            "history_jobs_four_methods": 4 * len(involved),
            "history_body_tokens_mem0_and_ami": 2 * tokens,
            "history_body_tokens_four_methods": 4 * tokens,
            "answer_judge_calls_per_method": sum(x["answer_calls"] + x["judge_calls"] for x in by_benchmark.values()),
            "relative_to_original_99_history_tokens": tokens / 1058203,
            "timing_note": "Input workload, not measured execution time; excludes memory-generation, embeddings, retries and method overhead. No cross-round memory reuse assumed."}
        write_json(output / f"seed-{seed}/workload.json", result[str(seed)])
    return result, len(found)


def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, default=ROOT / "runs/prepared-99-local")
    parser.add_argument("--bank-suite", type=Path, default=ROOT / "configs/family40-history30-five-seeds-v3/suite.json")
    parser.add_argument("--reference", type=Path, default=ROOT / "docs/references/AMI_Benchmark_99_reference.docx")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--history-scope", choices=["original", "all", "sampled"], default="sampled")
    parser.add_argument("--seeds", type=int, nargs=5, default=[17, 18, 19, 20, 21])
    parser.add_argument("--max-per-family", type=int, default=20)
    parser.add_argument("--min-per-leaf", type=int, default=0)
    parser.add_argument("--min-questions", type=int, default=0)
    parser.add_argument("--expand-histories", action="store_true")
    parser.add_argument("--max-histories", type=int)
    parser.add_argument("--beam-length", choices=["100K", "500K", "1M"])
    parser.add_argument("--workload-audit", action="store_true",
                        help="Verify selected native cases and count o200k_base history tokens offline")
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    if args.max_per_family <= 0 or len(set(args.seeds)) != 5:
        parser.error("Use a positive family cap and five different integer seeds")
    if args.min_per_leaf < 0 or args.min_questions < 0 or (args.max_histories is not None and args.max_histories <= 0):
        parser.error("Use nonnegative question minimums and a positive history cap")
    if args.expand_histories and (args.history_scope != "sampled" or args.max_histories is None):
        parser.error("History expansion requires sampled scope and an explicit total history cap")
    socket.socket.connect = block_network
    socket.socket.connect_ex = block_network
    socket.create_connection = block_network
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "summary.json").exists():
        raise ValueError("Sampling results already exist; choose a new output directory")
    old = read_json(args.prepared / "manifest.json")
    reference = table_three(args.reference.resolve())
    index_path = output / "candidate_questions.jsonl"
    if index_path.exists() and (output / "inventory.json").exists():
        index = list(rows(index_path))
        inventory = read_json(output / "inventory.json")
        if file_hash(args.bank_suite) != inventory["bank_suite_sha256"] or file_hash(index_path) != inventory["index_sha256"]:
            raise ValueError("Cached question-bank index/config changed")
        source_files = set(map(Path, inventory["source_sha256"]))
        if any(file_hash(path) != digest for path, digest in inventory["source_sha256"].items()):
            raise ValueError("Cached index source files changed; use a new output directory")
    else:
        index, source_files = question_index(args.prepared, args.bank_suite)
        write_rows(index_path, index)
    verify_reference(index, old, reference)
    original_histories = {(b, x["source_unit_id"]) for b, c in old["benchmarks"].items() for x in c["items"].values()}
    associated = [x for x in index if (x["benchmark"], x["source_unit_id"]) in original_histories]
    inventory = {"question_bank_questions": len(index), "bank_suite": str(args.bank_suite.resolve()),
        "bank_suite_sha256": file_hash(args.bank_suite), "index_sha256": file_hash(index_path),
        "source_sha256": {str(p): file_hash(p) for p in sorted(source_files)},
        "reference_table3": reference, "original_99_reference_and_associations_verified": True,
        "original_histories": len(original_histories), "questions_within_original_histories": len(associated),
        "by_benchmark": dict(Counter(x["benchmark"] for x in index)),
        "original_history_pool_by_benchmark": dict(Counter(x["benchmark"] for x in associated)), "api_calls": 0}
    write_json(output / "inventory.json", inventory)
    print(json.dumps({k: inventory[k] for k in ("question_bank_questions", "original_histories", "questions_within_original_histories", "by_benchmark", "original_history_pool_by_benchmark")}, ensure_ascii=False), flush=True)
    if args.inventory_only:
        return
    pool = [x for x in index if not args.beam_length or x["benchmark"] != "beam" or x["length_split"] == args.beam_length]
    requirements = leaf_requirements(index, args.min_per_leaf)
    available = Counter(x["leaf"] for x in pool)
    if any(available[leaf] < number for leaf, number in requirements.items()):
        raise ValueError("Length filter makes a requested leaf minimum infeasible")
    check_capacity(pool, args.max_per_family, requirements, args.min_questions)
    history_limits = {b: len({i["source_unit_id"] for i in c["items"].values()})
                      for b, c in old["benchmarks"].items()}
    additions = {seed: [] for seed in args.seeds}
    if args.history_scope == "sampled":
        histories = {seed: select_histories(pool, history_limits, seed) for seed in args.seeds}
        if args.expand_histories:
            for seed in args.seeds:
                histories[seed], additions[seed] = expand_histories(
                    pool, histories[seed], seed, args.max_per_family, requirements,
                    args.min_questions, args.max_histories)
        eligible_by_seed = {}
        for seed, selected_histories in histories.items():
            keys = {(h["benchmark"], h["source_unit_id"]) for h in selected_histories}
            eligible_by_seed[seed] = [x for x in pool if (x["benchmark"], x["source_unit_id"]) in keys]
    else:
        pool = [x for x in pool if (x["benchmark"], x["source_unit_id"]) in original_histories] if args.history_scope == "original" else pool
        eligible_by_seed = {seed: pool for seed in args.seeds}
        counts = {b: len({x["source_unit_id"] for x in pool if x["benchmark"] == b}) for b in history_limits}
        histories = {seed: select_histories(pool, counts, seed) for seed in args.seeds}
    draws = {seed: select(eligible_by_seed[seed], args.max_per_family, seed, requirements) for seed in args.seeds}
    for seed, selected in draws.items():
        eligible = eligible_by_seed[seed]
        ids = {(x["benchmark"], x["qa_id"]) for x in selected}
        if len(ids) != len(selected) or max(Counter(x["family"] for x in selected).values()) > args.max_per_family:
            raise ValueError("Sampling violated uniqueness or family cap")
        if selected != select(list(reversed(eligible)), args.max_per_family, seed, requirements):
            raise ValueError("Seed replay changed")
        leaf_counts = Counter(x["leaf"] for x in selected)
        if len(selected) < args.min_questions or any(leaf_counts[leaf] < number for leaf, number in requirements.items()):
            raise ValueError("Sampling violated the total or leaf question minimum")
        if args.history_scope == "original" and any((x["benchmark"], x["source_unit_id"]) not in original_histories for x in selected):
            raise ValueError("Question belongs to an unselected history")
        allowed_histories = {(x["benchmark"], x["source_unit_id"]) for x in histories[seed]}
        if any((x["benchmark"], x["source_unit_id"]) not in allowed_histories for x in selected):
            raise ValueError("Question does not belong to this round's selected histories")
        if args.max_histories is not None and len(histories[seed]) > args.max_histories:
            raise ValueError("Sampling exceeded the total history cap")
        if args.history_scope == "sampled" and not args.expand_histories and Counter(h["benchmark"] for h in histories[seed]) != history_limits:
            raise ValueError("Round does not preserve requested per-dataset history counts")
        folder = output / f"seed-{seed}"
        folder.mkdir()
        write_json(folder / "selected_histories.json", histories[seed])
        write_json(folder / "history_additions.json", additions[seed])
        write_rows(folder / "eligible_questions.jsonl", eligible)
        write_rows(folder / "selected_questions.jsonl", selected)
        write_json(folder / "question_ids.json", {b: sorted(x["qa_id"] for x in selected if x["benchmark"] == b)
                   for b in sorted({x["benchmark"] for x in selected})})
    seed_cols = [f"seed_{seed}" for seed in args.seeds]
    leaf_rows = []
    for ref in reference["rows"]:
        values = [sum(x["leaf"] == ref["leaf"] for x in draws[seed]) for seed in args.seeds]
        leaf_rows.append({"family": ref["family"], "leaf": ref["leaf"], "label": ref["label"],
            "table3_reference": ref["reference_count"], "bank_available": sum(x["leaf"] == ref["leaf"] for x in index),
            **{f"available_seed_{seed}": sum(x["leaf"] == ref["leaf"] for x in eligible_by_seed[seed]) for seed in args.seeds},
            **dict(zip(seed_cols, values)), "mean": mean(values), "min": min(values), "max": max(values)})
    family_rows = [{"family": family,
                    **{f"available_seed_{seed}": sum(x["family"] == family for x in eligible_by_seed[seed]) for seed in args.seeds},
                    **{f"seed_{seed}": sum(x["family"] == family for x in draws[seed]) for seed in args.seeds}}
                   for family in FAMILIES]
    write_csv(output / "leaf_counts.csv", list(leaf_rows[0]), leaf_rows)
    write_csv(output / "family_counts.csv", list(family_rows[0]), family_rows)
    workload, native_verified = workload_audit(old, eligible_by_seed, draws, output) if args.workload_audit else ({}, None)
    progress_path = ROOT / "runs/baselines-99-complete-20261015-v1/progress.json"
    progress = read_json(progress_path) if progress_path.exists() else {}
    evaluation_snapshot = {"observed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "status": progress.get("status", "unknown"), "pid": progress.get("pid"),
        "completed_jobs": progress.get("completed_jobs"), "failed_jobs": progress.get("failed_jobs"),
        "active": progress.get("active"), "action_taken": "none; existing evaluation left running"}
    sets = {str(seed): {(x["benchmark"], x["qa_id"]) for x in selected} for seed, selected in draws.items()}
    overlap = [{"seed_a": a, "seed_b": b, "shared_questions": len(sets[a] & sets[b])}
               for i, a in enumerate(sets) for b in list(sets)[i + 1:]]
    write_csv(output / "overlap_counts.csv", list(overlap[0]), overlap)
    write_json(output / "reference_table3.json", reference)
    (output / "sample_five_seeds.executed.py").write_bytes(Path(__file__).read_bytes())
    summary = {"status": "complete", "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "sampling_only": True, "api_calls": 0, "network_disabled": True,
        "existing_evaluation": evaluation_snapshot,
        "history_scope": args.history_scope, "candidate_questions": {str(seed): len(pool) for seed, pool in eligible_by_seed.items()},
        "parent_bank_questions": len(index), "initial_history_counts": history_limits,
        "eligible_source_questions": len(pool),
        "eligible_source_questions_by_benchmark": dict(Counter(x["benchmark"] for x in pool)),
        "question_preselection": any("question_ids" in config for config in read_json(args.bank_suite)["benchmarks"].values()),
        "max_history_instances_per_round": args.max_histories,
        "history_selection": "seeded-native-histories-first; seeded supplements for leaf and total minimums within hard history cap" if args.expand_histories else "uniform-SHA256-priority-native-source-units-first",
        "beam_length_filter": args.beam_length,
        "max_questions_per_family": args.max_per_family, "seeds": args.seeds,
        "algorithm": "random-leaf-minimum-reservation-then-random-family-fill-v2",
        "min_questions_per_leaf": args.min_per_leaf, "per_leaf_minimums": requirements,
        "min_questions_per_round": args.min_questions,
        "within_round_replacement": False, "between_round_overlap_allowed": True,
        "reference_role": "Table 3 defines leaf names and historical comparison counts; new leaf minimums are configured separately",
        "original_99_question_ids_unlocked": True, "native_question_history_associations_preserved": True,
        "script_sha256": file_hash(Path(__file__)), "inventory_sha256": file_hash(output / "inventory.json"),
        "sampling_seconds": time.monotonic() - started,
        "total_question_occurrences": sum(len(x) for x in draws.values()),
        "distinct_selected_questions": len(set().union(*sets.values())), "pairwise_overlap": overlap,
        "workload": workload,
        "leaf_counts": leaf_rows, "family_counts": family_rows,
        "rounds": {str(seed): {"questions": len(selected), "history_instances": len({(x['benchmark'], x['source_unit_id']) for x in selected}),
            "histories_selected_first": len(histories[seed]), "candidate_questions": len(eligible_by_seed[seed]),
            "initial_histories": sum(history_limits.values()) if args.history_scope == "sampled" else len(histories[seed]),
            "supplemental_histories": len(additions[seed]),
            "history_counts_by_benchmark": dict(Counter(x["benchmark"] for x in histories[seed])),
            "min_questions_in_available_leaf": min(Counter(x["leaf"] for x in selected).values()),
            "covered_leaves": len({x["leaf"] for x in selected}),
            "selected_histories_sha256": file_hash(output / f"seed-{seed}/selected_histories.json"),
            "by_benchmark": dict(Counter(x["benchmark"] for x in selected)),
            "selected_questions_sha256": file_hash(output / f"seed-{seed}/selected_questions.jsonl")} for seed, selected in draws.items()}}
    write_json(output / "summary.json", summary)
    validation = {"status": "passed", "verified_at": summary["generated_at"], "api_calls": 0,
        "network_disabled": True, "seed_count": len(draws), "seed_replay_and_input_order_invariance_verified": True,
        "round_question_minimum_verified": args.min_questions,
        "family_question_cap_verified": args.max_per_family,
        "leaf_question_minimum_verified": args.min_per_leaf,
        "history_cap_verified": args.max_histories,
        "all_selected_questions_belong_to_selected_native_histories": True,
        "original_99_counts_and_sources_match_docx_table3": True,
        "native_loader_eligible_questions_verified": native_verified,
        "all_question_sets_different": len({frozenset(s) for s in sets.values()}) == len(draws),
        "existing_evaluation": evaluation_snapshot}
    write_json(output / "validation.json", validation)
    report = ["# 五轮随机抽样的小类题数（大类上限 40、小类下限 2）", "",
        f"生成时间：{summary['generated_at']}。本次为离线抽样及统计，API 调用数为 0；原 99 题评测继续独立执行，未发送停止信号。", "",
        f"母题库 {len(index):,} 题；每个能力大类最多 {args.max_per_family} 题，每个有候选题的小类至少 min({args.min_per_leaf}, 母库题数) 题；每轮至少 {args.min_questions} 题。轮内不重复，轮间允许重合。", "",
        f"每轮历史总数硬上限为 {args.max_histories}。先按 seed 随机选择原规模的 19 个历史（LoCoMo 2、LongMemEval 7、CLBench 4、PersonaMem v1/v2 各 2、BEAM 2），若缺少小类候选，则在含该小类的剩余历史中按 seed 随机补充，达到上限仍不可行则报错。此为覆盖约束下的随机抽样。", "",
        f"BEAM 仅使用 {args.beam_length or '全部长度'} 分组的完整历史，以控制后续记忆构建规模；不裁剪历史。先从已选历史关联题中为各小类随机预留最低题数，再从剩余候选随机填充至大类上限或候选耗尽。上限不要求凑满；不按模型记忆内容、参考答案或得分选题。", "",
        f"小类名称、分组和旧 99 题对照读取 `{args.reference.relative_to(ROOT) if args.reference.is_relative_to(ROOT) else args.reference}`；旧题数不作为新轮次配额。F1 归在 D 大类，A2/B2/B3 无已验证映射。", "",
        "每道入选题均保存原生历史定位信息。LongMemEval 使用其独立 haystack；PersonaMem v1 保留原生因果前缀；BEAM 同时匹配长度分片和 conversation ID。", "",
        "| 小类 | 名称 | 表三旧题数 | 母库题数 | " + " | ".join(str(s) for s in args.seeds) + " | 均值 |", "|---|---|---:|---:|" + "---:|" * 6]
    for row in leaf_rows:
        report.append("| " + " | ".join(map(str, [row["leaf"], row["label"], row["table3_reference"], row["bank_available"],
            *(row[c] for c in seed_cols), f"{row['mean']:.1f}"])) + " |")
    report += ["", "| 大类 | " + " | ".join(str(s) for s in args.seeds) + " |", "|---|" + "---:|" * 5]
    for row in family_rows:
        report.append("| " + " | ".join(map(str, [row["family"], *(row[c] for c in seed_cols)])) + " |")
    report.append("| **合计** | " + " | ".join(str(len(draws[s])) for s in args.seeds) + " |")
    report += ["", "| seed | 先选历史 | 关联候选题 | 入选题数 | 题目涉及历史 | 覆盖小类 |", "|---|---:|---:|---:|---:|---:|"]
    for seed in args.seeds:
        result = summary["rounds"][str(seed)]
        report.append(f"| {seed} | {result['histories_selected_first']} | {result['candidate_questions']} | {result['questions']} | {result['history_instances']} | {result['covered_leaves']} |")
    if workload:
        report += ["", "历史初选与补齐总数均受 30 个上限约束；配额抽题后部分历史可能没有入选题，未来只需为实际涉及的历史构建记忆。", "",
            "| seed | 实际历史正文 token/方法 | 全部已选历史正文 token/方法 | 相对旧99题正文 | Answer+Judge 请求/方法 | Mem0+AMI 历史任务 |", "|---|---:|---:|---:|---:|---:|"]
        for seed in args.seeds:
            w = workload[str(seed)]
            report.append(f"| {seed} | {w['history_body_tokens_per_method']:,} | {w['planned_history_body_tokens_per_method']:,} | {w['relative_to_original_99_history_tokens']:.2f}× | {w['answer_judge_calls_per_method']} | {w['history_jobs_mem0_and_ami']} |")
        report += ["", "正文 token 使用 o200k_base 逐消息计算，不含 query、候选选项、消息包装和方法生成的记忆。旧 99 题基线正文为 1,058,203 token。CLBench 的参考资料可能位于 query，虽保留用于作答，但不计入正文 token。Answer+Judge 请求数按每题一次 Answer、除 PersonaMem v1 外每题一次 Judge 计算，不含记忆提取、embedding 和重试。", "",
            "这些是后续评测的输入工作量，不是已实测工期。API 延迟、限流、方法构建成本和硬件会影响耗时；五轮按独立冷启动统计，不假设跨轮复用记忆。完整原生关联与正文规模核验见 `validation.json`、`history_workload.jsonl` 及各轮 `workload.json`。"]
    report += ["", f"五轮合计 {summary['total_question_occurrences']} 题次、{summary['distinct_selected_questions']} 道不同题目。", ""]
    report += ["", "抽样结果仅包含题目及来源元数据；后续如启动真实评测，应按本轮入选 ID 恢复对应完整历史及评分目标。当前不会自动启动评测。", "",
        "[小类 CSV](leaf_counts.csv) · [大类 CSV](family_counts.csv) · [机器可读汇总](summary.json) · [核验记录](validation.json) · [完整母题库索引](candidate_questions.jsonl)", "",
        "复现命令（改用新的输出目录）：", "", "```bash", shlex.join([sys.executable, *sys.argv]), "```"]
    (output / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps({"output": str(output), "rounds": summary["rounds"], "api_calls": 0}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
