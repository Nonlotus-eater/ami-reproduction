"""Full chronological conversations; captions are an explicit input policy."""
from pathlib import Path
import json

from ...runtime.artifacts import rows
from ...schemas import Case, History, Message, Query, Session, Target, timestamp


def load(config, *, episode_ids=None, question_ids=None, episode_limit=None, session_limit=None,
         question_limit=None, require_supported=False):
    root = Path(config["data_dir"])
    policy = config.get("media_policy", "text_only")
    if policy not in {"text_only", "text_with_captions"}:
        raise ValueError("Unknown LoCoMo media policy")
    grouped = {}
    for q in rows(root / "questions.jsonl"):
        if policy == "text_only" and q.get("is_multi_modality"):
            continue
        if question_ids is not None and q["qa_id"] not in question_ids:
            continue
        grouped.setdefault(q["sample_id"], []).append(q)
    mapping = json.loads(Path(__file__).with_name("capability_map.json").read_text())["categories"]
    count = 0
    for conversation in rows(root / "conversations.jsonl"):
        ident = conversation["sample_id"]
        if episode_ids and ident not in episode_ids:
            continue
        sessions = []
        for raw in conversation["sessions"]:
            when = timestamp(raw["date_time"])
            messages = []
            for m in raw["messages"]:
                text = m["text"].strip()
                caption = m.get("blip_caption")
                if policy == "text_with_captions" and caption:
                    text += "\n[Provided image caption: " + str(caption) + "]"
                if text:
                    messages.append(Message(m["dia_id"], m["role"], m["speaker"], text, when))
            sessions.append(Session(f"{ident}:session:{raw['session_index']}", when,
                                    raw["date_time"], tuple(messages)))
        sessions.sort(key=lambda s: s.timestamp)
        if session_limit:
            sessions = sessions[:session_limit]
        source_ids = {m.source_id for s in sessions for m in s.messages}
        selected = [q for q in grouped.get(ident, []) if not require_supported or
                    bool(q.get("evidence")) and set(q["evidence"]) <= source_ids]
        if question_limit:
            selected = selected[:question_limit]
        if not selected:
            continue
        history = History("locomo_refined", ident,
                          (conversation["speaker_a"], conversation["speaker_b"]), tuple(sessions))
        queries = tuple(Query("locomo_refined", ident, q["qa_id"], q["question"]) for q in selected)
        targets = tuple(Target(q["qa_id"], q["answer"], (mapping[str(q["category"])],),
                               tuple(q.get("evidence", [])), str(q["category"])) for q in selected)
        yield Case(history, queries, targets)
        count += 1
        if episode_limit and count >= episode_limit:
            return
