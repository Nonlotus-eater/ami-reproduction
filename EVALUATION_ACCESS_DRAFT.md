# Evaluation Access Request — internal preparation draft

**Not submitted. Not an official AML form.** This is a project information sheet to transfer into the current platform form after user approval. No endpoint, approval status, capacity or score is invented.

| Field | Prepared information / remaining action |
|---|---|
| Project | ActiveMemoryIndex reproduction baseline, `reproduction-v1` |
| Target | Textual Memory / Open-source Methods |
| Method description | Raw turns plus extracted facts; original/recall dense-score fusion; same-chunk neighbours; raw-first evidence; conservative explicit-update withholding with historical memories retained |
| Originality disclosure | Reproduction of Xuhao Lin's ActiveMemoryIndex; no original algorithm contribution; full implementation reuse disclosed in ATTRIBUTION |
| Upstream | https://github.com/linxuhao/ActiveMemoryIndex ; local rc3 snapshot pinned by file manifest; upstream commit unknown |
| Our repository / version | Local repository only. Public URL and reviewed fixed commit to be provided after publication approval |
| Changes | Textual input validation; user-space runtime/dependency adaptation; offline fake verification; no algorithm changes |
| LLM | Intended `gpt-4o-mini`; real endpoint/credential not configured or tested |
| Embedding | Intended `text-embedding-v4`, 1024 dimensions; real endpoint/credential not configured or tested |
| Reranker | Disabled |
| Add / Search URLs | Public HTTPS URLs pending deployment approval; ephemeral loopback tests are not platform endpoints |
| Health | GET /health, anonymous liveness; future public URL pending |
| Authentication | Bearer; token exchanged through approved secure channel, never included in repository/draft |
| Storage | SQLite WAL, persistent writable directory, single worker; user_id isolation |
| Admission profile | Add 16 / Search valve off; wait 45s, retry-after 10s. These are upstream configuration, not measured production capacity |
| Suggested platform concurrency | Must be chosen after real-provider small-scale validation; no Full load capacity claimed |
| Local evidence | 26 upstream fake test files; 37 reproduction + 38 upstream local HTTP checks; persistence/restart/idempotency/isolation verified |
| Missing evidence | Real provider credentials and connectivity, minimal paid end-to-end check, HTTPS deployment, resource/latency characterization, official validation |
| Official Smoke / Full | Not run; separate user approval required |
| Submission | Only after the user reviews the completed actual platform fields and explicitly authorizes submission |

Before submitting, recheck the current AML requirements and deadlines. Current project knowledge: materials due 2026-10-31 23:59 UTC+8; evaluation stops 2026-11-04 23:59 UTC+8. Full is limited to two per AML Key per track, second opens 30 days after first completion; it is not a debugging tool. This sheet does not reserve access or spend quota.
