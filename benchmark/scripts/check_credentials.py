"""Reject staged credential files/values without echoing secret contents.

Uses Git's index, not working-tree contents. --history reports whether old
credential-bearing env blobs still exist; it never rewrites repository history.
"""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess

SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)
TOKEN = re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}")
PLACEHOLDER = re.compile(r"^(?:|none|null|changeme|replace.*|your[-_ ].*|<.*>|\$\{.*\}|.*(?:placeholder|example|fixture|dummy).*)$", re.I)

# Two public PersonaMem-v2 sensitive_info targets contain generated key-shaped
# strings. Only these exact source-derived files may bypass the shape heuristic;
# known local/history credential values and private env files always fail.
# Upstream revision ed956dea41521fc4499acbc63f966e0fd3c053ba, CSV rows 381/1852.
PUBLIC_FIXTURE_FILES = {
    'samples/proportional-200-v1/prepared/seed-17/inputs/personamem_v2/bff6f437ff1754ca9e5745d8/targets.eval.jsonl':
        '6c093ac1c774772ff94f9def01e43a8dea7b0a7700b391242e8af25ba68804ab',
    'samples/proportional-200-v1/prepared/seed-20/inputs/personamem_v2/886078ab239faa0b1c640287/targets.eval.jsonl':
        'f33aed39566c63fb01a9627c22c7db1e164ba0106fe9b9945ab4e60463db26fb',
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def credentials(content):
    result = set()
    for line in content.decode("utf-8", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip().strip("\"'")
        if SECRET_NAME.search(key) and len(value) >= 12 and not PLACEHOLDER.fullmatch(value):
            result.add(value.encode())
    return result


def known_credentials(root):
    root = Path(root)
    secrets, historical_blobs = set(), set()
    env_paths = (".env", "benchmark/.env")
    for name in env_paths:
        if (root / name).is_file():
            secrets.update(credentials((root / name).read_bytes()))
    # Scan historical env values too: removing a file from HEAD does not revoke
    # a key or make its old value safe to include elsewhere.
    try:
        commits = git(root, "log", "--all", "--format=%H", "--", *env_paths).splitlines()
    except subprocess.CalledProcessError:
        # A clean exported bundle intentionally has no Git history.
        commits = []
    for commit in commits:
        for name in env_paths:
            try:
                blob = git(root, "show", commit.decode() + ":" + name)
            except subprocess.CalledProcessError:
                continue
            values = credentials(blob)
            secrets.update(values)
            if values:
                historical_blobs.add(git(root, "rev-parse", commit.decode() + ":" + name).strip())
    return secrets, historical_blobs


def inspect(root, *, history=False):
    root = Path(root)
    secrets, historical_blobs = known_credentials(root)
    failures, public_fixtures = [], []
    names = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
    for raw in filter(None, names):
        name = raw.decode("utf-8", errors="surrogateescape")
        basename = Path(name).name
        if (basename == ".env" or basename.startswith(".env.")) and basename != ".env.example":
            failures.append({"path": name, "reason": "private_env_file"})
            continue
        data = git(root, "show", ":" + name)
        known_secret = any(value in data for value in secrets)
        shaped_token = TOKEN.search(data)
        public_fixture = PUBLIC_FIXTURE_FILES.get(name.removeprefix('benchmark/')) == hashlib.sha256(data).hexdigest()
        if known_secret or (shaped_token and not public_fixture):
            failures.append({"path": name, "reason": "credential_value"})
        elif shaped_token and public_fixture:
            public_fixtures.append(name)
    return {"status": "failed" if failures or (history and historical_blobs) else "passed", "staged_findings": failures,
            "historical_credential_env_blobs": len(historical_blobs) if history else None,
            "values_printed": False, "history_rewritten": False,
            "verified_public_test_fixtures": public_fixtures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    import json
    root = git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip()
    result = inspect(root, history=args.history)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["staged_findings"] or (args.history and result["historical_credential_env_blobs"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
