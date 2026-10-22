"""Validate explicitly recorded execution changes against the frozen baseline.

The prepared data/scoring contract stays immutable. An overlay lists exact
before/after hashes and keeps original files outside src/sources. Every other
file must still match the original contract. This is not an implicit bypass.
"""
from pathlib import Path

from .artifacts import file_hash, read_json

RECEIPT = "execution_overlay.json"


def find_overlay(path):
    path = Path(path).resolve()
    for root in (path, *path.parents):
        receipt = root / RECEIPT
        if receipt.is_file():
            value = read_json(receipt)
            if value.get("version") != 1 or value.get("activation") != "new_memory_workers":
                raise ValueError("Invalid execution overlay")
            return root, value
    return None


def checked_changes(root, receipt):
    changes = receipt["changes"]
    for relative, change in changes.items():
        path = root / relative
        if path.resolve().is_relative_to(root.resolve()) is False:
            raise ValueError("Execution overlay path escaped its queue")
        if file_hash(path) != change["after_sha256"]:
            raise ValueError("Execution overlay file changed")
        if change["before_sha256"] is not None:
            original = root / change["original"]
            if not original.resolve().is_relative_to(root.resolve()) or file_hash(original) != change["before_sha256"]:
                raise ValueError("Original execution overlay file changed")
    return changes


def verify_framework_overlay(framework_root, expected, actual):
    overlay = find_overlay(framework_root)
    if not overlay:
        raise ValueError("Framework changed: prepare a new input contract")
    root, receipt = overlay
    if receipt["baseline_framework_sha256"] != expected["sha256"]:
        raise ValueError("Execution overlay baseline differs")
    changes = checked_changes(root, receipt)
    restored = dict(actual["files"])
    for relative, change in changes.items():
        path = root / relative
        if not path.is_relative_to(framework_root):
            continue
        name = str(path.relative_to(framework_root))
        if not name.startswith(("src/", "sources/")):
            continue
        if restored.get(name) != change["after_sha256"]:
            raise ValueError("Overlay framework hash differs")
        if change["before_sha256"] is None:
            if name in expected["files"]:
                raise ValueError("Added overlay file existed in baseline")
            restored.pop(name)
        else:
            if expected["files"].get(name) != change["before_sha256"]:
                raise ValueError("Overlay original hash differs from contract")
            restored[name] = change["before_sha256"]
    if restored != expected["files"]:
        raise ValueError("Framework has changes outside the recorded execution overlay")


def verify_frozen_file(path, expected):
    path = Path(path).resolve()
    if file_hash(path) == expected:
        return
    overlay = find_overlay(path.parent)
    if overlay:
        root, receipt = overlay
        changes = checked_changes(root, receipt)
        change = changes.get(str(path.relative_to(root)))
        if change and change["before_sha256"] == expected:
            return
    raise ValueError("Frozen execution file changed")
