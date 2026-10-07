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


## Post-v1 preparation — authorized minimal real integration (not yet executed)

- Updated status with the user's 1 Add / 1 Search synthetic-only authorization and separate 5-request LLM/embedding hard caps; stop on first provider/network/model error.
- Recorded the user's independently verified Cloudflare Quick Tunnel connectivity; no tunnel operation performed here.
- Created an ignored private .env from the public template, mode 600 and blank credentials, with automatic retries disabled. No secret entered, no paid request made. Versioned algorithm/config templates remain unchanged.

## Provider configuration preparation — no real API calls

- Private ignored `.env`: set LLM endpoint to the user-provided `https://linkapi.ai/v1`; set embedding endpoint to the user-provided full native URL `https://maas.qianwenaiapi.com/api/v1/services/embeddings/text-embedding/text-embedding`. Preserve credential values without printing them; retain 1024 dimensions and zero automatic retries.
- Added opt-in `AMI_REPRO_EMBED_PROTOCOL=dashscope-native` in private config. Upstream `AMI_EMBED_BACKEND=openai` remains the remote embedding processing path; the reproduction launcher replaces only its HTTP client factory.
- Added `reproduction/native_embedding.py`: HTTPX bridge from the existing embed client signature to DashScope native `input.texts` / `parameters.dimension` request and `output.embeddings[].text_index` response. Adds native transport identity to prevent database-space confusion. No changes to app/, prompts, batching, splitting, vector normalization, retrieval or update logic. No DashScope SDK dependency installed.
- `reproduction/serve.py`: explicit native transport opt-in and client cleanup; fake mode and default OpenAI-compatible path remain separate. Ordinary serving is not the authorized capped integration runner and must not yet be used for the real one-shot check.
- Added `reproduction/test_native_embedding.py`: six in-memory MockTransport unit tests passed, covering wire format, upstream index/normalization/dimension validation, no retries/redirects on HTTP errors, network failure redaction and model mismatch. Initial fixture inherited the upstream BGE default; test fixture corrected to explicitly set v4. No provider/network calls were made.
- Updated README, attribution and status with the protocol adaptation and its validation limits. Original reproduction-v1 tag remains unchanged.

## Beijing OpenAI-compatible configuration correction

- User clarified that embedding uses an Alibaba Cloud Bailian Beijing API key and the workspace-specific official OpenAI-compatible URL. Private config now selects `AMI_REPRO_EMBED_PROTOCOL=openai`, `AMI_EMBED_BACKEND=openai`, `text-embedding-v4`, 1024 dimensions. The previous native bridge is retained but inactive; no app/ or algorithm changes.
- The supplied example's `qwen3.7-text-embedding` model is not adopted: the authorized challenge model remains `text-embedding-v4`. LLM remains gpt-4o-mini at the user's previously supplied gateway. Both retry settings remain zero.
- WorkspaceId has not been provided. The private URL contains the explicit `{WorkspaceId}` placeholder, not a guessed endpoint. The launcher now rejects unresolved URL placeholders before app/database/network startup; a synthetic configuration-only check passed.
- Launcher accepts either AMI_EMBED_API_KEY or DASHSCOPE_API_KEY in the explicit private env file, matching upstream config's existing alias behavior; AMI_EMBED_API_KEY takes precedence. Credential values were preserved and never output. Real API counts remain zero.


### Workspace URL 已补齐

用户已提供北京工作空间完整 OpenAI-compatible URL，已替换私密 .env 中的占位符；本地 URL 格式检查通过。模型仍为 text-embedding-v4、1024 维、零重试，native bridge 关闭，凭据未改动或输出。此操作未验证账户授权或真实连通性，实际 LLM/Embedding API 调用仍各 0 次。下一步仍须通过有全局计数/硬上限/首错中止的专用入口执行已授权的单次 synthetic Add/Search；不直接运行普通 serve 入口。

## Authorized one-shot real integration — stopped on model verification

