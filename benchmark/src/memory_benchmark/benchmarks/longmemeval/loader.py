"""Stream one independent question/history at a time; strip all gold labels."""
from pathlib import Path
from collections import Counter
import json

from ...runtime.artifacts import json_array_items
from ...schemas import Case, History, Message, Query, Session, Target, timestamp


def load(config, *, episode_ids=None, question_ids=None, episode_limit=None, session_limit=None,
         question_limit=None, require_supported=False):
    mapping = json.loads(Path(__file__).with_name("capability_map.json").read_text())
    count = 0
    for row in json_array_items(config["data_file"]):
        ident = row["question_id"]
        if episode_ids and ident not in episode_ids:
            continue
        if question_ids is not None and ident not in question_ids:
            continue
        ids, dates, histories = row["haystack_session_ids"], row["haystack_dates"], row["haystack_sessions"]
        if not len(ids) == len(dates) == len(histories):
            raise ValueError("LongMemEval session IDs/dates/history alignment is invalid")
        query_time = timestamp(row["question_date"])
        sessions = []
        counts = Counter(ids)
        future = 0
        for occurrence, (sid, raw_time, messages) in enumerate(zip(ids, dates, histories)):
            when = timestamp(raw_time)
            if when > query_time:
                future += 1
            unique = f"{sid}:occurrence:{occurrence}" if counts[sid] > 1 else str(sid)
            normalized = tuple(Message(f"{unique}:message:{i}", m["role"],
                {"user": "User", "assistant": "Assistant", "system": "System"}[m["role"]],
                m["content"], when) for i, m in enumerate(messages) if m["content"].strip())
            sessions.append(Session(unique, when, raw_time, normalized, str(sid)))
        sessions.sort(key=lambda s: s.timestamp)
        if session_limit:
            sessions = sessions[:session_limit]
        if require_supported and not set(row["answer_session_ids"]) <= {s.source_session_id for s in sessions}:
            continue
        leaf = mapping["abstention_leaf"] if ident.endswith(mapping["abstention_suffix"]) else mapping["question_types"][row["question_type"]]
        history = History("longmemeval_s", ident, ("User", "Assistant"), tuple(sessions))
        query = Query("longmemeval_s", ident, ident, row["question"], query_time)
        # Evidence IDs remain solely in this scoring target.
        target = Target(ident, row["answer"], (leaf,), tuple(row["answer_session_ids"]), row["question_type"])
        yield Case(history, (query,), (target,), {
            "duplicate_source_session_ids": sorted(k for k, v in counts.items() if v > 1),
            "sessions_after_question_time": future,
            "history_policy": "preserve_all_provided_sessions_then_sort_chronologically"})
        count += 1
        if episode_limit and count >= episode_limit:
            return
