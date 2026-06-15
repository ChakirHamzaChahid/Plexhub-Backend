# Clean-Room Audit — PlexHub Backend — 2026-06-15

> **Table-rase, independent diagnostic.** Judged only on the code at HEAD `3c8beef` + a running/in-process instance — no prior audit was read (anti-anchoring). Read-only on application code. Fresh `CR-*` finding IDs.
>
> Method: 6 parallel dimension auditors. Benchmark: `pytest -v` + in-process ASGI probes against the **real 170 MB `data/plexhub.db`** (102,721 media rows). Native `uvicorn` boot is blocked on Windows by the POSIX-only `fcntl` master election (`app/main.py:196`, piège #7) — the app was therefore exercised in-process (ASGI), which runs the real handlers identically; `GET /api/health` → **200** confirmed.

## Dimension reports
- [01-architecture.md](01-architecture.md) — Architecture & module boundaries (§2)
- [02-conventions-debt.md](02-conventions-debt.md) — Conventions (§3) & open debt (§10)
- [03-flows.md](03-flows.md) — Key flows end-to-end (§5)
- [04-security.md](04-security.md) — Security
- [05-performance.md](05-performance.md) — Performance & latency
- [06-tests.md](06-tests.md) — Tests & coverage

## Scorecard

| Dimension | Score /100 | P0 | P1 | P2 | dette | Total |
|---|---:|---:|---:|---:|---:|---:|
| Security | **38** | 2 | 2 | 4 | 2 | 10 |
| Tests & coverage | **58** | 1 | 3 | 4 | — | 8 |
| Conventions & debt (§3/§10) | **68** | 0 | 3 | 3 | 8 | 14 |
| Performance & latency | **68** | 0 | 3 | 4 | 3 | 10 |
| Architecture (§2) | **72** | 0 | 3 | 7 | 1 | 11 |
| Flows end-to-end (§5) | **72** | 0 | 6 | 17 | 2 | 25 |
| **TOTAL** | **~62 (weighted)** | **3** | **20** | **39** | **16** | **78** |

**One-line verdict:** internally well-engineered (resilient sync/validation pipeline, clean crypto/pairing core, disciplined async + DB-session conventions) but **ships with a critically exposed surface** (unauthenticated mutation + arbitrary filesystem write), a **dead CI quality gate** (red on `main`), and **model↔DB / test↔code drift**.

## Top-10 priorities (cross-dimension)

| # | ID | Sev | Finding | Dim |
|---|---|---|---|---|
| 1 | **CR-S01** | P0 | Entire catalog/mutation/admin API is unauthenticated — only `/api/ai/*` + `POST /api/tv-auth/approve` check `X-API-Key`. Anyone can `DELETE /api/accounts/{id}`, trigger pipelines, browse `/admin`. | Security |
| 2 | **CR-S02** | P0 | `POST /api/plex/generate` = unauthenticated **arbitrary filesystem write**: client `outputDir` flows verbatim into `LocalStorage` with no containment (`plex.py:34-44`, `storage.py`). Proven by writing a file outside any intended dir. | Security |
| 3 | **CR-T01 / CR-C01** | P0 | **CI is RED on `main`.** `test_ai_status.py:113` asserts the obsolete model name `intfloat/multilingual-e5-small`; code returns `…MiniLM-L12-v2` (changed in `b6ad94e`, test not updated). CI deselects only the base64 flaky test, not this one → green-build signal is dead; regressions ship silently. | Tests |
| 4 | **CR-F24** | P1 | Boot `initial_sync_then_enrich` and the interval pipeline share **no mutual exclusion** (`max_instances=1` only self-serializes the interval). On a slow first sync they overlap → 2 concurrent enrichment/validation/Plex passes corrupt `.plex_mapping.json` (last-writer-wins) and double-spend the TMDB budget. | Flows |
| 5 | **CR-F01** | P1 | **Delisted episodes are never cleaned up** — cleanup exists for movie/show/live, none for `type=episode`; `map_episode_to_media` sets no `dto_hash`. On 77,781 episodes this is the largest silent orphan-drift source. | Flows |
| 6 | **CR-F07** | P1 | `ENRICHMENT_DAILY_LIMIT` **under-counts real TMDB calls** — `api_used` is a static per-call estimate decoupled from `_request`'s 4-attempt retry loop; under rate-limiting the worker spends 2–4× the budgeted ceiling (contradicts "compté en appels réels"). | Flows |
| 7 | **CR-F02** | P1 | `differential_cleanup(filter="all")` can **mass-delete on a partial** (not just total) fetch failure → catalogue wipe on a flaky upstream page. | Flows |
| 8 | **CR-A01** | P1 | On-demand pipeline endpoints (`/api/sync/*`, `/api/plex/generate`, accounts) run on **any worker with no master-gate** → in multi-worker Docker a slave duplicates the master's pipeline and double-writes SQLite. | Architecture |
| 9 | **CR-P01 / CR-P02** | P1 | Media-list COUNT is a **full scan (~18ms of the 29ms handler time)**; the 4 compound indexes declared in `models/database.py:102-105` are **missing from the live DB** (model↔DB index drift — migrations own a different index set). Sorted/deep pagination degrades to **325ms @ OFFSET 10000**. | Perf |
| 10 | **CR-S04 + CR-S03/CR-D05** | P1 | Xtream **passwords stored plaintext** at rest & copied into backups; **CORS default `*`** + wildcard methods/headers; **no rate limiting** on tv-auth `approve`/poll (userCode brute-force). | Security |

**Notable runners-up:** CR-F14 (generator blanket `tvshow.nfo`/`poster.jpg` delete keyed off an episode's parent dir); **tv-auth contract bug** — `GET /status` expects snake_case `device_code` query param while the rest of the API is camelCase `deviceCode` (`tv_auth.py:314-317`) → 422 for a contract-consistent TV client; CR-A02 (Plex-gen orchestration triplicated & divergent across `main.py`/`plex.py`/`cli.py`); CR-A03 (business logic + raw DB access in 4 routers); CR-F08 (imdb-only items burn 3 attempts despite `find_by_imdb_id`); fcntl Windows-boot blocker (Linux/Docker-only by design).

## Roadmap

**Wave 0 — Stop the bleeding (P0, ~days)**
1. Restore the CI gate: update the stale `test_status_shape` model-name assertion → green `main` (CR-T01).
2. Authenticate (or network-isolate) all mutating + admin routers (CR-S01); apply `verify_api_key` beyond `/api/ai`.
3. Contain `outputDir` (reject absolute/`..`, anchor under a base dir) + require auth on `/api/plex/generate` (CR-S02).

**Wave 1 — Data integrity & secrets (P1, ~1–2 wks)**
4. Pipeline mutual-exclusion (single global lock across boot + interval) (CR-F24).
5. Episode cleanup + `dto_hash` on episodes (CR-F01); guard `differential_cleanup` against partial-fetch wipes (CR-F02); fix generator blanket delete (CR-F14).
6. Count real TMDB calls including retries (CR-F07); short-circuit imdb-only items via `find_by_imdb_id` (CR-F08).
7. Encrypt Xtream passwords at rest (CR-S04); lock down CORS in prod; add rate limiting to tv-auth (CR-S03/CR-D05).

**Wave 2 — Perf & architecture (P1/P2, ~1–2 wks)**
8. Reconcile model↔DB indexes via migration; add the missing compound indexes; avoid the double-COUNT / adopt keyset pagination (CR-P01/P02/P03).
9. Master-gate on-demand pipeline endpoints (CR-A01).
10. Extract a `PlexGenerationService` (kill triplicated orchestration, CR-A02); move business logic + raw DB out of routers (CR-A03).

**Wave 3 — Quality infra & debt (ongoing)**
11. Wire `ruff` + coverage; tests for `health_check_worker` & `sync_worker` (CR-T02/T03); fix mis-marked non-async tests (CR-T06).
12. Align Docker 3.12 ↔ CI 3.13; decide on `pydantic-settings` (use or drop); typed responses for raw-dict endpoints (CR-C02/C03); tv-auth camelCase contract consistency; fcntl portability shim or explicit Linux-only doc.

## Follow-up (separate, NOT done here)
This audit is **read-only**. The cartography it produced (these 6 reports + this index) is the base for updating `docs/architecture/ARCHITECTURE.md` and the stale `CLAUDE.md` banner (currently `1da2ab9`, real HEAD `3c8beef`) — to be done via **`/refresh-context`** or a dedicated pass, after this audit.