- Added `reproduction/real_minimal.py`: 5+5 global request caps, serialized provider I/O, zero retries/redirects, first-error global stop, strict requested/returned model and response structure checks, sanitized reporting, store-write guard, and a persistent one-shot lock. Exercises actual startup and ASGI routes using synthetic data only; does not deploy an HTTP/public service.
- Added `reproduction/test_real_minimal.py`: six in-memory safety tests passed (no real API calls).
- Executed the authorized attempt once: embedding warm-up 1 request (HTTP 200, text-embedding-v4, 3 tokens); LLM 1 request (HTTP 200, response model verification failed). One Add returned local 503, no rows committed, zero Search requests and zero retries. Stopped immediately; no repeat call or alias relaxation.
- Exact response model was not retained by the sanitized report, so missing/alias/different-model cases remain unresolved. LLM usage was not captured before the stop; no precise LLM cost claimed.
- Updated status with outcomes and diagnostic limitations. Real credentials remain solely in the ignored private config; no secret included in this change or any commit. Upstream remains unchanged; reproduction-v1 tag has not moved.

## Confirmed GPT-4o mini snapshot compatibility fix

- Added `reproduction/diagnose_model.py`, a separately authorized one-request synthetic metadata probe (max_tokens 8, no retry/redirect, persistent one-shot lock). Captures only sanitized model identifier and numeric usage, never response body or credentials.
- One real LLM diagnostic returned HTTP 200, requested `gpt-4o-mini`, returned `gpt-4o-mini-2024-07-18`, usage 11 input / 2 output tokens. No embedding/Add/Search request added. Cumulative real API attempts: LLM 2, embedding 1.
- OpenAI official model page confirms that returned identifier is a GPT-4o mini snapshot. Fixed only reproduction/real_minimal.py's overstrict response-name check: explicit set {gpt-4o-mini, gpt-4o-mini-2024-07-18}; no arbitrary prefix or unknown snapshot accepted. Requested model and all algorithmic settings remain unchanged.
- Added two offline guard regression cases; all 8 guard tests pass. Accepted response identifier and pre-stop numeric usage are retained in sanitized reports. Original failed attempt is preserved, not rewritten as a successful run; original run lock not removed.
- Updated status and README with the confirmed diagnostic and remaining unverified full chain. Used official OpenAI documentation: https://developers.openai.com/api/docs/models/gpt-4o-mini .

## Authorized minimal real Add/Search completion — passed

- Added an explicit `--authorized-completion` path in reproduction/real_minimal.py for the user's renewed authorization. Keeps original one-shot locks and reports; reads the original+diagnostic request counts and subtracts them from the 5+5 cumulative caps. A separate fixed output directory and exclusive lock prevent rerunning this completion attempt automatically.
- Added a historical-budget regression test; all 9 offline guard cases pass. No app/ algorithm, prompt, model-request name or academic retrieval setting changed.
- Continued the single launched process when the user asked to resume; no second launch. One synthetic Add and one Search both returned HTTP 200. SQLite held one raw turn, one fact and one request ledger row; Search returned two memories including the synthetic fact.
- New real requests: LLM 3 (1022 input / 66 output tokens), embedding 3 (76 tokens including warm-up). Historical total: LLM 5 / embedding 4. Zero retries, no budget increase, no benchmark/official Smoke/Full/public action.
- Estimated current-run cost using user-provided RMB prices: ¥0.0002309 before gateway discount; ¥0.00013445 if the earlier 0.5x LLM factor applies. Not a billing receipt.
- Updated README/status to lead with the completed minimal real validation while retaining historical failures and evidence boundaries. Real calls stopped after success; secrets and runtime databases remain ignored; original reproduction-v1 tag unchanged.

## reproduction-v1.1 — teammate handoff preparation (2026-10-07)

- Freeze post-v1 launch/transport adapters, capped synthetic integration tools, snapshot validation fix, and completed validation records in a new local version; no new model/API calls.
- Update public .env.example to the tested OpenAI-compatible protocol with a Beijing WorkspaceId placeholder, zero automatic retries and the previously selected LLM gateway. All credentials stay blank; upstream .env.academic.example remains unchanged. Private .env is untouched.
- Refresh EVALUATION_ACCESS_DRAFT.md with completed real-model validation and remaining publication/service fields.
- Add TEAM_HANDOFF.md with dependency setup, free tests, key separation, publication steps and remaining evaluation-access material.
- Export a source-only ZIP from the frozen Git revision, excluding private config, secrets, environment, databases, artifacts, logs and .git. GitHub repository creation/push not executed: no GitHub CLI/API authentication configured locally.
