"""Full native histories for the four additional public benchmark versions.

Unknown dates stay unknown. Blocks are transport units, not invented sessions.
Preference targets, rubrics and persona descriptions never enter history/query.
"""
import ast
from collections import defaultdict
import csv
import json
from pathlib import Path

from ..runtime.artifacts import rows
from ..schemas import Case, History, Message, Query, Session, Target

BEAM_LEAVES = {
    "information_extraction": ("A3",), "preference_following": ("E1",),
    "abstention": ("H1",), "knowledge_update": ("D1",),
    "contradiction_resolution": ("D2",), "event_ordering": ("C2",),
    "temporal_reasoning": ("C1",), "multi_session_reasoning": ("B1",),
    "summarization": ("F1",), "instruction_following": ("G5",),
}
CL_LEAVES = {"Domain Knowledge Reasoning": ("G1",), "Rule System Application": ("G2",),
             "Procedural Task Execution": ("G3",), "Empirical Discovery & Simulation": ("G4",)}
V1_LEAVES = {"recall_user_shared_facts": ("A1",), "recalling_facts_mentioned_by_the_user": ("A1",),
    "track_full_preference_evolution": ("C3",), "recalling_the_reasons_behind_previous_updates": ("C3",),
    "provide_preference_aligned_recommendations": ("E1",), "generalizing_to_new_scenarios": ("E1",),
    "suggest_new_ideas": ("E1",)}
V2_LEAVES = {"anti_stereotypical_pref": ("E2",), "stereotypical_pref": ("E2",),
    "neutral_preferences": ("E1",), "health_and_medical_conditions": ("E3",),
    "therapy_background": ("E3",), "ask_to_forget": ("D3",), "sensitive_info": ("H2",)}
METRICS = {"locomo_refined": "judged_accuracy", "longmemeval_s": "judged_accuracy",
           "beam": "rubric_mean", "clbench": "strict_accuracy", "personamem_v1": "mcq_accuracy",
           "personamem_v2": "preference_adherence"}


def csv_rows(path):
    csv.field_size_limit(20 * 1024 * 1024)
    with Path(path).open(newline="") as stream:
        yield from csv.DictReader(stream)


