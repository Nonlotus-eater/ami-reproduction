"""Deterministic ordinary-text Add chunks, independent of method and targets.

AML publishes 20-message / 2,000 Adapter-counted-word boundaries, but not its
private word counter. This version freezes Unicode whitespace words in source
text. Native method prompts/prefixes are not source words. Sessions never merge.
"""
from dataclasses import dataclass
import re

from ..runtime.artifacts import contract_hash
from ..schemas import Message, Session

POLICY = "aml_20_messages_2000_words_v1"
MAX_MESSAGES = 20
MAX_WORDS = 2000


@dataclass(frozen=True)
class AddChunk(Session):
    # A logical write ID differs between chunks; the source session_id does not.
    chunk_id: str = ""


def word_count(text):
    return len(text.split())


def message_parts(message):
    words = list(re.finditer(r"\S+", message.text))
    starts = [0] + [words[i].start() for i in range(MAX_WORDS, len(words), MAX_WORDS)]
    ends = starts[1:] + [len(message.text)]
    for start, end in zip(starts, ends):
        part = Message(message.source_id, message.role, message.speaker,
                       message.text[start:end], message.timestamp)
        yield part, {"source_id": message.source_id, "start": start, "end": end}


def plan(history, policy=None):
    if policy is None:
        return history.sessions, {"policy": "source_sessions", "chunks": len(history.sessions)}
    if policy != POLICY:
        raise ValueError("Unknown Add chunk policy")
    chunks, audit = [], []
    for session in history.sessions:
        pending, spans, words = [], [], 0

        def flush():
            nonlocal pending, spans, words
            if not pending:
                return
            chunk_id = contract_hash([POLICY, session.session_id, len(chunks), spans,
                                      [m.text for m in pending]])
            chunk = AddChunk(session.session_id, session.timestamp, session.raw_timestamp,
                             tuple(pending), session.source_session_id, chunk_id)
            chunks.append(chunk)
            audit.append({"chunk_id": chunk_id, "session_id": session.session_id,
                          "messages": len(pending), "words": words, "source_spans": spans,
                          "content_sha256": contract_hash([m.text for m in pending])})
            pending, spans, words = [], [], 0

        for message in session.messages:
            for part, span in message_parts(message):
                count = word_count(part.text)
                if pending and (len(pending) == MAX_MESSAGES or words + count > MAX_WORDS):
                    flush()
                pending.append(part)
                spans.append(span)
                words += count
                if len(pending) == MAX_MESSAGES or words == MAX_WORDS:
                    flush()
        flush()
    return tuple(chunks), {"policy": POLICY, "word_counter": "unicode_whitespace_source_text_v1",
                           "source_history_sha256": contract_hash(history.to_wire()),
                           "source_sessions": len(history.sessions), "chunks": audit,
                           "plan_sha256": contract_hash(audit)}
