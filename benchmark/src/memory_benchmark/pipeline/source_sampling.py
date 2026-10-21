"""Public-source indexing and deterministic sampling without historical inputs."""
import ast
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from ..benchmarks.native_contract import ROOT
from ..benchmarks.public_datasets import (
    BEAM_LEAVES, CL_LEAVES, V1_LEAVES, V2_LEAVES, csv_rows, safe_link,
)
from ..runtime.artifacts import json_array_items, read_json, rows
from .aggregate import FAMILIES, LEAF_FAMILY


def question_index(bank_suite):
    """Index the complete public sources named by a standalone suite."""
    suite = read_json(bank_suite)
    result, seen = [], set()
    source_files = set()
    for benchmark, base in suite["benchmarks"].items():
        local = base
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


def select(index, maximum, seed, requirements=None, min_histories=0):
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
    # Preserve the legacy draw when no history floor is requested. For the new
    # profile, replace redundant-history questions without changing family size
    # or violating any leaf quota. A selected but unused history does not count.
    history_key = lambda q: (q["benchmark"], q["source_unit_id"])
    def priority(q):
        value = json.dumps([seed, q["family"], q["benchmark"], q["qa_id"]], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(value.encode()).digest(), q["benchmark"], q["qa_id"]
    while len({history_key(q) for q in selected}) < min_histories:
        history_counts = Counter(history_key(q) for q in selected)
        leaf_counts = Counter(q["leaf"] for q in selected)
        replacement = None
        for candidate in sorted(index, key=priority):
            if history_key(candidate) in history_counts:
                continue
            removable = [q for q in selected if q["family"] == candidate["family"] and
                         history_counts[history_key(q)] > 1 and
                         (q["leaf"] == candidate["leaf"] or leaf_counts[q["leaf"]] > requirements.get(q["leaf"], 0))]
            if removable:
                replacement = candidate, max(removable, key=priority)
                break
        if replacement is None:
            raise ValueError(f"Cannot retain {min_histories} actual histories within the family/leaf quotas")
        incoming, outgoing = replacement
        selected.remove(outgoing)
        selected.append(incoming)
    if min_histories:
        selected.sort(key=lambda q: (FAMILIES.index(q["family"]), priority(q)))
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


def expand_histories(index, initial, seed, maximum, requirements, min_questions, max_histories,
                     min_histories=0, priority="seeded"):
    """Add randomly ranked native histories until the question quotas are feasible."""
    check_capacity(index, maximum, requirements, min_questions)
    if min_histories < 0 or min_histories > max_histories:
        raise ValueError("History minimum must be between zero and the history cap")
    if priority not in {"seeded", "coverage"}:
        raise ValueError("Unknown history supplementation priority")
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
            candidate_counts = Counter(x["leaf"] for x in history_rows[key])
            gain = sum(min(max(0, need - counts[leaf]), candidate_counts[leaf]) for leaf, need in requirements.items())
            return (-gain if priority == "coverage" else 0), hashlib.sha256(data.encode()).digest(), key
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
    while len(selected) < min_histories:
        candidates = [key for key in inventory if key not in chosen]
        if not candidates:
            raise ValueError(f"Native pool cannot supply {min_histories} histories")
        add(candidates, "total-history-minimum")
    return selected, additions
