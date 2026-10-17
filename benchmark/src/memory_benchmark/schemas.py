"""Explicit wire views prevent scoring labels from entering memory workers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re


def timestamp(value: str) -> str:
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        normalized = re.sub(r"\s*\([^)]*\)", "", raw)
        for fmt in ("%I:%M %p on %d %B, %Y", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError("Unsupported source timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class Message:
    source_id: str
    role: str
    speaker: str
    text: str
    timestamp: str | None

    def __post_init__(self):
        if self.role not in {"user", "assistant", "system"} or not self.source_id:
            raise ValueError("Invalid message role or source ID")
        if not isinstance(self.text, str) or not self.speaker:
            raise ValueError("Message text and speaker are required")


@dataclass(frozen=True)
class Session:
    session_id: str
    timestamp: str | None
    raw_timestamp: str
    messages: tuple[Message, ...]
    source_session_id: str | None = None

    @classmethod
    def from_wire(cls, row):
        return cls(row["session_id"], row["timestamp"], row["raw_timestamp"],
                   tuple(Message(**m) for m in row["messages"]), row.get("source_session_id"))


@dataclass(frozen=True)
class History:
    benchmark: str
    episode_id: str
    speakers: tuple[str, str]
    sessions: tuple[Session, ...]

    def to_wire(self):
        return asdict(self)

    @classmethod
    def from_wire(cls, row):
        return cls(row["benchmark"], row["episode_id"], tuple(row["speakers"]),
                   tuple(Session.from_wire(s) for s in row["sessions"]))


@dataclass(frozen=True)
class Query:
    benchmark: str
    episode_id: str
    qa_id: str
    text: str
    timestamp: str | None = None
    system_prompt: str = ""
    all_options: str = ""
    evaluation_mode: str = ""

    def to_wire(self):
        return asdict(self)


@dataclass(frozen=True)
class Target:
    qa_id: str
    gold: str | list
    capability_leaves: tuple[str, ...]
    evidence_source_ids: tuple[str, ...] = ()
    question_type: str = ""
    scoring_data: dict = field(default_factory=dict)

    def to_wire(self):
        return asdict(self)


@dataclass(frozen=True)
class Case:
    history: History
    queries: tuple[Query, ...]
    targets: tuple[Target, ...]
    dataset_audit: dict = field(default_factory=dict)

    def __post_init__(self):
        ids = [q.qa_id for q in self.queries]
        if not ids or len(ids) != len(set(ids)) or set(ids) != {t.qa_id for t in self.targets}:
            raise ValueError("Case query IDs and scoring targets must agree")


@dataclass(frozen=True)
class Evidence:
    id: str
    text: str
    source_ids: tuple[str, ...] = ()
    timestamp: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id or not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Evidence requires a nonempty ID and text")

    def to_wire(self):
        return asdict(self)

    @classmethod
    def from_wire(cls, row):
        return cls(row["id"], row["text"], tuple(row.get("source_ids", [])),
                   row.get("timestamp"), row.get("metadata", {}))
