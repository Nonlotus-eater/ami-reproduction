from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

def rows(path):
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("JSONL rows must be objects")
                yield obj

def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(target)

def contract_hash(manifest):
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()

def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def json_array_items(path, block_size=65536):
    """Decode one top-level array member at a time; keep one history in memory."""
    decoder = json.JSONDecoder()
    with Path(path).open(encoding="utf-8") as handle:
        buffer = ""
        eof = False
        def consume_value():
            nonlocal buffer, eof
            while True:
                buffer = buffer.lstrip()
                try:
                    value, end = decoder.raw_decode(buffer)
                    buffer = buffer[end:]
                    return value
                except json.JSONDecodeError:
                    if eof:
                        raise ValueError("invalid or truncated JSON array") from None
                    chunk = handle.read(block_size)
                    eof = not chunk
                    buffer += chunk
        def consume_character():
            nonlocal buffer, eof
            while not buffer.strip() and not eof:
                chunk = handle.read(block_size)
                eof = not chunk
                buffer += chunk
            buffer = buffer.lstrip()
            if not buffer:
                raise ValueError("truncated JSON array")
            char, buffer = buffer[0], buffer[1:]
            return char
        if consume_character() != "[":
            raise ValueError("expected a top-level JSON array")
        # Distinguish an empty array before attempting to decode the first item.
        first = consume_character()
        if first == "]":
            tail = buffer + handle.read()
            if tail.strip():
                raise ValueError("data after JSON array")
            return
        buffer = first + buffer
        while True:
            value = consume_value()
            if not isinstance(value, dict):
                raise ValueError("dataset members must be objects")
            yield value
            char = consume_character()
            if char == "]":
                if (buffer + handle.read()).strip():
                    raise ValueError("data after JSON array")
                return
            if char != ",":
                raise ValueError("missing JSON array separator")

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def write_rows(path, values):
    target=Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w",encoding="utf-8") as handle:
        for row in values:
            handle.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+"\n")

def append_row(path, value):
    with Path(path).open("a",encoding="utf-8") as handle:
        handle.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+"\n")