def safe_link(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid source history link")
    result = (Path(root) / path).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise ValueError("Source history link leaves snapshot")
    return result


def blocks(benchmark, episode, batches):
    sessions = []
    for i, batch in enumerate(batches):
        messages = []
        for j, message in enumerate(batch):
            role, content = message["role"], message["content"]
            if role not in {"system", "user", "assistant"} or not isinstance(content, str):
                raise ValueError("Invalid source chat message")
            if content.strip():
                # time_anchor in BEAM is a source ordering marker, not a calendar date.
                anchor = message.get("time_anchor")
                text = (f"[Source time anchor: {anchor}] " if anchor is not None else "") + content
                messages.append(Message(str(message.get("id", f"{episode}:block:{i}:message:{j}")),
                    role, {"system": "System", "user": "User", "assistant": "Assistant"}[role], text, None))
        if messages:
            sessions.append(Session(f"{episode}:block:{i}", None, "", tuple(messages)))
    return History(benchmark, episode, ("User", "Assistant"), tuple(sessions))


def chat_blocks(messages):
    # Preserve complete messages and order; start a new transport block at each user turn.
    batch = []
    for message in messages:
        if message["role"] == "user" and batch:
            yield batch
            batch = []
        batch.append(message)
    if batch:
        yield batch


def selected(config, ident, episode, selection):
    ids = selection.get("question_ids")
    if ids is None:
        ids = config.get("question_ids")
    return ((ids is None or ident in ids) and
            (not selection.get("episode_ids") or episode in selection["episode_ids"]))


def load_clbench(config, **selection):
    count = 0
    for row in rows(config["data_file"]):
        meta = row["metadata"]
        episode = str(meta["task_id"])
        ident = "clbench:" + episode
        if not selected(config, ident, episode, selection):
            continue
        messages = row["messages"]
        if not messages or messages[-1]["role"] != "user":
            raise ValueError("CLBench requires a final user task")
        history = blocks("clbench", episode, chat_blocks(messages[:-1]))
        category = meta["context_category"]
        query = Query("clbench", episode, ident, messages[-1]["content"],
                      system_prompt="\n".join(m["content"] for m in messages[:-1] if m["role"] == "system"))
        target = Target(ident, "", CL_LEAVES.get(category, ()), question_type=category,
                        scoring_data={"rubrics": row["rubrics"], "sub_category": meta.get("sub_category")})
        yield Case(history, (query,), (target,), {"cluster_id": str(meta["context_id"]),
            "stratum": category, "source_task_id": episode, "unknown_recording_dates": True,
            "context_remains_in_native_final_user_task": True})
        count += 1
        if selection.get("episode_limit") and count >= selection["episode_limit"]:
            return


def load_beam(config, **selection):
    import pyarrow.parquet as pq
    count = 0
    for path in sorted(Path(config["data_dir"]).glob("*.parquet")):
        split = path.name.split("-")[0]
        for batch in pq.ParquetFile(path).iter_batches(batch_size=1):
            for row in batch.to_pylist():
                episode = split + ":" + str(row["conversation_id"])
                raw = row["probing_questions"]
                raw = ast.literal_eval(raw) if isinstance(raw, str) else raw
                queries, targets = [], []
                for kind, items in raw.items():
                    for i, item in enumerate(items):
                        ident = f"beam:{episode}:{kind}:{i}"
                        if not item.get("question") or not item.get("rubric") or not selected(config, ident, episode, selection):
                            continue
                        queries.append(Query("beam", episode, ident, item["question"]))
                        targets.append(Target(ident, "", BEAM_LEAVES.get(kind, ()), question_type=kind,
                                              scoring_data={"rubric_nuggets": item["rubric"], "length_split": split}))
                if selection.get("question_limit"):
                    queries, targets = queries[:selection["question_limit"]], targets[:selection["question_limit"]]
                if queries:
                    yield Case(blocks("beam", episode, row["chat"]), tuple(queries), tuple(targets),
                        {"cluster_id": str(row["conversation_id"]), "length_split": split, "unknown_recording_dates": True,
                         "complete_source_chat": True, "source_batches": len(row["chat"])})
                    count += 1
                    if selection.get("episode_limit") and count >= selection["episode_limit"]:
                        return


def load_v1(config, **selection):
    root = Path(config["data_dir"])
    groups = defaultdict(list)
    for row in csv_rows(root / "questions_32k.csv"):
        episode = f"{row['persona_id']}:{row['shared_context_id']}:prefix:{row['end_index_in_shared_context']}"
        if selected(config, "personamem_v1:" + row["question_id"], episode, selection):
            groups[episode].append(row)
    contexts = {}
    for row in rows(root / "shared_contexts_32k.jsonl"):
        contexts.update(row)
    for count, (episode, records) in enumerate(groups.items(), 1):
        if selection.get("question_limit"):
            records = records[:selection["question_limit"]]
        first = records[0]
        end = int(first["end_index_in_shared_context"])
        source = contexts[first["shared_context_id"]]
        if not 0 < end <= len(source):
            raise ValueError("Invalid native causal prefix")
        queries = tuple(Query("personamem_v1", episode, "personamem_v1:" + r["question_id"],
            r["user_question_or_message"], all_options=r["all_options"]) for r in records)
        targets = tuple(Target(q.qa_id, r["correct_answer"], V1_LEAVES.get(r["question_type"], ()),
            question_type=r["question_type"]) for q, r in zip(queries, records))
        yield Case(blocks("personamem_v1", episode, chat_blocks(source[:end])), queries, targets,
            {"cluster_id": first["persona_id"], "end_index_exclusive": end,
             "source_prefix_rule": "context[:end_index_in_shared_context]", "unknown_recording_dates": True})
        if selection.get("episode_limit") and count >= selection["episode_limit"]:
            return


def load_v2(config, **selection):
    from .native_contract import contract
    root = Path(config["data_dir"])
    groups = defaultdict(list)
    for i, row in enumerate(csv_rows(root / "benchmark/text/benchmark.csv")):
        row["_id"] = f"personamem_v2:{i}"
        episode = row["persona_id"] + ":" + row["chat_history_32k_link"]
        if selected(config, row["_id"], episode, selection):
            groups[episode].append(row)
    for count, (episode, records) in enumerate(groups.items(), 1):
        if selection.get("question_limit"):
            records = records[:selection["question_limit"]]
        first = records[0]
        path = safe_link(root, first["chat_history_32k_link"])
        source = json.loads(path.read_text())
        messages = source["chat_history"] if isinstance(source, dict) else source
        queries = tuple(Query("personamem_v2", episode, r["_id"], contract("personamem_v2").user_query_text(r),
            evaluation_mode="generative") for r in records)
        targets = tuple(Target(q.qa_id, r["preference"], V2_LEAVES.get(r["pref_type"], ()),
            question_type=r["pref_type"], scoring_data={"preference": r["preference"],
                "response_adherence_proxy_only": r["pref_type"] in {"ask_to_forget", "sensitive_info"}})
            for q, r in zip(queries, records))
        yield Case(blocks("personamem_v2", episode, chat_blocks(messages)), queries, targets,
            {"cluster_id": first["persona_id"], "history_file": str(path), "unknown_recording_dates": True,
             "complete_source_chat": True, "mode": "generative"})
        if selection.get("episode_limit") and count >= selection["episode_limit"]:
            return
