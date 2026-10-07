# Attribution and reuse disclosure

## Identity and scope

This repository is **ActiveMemoryIndex reproduction-v1**, a reproduction maintained within the Agent-Memory project. It is not the upstream author's official release and makes **no original algorithmic contribution claim**.

- Original author: **Xuhao Lin** (as credited by upstream README and LICENSE).
- Original repository: https://github.com/linxuhao/ActiveMemoryIndex
- Source used: local `ActiveMemoryIndex-main/` snapshot, inspected 2026-10-06; upstream documents identify `academic-v4-20261005-rc3`.
- The source snapshot has no `.git` history. The exact upstream commit is **unknown**; no commit hash is invented, and the release label is not treated as a verified Git tag.
- `upstream/MANIFEST.sha256.json` records SHA-256 for all 105 source files. Manifest SHA-256: `1bdeb0bd94bcc8d4989e1daf8a28730a970f55da67386ffb0ff0cbb287e58ade`.
- Original MIT copyright notice is preserved verbatim in `LICENSE`: Copyright (c) 2026 Xuhao Lin.

## Reused implementation

The complete `app/`, `tests/`, and `scripts/` directories are copied from that snapshot. Reused work includes the API and schemas, raw/fact construction, extraction and recall prompts, fusion/ranking/ordering/neighbour logic, explicit-update prompts/detector/verifier/router, embedding adapter, SQLite store, identity and idempotency machinery, retry/admission logic, and optional research branches.

`requirements.txt` and `.env.academic.example` are retained verbatim. Upstream README, RELEASE, SUBMISSION and container files are retained under `upstream/` as attributed historical source documents, not statements of our authorship, deployment or test results. The source snapshot's bench/results/docs are not included in this runtime repository. No benchmark datasets or gold answers are included.

The **only modified upstream program file** is `app/main.py`: Textual role enumeration and nonblank content validation. The exact diff is in `upstream/LOCAL_CHANGES.patch`; valid inputs retain their original strings. All algorithm code and prompts are unchanged. New environment/testing/documentation files and dependency adaptations are enumerated in `CHANGELOG.md`.

## Upstream prior work

Upstream credits **Xuhao Lin, _An Index, Not a Store: The Model Does Remember — It Just Needs Its Notebook_ (2026)**:

- https://doi.org/10.5281/zenodo.21405963
- https://github.com/linxuhao/index-not-store

These bibliographic references are transcribed from the supplied upstream README, not independently verified during reproduction. Its research results and AMI's benchmark results remain the original author's findings; we have not reproduced their quality scores. The LoRA variant discussed in that prior work is not implemented here.

Upstream's research harness references LoCoMo (https://github.com/snap-research/locomo) and AML's public evaluator (https://github.com/AML-memory/agent-memory-leaderboard). We do not vendor or run either harness/data in reproduction-v1.

## Components and disclosure boundaries

Runtime relies on FastAPI, Uvicorn, Pydantic, NumPy, SQLite, the OpenAI Python SDK and HTTPX. The academic design uses provider APIs `gpt-4o-mini` and `text-embedding-v4`; neither model is bundled or called in this phase. Upstream's optional SentenceTransformers/BGE and cross-encoder paths remain in code but are not installed/enabled in the lightweight academic environment. Dependency licenses remain with their respective authors/distributions; this document is project disclosure, not a legal determination.

`reproduction/` contains newly authored launch/test scaffolding and explicitly fake SDK fixtures. It is engineering adaptation, not a new memory method. Fixture retrieval or contract success must not be described as real model performance. The project background ranks AMI highly, but no leaderboard rank is claimed for this reproduction.

Before a public release, bind this repository to its own reviewed commit, retain this disclosure and LICENSE, and disclose subsequent algorithm changes separately. Recovering the exact upstream commit, if possible, is still a provenance follow-up; the source file manifest already fixes the local snapshot used.

### Post-v1 transport adaptation

The optional `reproduction/native_embedding.py` bridge is our engineering adaptation for the user's supplied DashScope-native endpoint. It changes wire format only and keeps upstream embedding preprocessing, representation and retrieval unchanged. Its protocol mapping is based on the supplied curl example and Alibaba Cloud's [native embedding API documentation](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api). The user-supplied gateway host is not independently verified as an official provider. No original memory-algorithm claim or new model-quality claim follows from this adaptation. The reproduction-v1 tag still records the earlier baseline without this bridge.
