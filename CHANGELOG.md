# Changelog

## reproduction-v1 — 2026-10-06

No original algorithm contribution is claimed. No algorithm optimization, tuning, model call, benchmark run, official Smoke/Full or deployment occurred.

### Imported unchanged

- `app/` from ActiveMemoryIndex `academic-v4-20261005-rc3` local snapshot, except the validation-only patch below.
- `tests/` (26 files), `scripts/` (3 files), `LICENSE`, `requirements.txt`, `.env.academic.example`.
- Upstream README/RELEASE/SUBMISSION/Dockerfile/docker-compose.yml/.dockerignore under `upstream/`. Docker files are reference copies, not a tested reproduction deployment.
- Did not copy bench, datasets, results, docs or source `.gitignore` into the runtime tree. Complete original file hash inventory is retained for provenance.

### AML Textual contract correction

- `app/main.py`: constrain message roles to `user` / `assistant`; reject empty or whitespace-only content with 422 before any model/store operation. Preserve valid content unchanged. This prevents successful empty Add writes and follows the project's current Textual API contract. Exact diff: `upstream/LOCAL_CHANGES.patch`.
- No change to raw+facts, fact extraction/prompt, recall query/prompt, fusion weight, raw-first, neighbour window or update logic.

### Environment and execution additions

- `.env.example`: copied academic parameters; local DB path changed from `/data/memory-v4.sqlite3` to `./data/memory-v4.sqlite3`; credentials remain blank. Launch comments use the local guarded entrypoint; the failure comment clarifies partial verifier behavior. Original `.env.academic.example` retained unchanged.
- `requirements-academic.txt`: retain upstream runtime pins; omit unused local BGE/reranker/HuggingFace stack; explicitly pin HTTPX 0.28.1 for local ASGI tests.
- `requirements-academic.lock.txt`: freeze installed transitive versions on Python 3.10.20. Original requirements retained unchanged.
- `reproduction/__init__.py`, `serve.py`: separate fake and explicitly authorized academic launch modes, single worker/loopback only, explicit env loading, user-writable database. Fake SDK operates at transport boundary; independent fake identity and blocked outbound sockets. Fake fixtures are not algorithm modifications or quality evidence.
- `reproduction/run_tests.py`: sanitized environment, outbound socket guard, separate processes for upstream standalone tests, local logs and exit-code summary.
- `reproduction/check_http.py`: fake live HTTP contract, authentication, persistence/restart and isolation tests; wraps upstream local contract script only against the owned fake service. Tokens generated in memory. Temporary servers and databases cleaned up.
- `.gitignore`: exclude venv, real env files, databases, logs, bytecode, artifacts and common credential files.
- Environment created at `.venv/` from existing Conda Python 3.10.20; no sudo and no existing Conda environment modification. Installation used the configured PyPI mirror; no model weights downloaded.

### Documentation and evidence additions

- `README.md`, `ATTRIBUTION.md`, `CHANGELOG.md`, `REPRODUCTION_STATUS.md`, `EVALUATION_ACCESS_DRAFT.md`.
- `upstream/MANIFEST.sha256.json`, `upstream/LOCAL_CHANGES.patch` for exact provenance/diff.
- Local-only, ignored `artifacts/` contains upstream test output, HTTP logs and summaries; no model credentials or authentication token is logged.

### Validation

- 26 upstream test files passed; combined output contains 330 PASS labels/unittest cases (different test styles, not 330 API-only tests).
- 37 reproduction HTTP assertions plus 38 upstream local contract assertions passed with fake providers, including restart persistence/idempotency.
- `pip check` passed; academic launcher without paid opt-in rejects startup before importing the app.
- Initialized an independent local Git repository and froze reproduction-v1 with an automation-authored commit/tag; no public remote or push.
- Initial restricted-sandbox tests could not create sockets; subsequent authorized local runs passed. Initial new response test incorrectly required optional `created_at`; corrected the test to match the contract, without changing upstream response behavior.
- Source hash comparison: original ActiveMemoryIndex snapshot unchanged.
