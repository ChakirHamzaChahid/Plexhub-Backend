# CR — Conventions (§3) & Debt (§10)

**Dimension score: 68/100** — Conventions are mostly real and consistently applied at the data-model layer (Pydantic v2 camelCase, constant-time auth, no secret leaks, idempotent migrations, blocking-call offloading). The score is dragged down by a **broken-but-undetected CI** (a stale assertion fails on `main` yet CI only deselects a *different* test), a class of **raw-dict public responses** that violate the stated "no raw dict at the boundary" rule, **zero lint/format/type/coverage tooling**, and a documented-but-thin reliability convention (per-connection PRAGMAs).

**Mental model.** PlexHub leans on hand-rolled conventions rather than tooling: a bespoke `Settings` class (not pydantic-settings), Pydantic v2 schemas with a shared `to_camel` alias generator at every public boundary, a constant-time `verify_api_key` gating only the AI + tv-auth/approve surfaces, and a manual `commit_with_retry` discipline for SQLite write contention. The conventions that *are* enforced are enforced well and uniformly; the gaps are (a) no automated guardrails to keep them enforced over time, and (b) a handful of older routers (`sync`, `categories`, partially `media`) predating the schema discipline that still return raw dicts.

---

## Convention findings

### CR-C01 — CI is red on `main`: a stale test assertion fails and CI does not deselect it
- **Severity: P1**
- **Evidence:** `tests/test_ai_status.py:113` asserts `data["modelName"] == "intfloat/multilingual-e5-small"`, but the endpoint returns `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (`app/services/embedding_service.py:26`, surfaced via `app/api/ai.py:418`). `.github/workflows/tests.yml:32-33` runs `pytest -v` deselecting **only** `tests/test_utilities.py::TestBase64Decode::test_text_that_looks_like_base64_but_decodes_to_garbage` — it does NOT deselect `test_status_shape`. Verified locally: `297 passed, 1 failed` even after applying the exact CI deselect.
- **Impact:** The CI test job fails on every push/PR to `main`. The "green CI" signal is dead; real regressions will hide behind an already-red pipeline. The §3 convention "Pydantic v2 / contract tests" is silently unenforced.
- **Root cause:** Model name changed in commit `b6ad94e` (`MiniLM-L6 -> MiniLM-L12`); the diagnostic-status test asserting the old name was never updated. The model rename also predates a prior model (`e5-small`), so the test string is two generations stale.
- **Suggested fix:** Update the assertion to `embedding_service.DEFAULT_MODEL_NAME` (import it rather than hard-coding the string) so future renames can't desync. Then confirm CI is green and remove any stopgap deselect.

### CR-C02 — Raw dicts returned from public endpoints (violates "jamais de dict brut en réponse publique")
- **Severity: P2**
- **Evidence:** `app/api/sync.py` returns bare dicts on 8 endpoints: lines `20, 30, 39, 48, 57, 79, 96` (`{"jobId": ...}`, `{"message": ...}`, `{"jobs": ...}`). `app/api/categories.py:67` (`{"message": ...}`) and `:145-150` (`{"message", "vod_count", "series_count", "total"}`). `app/api/media.py:137` (`{"status": "queued"}`). None declare a `response_model`.
- **Impact:** No schema validation, no OpenAPI contract, no camelCase enforcement on these responses. Android clients must hand-parse untyped JSON; field renames silently break consumers. Directly contradicts the documented boundary convention (`CLAUDE.md §3`).
- **Root cause:** These routers predate the Pydantic-v2-schema discipline visible in `media`/`live`/`ai`/`tv_auth`; the job-trigger endpoints were written as quick 202 stubs.
- **Suggested fix:** Introduce `JobAcceptedResponse(job_id: str)` / `MessageResponse(message: str)` schemas (camelCase via `to_camel`) and wire `response_model=` on each endpoint.

### CR-C03 — camelCase boundary convention broken inside a raw dict response
- **Severity: P2**
- **Evidence:** `app/api/categories.py:145-150` returns `{"message": ..., "vod_count": ..., "series_count": ..., "total": ...}` — `vod_count`/`series_count` are **snake_case** in a public JSON body, unlike the rest of the API which is camelCase (`vodCount`/`seriesCount`).
- **Impact:** Inconsistent JSON casing across the API surface; an Android client expecting camelCase everywhere gets snake_case here. Subset of CR-C02 but called out because it is an active casing inconsistency, not just an untyped body.
- **Root cause:** Same as CR-C02 — no schema, so the alias generator never runs on these keys.
- **Suggested fix:** Same as CR-C02; the schema's `to_camel` generator fixes the casing automatically.

### CR-C04 — Runtime SQLite PRAGMAs (WAL, busy_timeout) set only on the init connection, not via the connect listener
- **Severity: P2 (reliability convention is thinner than §3 claims)**
- **Evidence:** `app/db/database.py:80-85` applies `journal_mode=WAL`, `busy_timeout=5000`, `synchronous`, `cache_size`, `mmap_size` inside a single `engine.begin()` in `init_db()`. The `connect` event listener (`database.py:38-54`) loads **only** sqlite-vec — it does NOT set any PRAGMA. `app/services/nfo_import_service.py:62-66` explicitly documents the model: "`PRAGMA busy_timeout` is per-connection and aiosqlite reuses one connection per session, so this only affects the import session — production paths keep the global 5 s default." `journal_mode=WAL` is a database-level (persistent) PRAGMA, so it survives; but `busy_timeout` is connection-scoped and is only guaranteed on the exact connection used during `init_db`.
- **Impact:** The §3 claim "WAL + busy_timeout=5000 globally" holds for WAL (persisted) but is fragile for `busy_timeout`: any pooled connection that was not the init connection may run with the SQLite default `busy_timeout=0`, making `database is locked` more likely and relying entirely on `commit_with_retry` to paper over it. The codebase's own comment (nfo_import) is the proof that the team knows PRAGMAs are per-connection.
- **Root cause:** PRAGMA application lives in one-shot `init_db()` rather than in the `@event.listens_for(..., "connect")` hook where it would apply to every connection.
- **Suggested fix:** Move `busy_timeout` (and ideally `synchronous`/`cache_size`/`mmap_size`) into the existing connect listener so every connection is configured identically; keep WAL there too for robustness. Removes the need for the nfo_import per-session workaround.

### CR-C05 — `version` string hard-coded in the health response instead of `APP_VERSION`
- **Severity: dette**
- **Evidence:** `app/api/health.py:31` returns `version="1.0.0"` as a literal. The single source of truth is `app/main.py:17` `APP_VERSION = "1.0.0"`. They agree today, but the health endpoint does not import it.
- **Impact:** Version drift: bumping `APP_VERSION` will not update `/api/health`, so observability/monitoring will report a stale version.
- **Root cause:** Copy-pasted literal rather than importing the constant.
- **Suggested fix:** `from app.main import APP_VERSION` (or move `APP_VERSION` to `config`/a `__version__` module to avoid the import cycle) and reference it.

### CR-C06 — `create_account` builds an Xtream auth probe with an ad-hoc empty class instead of a typed object
- **Severity: dette**
- **Evidence:** `app/api/accounts.py:50-56` defines `class TempAccount: pass` then attaches attributes dynamically; `app/main.py:141-145` does the same with `class _Acc:`. `xtream_service.authenticate(...)` is called with these duck-typed stand-ins.
- **Impact:** No type safety on the auth probe input; a future field rename in the real `XtreamAccount` won't be caught at the call site. Minor, but inconsistent with the otherwise-typed boundary discipline.
- **Root cause:** Avoiding construction of a full ORM object just to test credentials.
- **Suggested fix:** Use a small frozen dataclass / Pydantic model (e.g. `XtreamCredentials`) shared by both call sites and accepted by `authenticate`.

---

## What's solid (conventions confirmed against code)

- **Pydantic v2 at the boundary, camelCase via `to_camel` + `populate_by_name`** — uniformly applied across `models/schemas.py` (every response model), `api/ai.py:48`, `api/tv_auth.py:94`, `api/plex.py:16,25`. `response_model_by_alias=True` set on AI/tv-auth endpoints. Field-level validators present (`schemas.py:100-126` imdb/tmdb regex).
- **Constant-time auth** — `secrets.compare_digest` on byte-encoded keys in both `api/deps.py:42-45` and `api/tv_auth.py:80-83`; plain `==` is explicitly forbidden and the docstring references a grep-based acceptance test.
- **Three contractual AI 503 patterns** — verified: `deps.py:33-36` (not configured), `deps.py:37-41` (vec storage), `ai.py:216-219` / `:306-311` (model unavailable). tv-auth mirrors the config-503 (`tv_auth.py:75-79`).
- **No secrets in logs/responses** — TMDB key truncated to 4 chars + `****` (`config.py:76`); tv-auth logs session IDs, never the decrypted payload (`tv_auth.py:339,346`); `AccountResponse` (`schemas.py:169-189`) exposes `username` but **not** `password`. Boot summary logs sanitized config flags only (`main.py:210-221`). No `print()` in request paths (only `scripts/strip_titles_pollution.py:548`, a CLI tool).
- **`.env` gitignored** — `.gitignore` lists `.env`; `git ls-files` confirms only `.env.example` is tracked.
- **Migration idempotency** — `migrations.py` uses `CREATE TABLE/INDEX IF NOT EXISTS` and `ADD COLUMN` guarded by `try/except` with a warning (e.g. `001:42-67`, `002:74-82`, `003:91-103`). New migrations appended in order in `run_migrations()` (`migrations.py:20-31`), with M008 on a dedicated connection and M009 last.
- **Async I/O discipline** — every blocking call is offloaded: ONNX model load/inference via `asyncio.to_thread` (`embedding_service.py:78,124,130`), sqlite backup via `asyncio.to_thread(_run_backup)` (`main.py:296`), image downloads via a dedicated `ThreadPoolExecutor` (`storage.py`). No synchronous blocking call found on the event loop.
- **i18n propagation** — `settings.TMDB_LANGUAGE` (default `fr-FR`) threaded through every TMDB call: `tmdb_service.py:149,167,180,188,219`.
- **`commit_with_retry` discipline in workers** — sync/enrichment/health-check workers consistently route writes through `commit_with_retry` (`sync_worker.py:849,1002,1024,1116,1172,1238,1252,1267`; `enrichment_worker.py:193,227`; `health_check_worker.py:266,437,446`). Short-lived request-path commits use plain `db.commit()` — defensible given the `get_db` rollback wrapper, but see CR-C04 for the busy_timeout caveat.
- **Logging convention** — single `plexhub` logger tree, DEBUG→file / INFO→console, `request_id` injected by filter+middleware (`main.py:40-66`, `RequestIdMiddleware` added last so it wraps others, `main.py:366`). `SafeRotatingFileHandler` swallows Windows `PermissionError` (`main.py:30-35`).

---

## Debt findings (§10 — confirmed and rated)

### CR-D01 — No lint / formatter / type-checker / coverage tooling anywhere
- **Severity: dette (elevated — enables all other drift)**
- **Evidence:** `requirements-dev.txt` = `pytest`, `pytest-asyncio`, `respx` only. Grep across `requirements*.txt`, `pyproject.toml`, `.github/` for `ruff|black|mypy|flake8|isort` → **NONE**. No coverage tool (no `pytest-cov`/`coverage`); `pyproject.toml` has only `[tool.pytest.ini_options]`.
- **Impact:** No automated guardrail keeps conventions (camelCase, typing, unused imports, raw dicts) enforced. Every convention finding above is "manual discipline" with no safety net. Coverage is unmeasurable.
- **Suggested fix:** Add `ruff` (lint+format) and `pytest-cov` to `requirements-dev.txt` + a CI step; optionally `mypy` on `app/`. Wire a `ruff check` job into `tests.yml`.

### CR-D02 — `pydantic-settings>=2.1` declared but unused
- **Severity: dette**
- **Evidence:** `requirements.txt:7` declares `pydantic-settings>=2.1`; `app/config.py:21` is a hand-rolled `class Settings` reading `os.getenv` directly — `BaseSettings` is never imported anywhere in `app/`.
- **Impact:** Dependency bloat / misleading dependency surface; a reader assumes settings use pydantic-settings validation (they don't — e.g. `_safe_int` is a bespoke parser, no validation on URLs/keys).
- **Suggested fix:** Either drop the dependency, or migrate `Settings` to `pydantic_settings.BaseSettings` (gains typed validation, env-prefix, `.env` parsing for free).

### CR-D03 — Docker image Python 3.12 vs CI test runner Python 3.13
- **Severity: dette**
- **Evidence:** `Dockerfile:1` = `FROM python:3.12-slim`; `.github/workflows/tests.yml:20` = `python-version: "3.13"`. Tests never run on 3.12; the shipped artifact never runs on 3.13.
- **Impact:** Tests pass on a runtime that is never deployed, and the deployed runtime is never tested. Version-specific behavior (e.g. asyncio, typing, stdlib changes) can slip through.
- **Suggested fix:** Align — either test a 3.12 matrix entry, or move the image to `python:3.13-slim`. A matrix `[3.12, 3.13]` is cheapest insurance.

### CR-D04 — `X-API-Key` auth not applied to catalogue/admin/sync/plex routers
- **Severity: P1 (security-adjacent; cross-listed for the security dimension)**
- **Evidence:** `verify_api_key` is wired only as a router-level dependency on `/api/ai` (`api/ai.py:40`) and on `POST /api/tv-auth/approve` (`tv_auth.py:270`). `main.py:369-380` mounts `accounts`, `categories`, `live`, `media`, `stream`, `sync`, `plex`, and `admin` with **no auth dependency**. Concretely: `POST /api/accounts` (creates accounts, stores Xtream password), `DELETE /api/accounts/{id}` (cascades deletes of all media), `POST /api/sync/*` (triggers full pipeline), `POST /api/plex/generate` (writes to the filesystem) are all unauthenticated.
- **Impact:** Any client that can reach the backend can create/delete accounts, trigger heavy background jobs, and generate Plex libraries to disk. Combined with CR-D05 (CORS `*`) this is a real exposure if the backend is network-reachable.
- **Root cause:** Auth was scoped to the AI/pairing missions; catalogue routers predate the auth dependency and were never retrofitted.
- **Suggested fix:** Apply `verify_api_key` (or a non-vec variant like `verify_pairing_api_key`) as a router-level dependency on the mutating routers (`accounts` write ops, `sync`, `plex`, `categories` write ops, `admin`), or gate the whole app behind auth with an explicit public allowlist (`/api/health`, read-only catalogue if intended).

### CR-D05 — CORS default `*`
- **Severity: dette (P1 in prod)**
- **Evidence:** `config.py:55-57` defaults `CORS_ORIGINS` to `["*"]`; `main.py:358-363` configures `CORSMiddleware(allow_origins=settings.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])`.
- **Impact:** With wildcard origin + wildcard methods/headers, any web origin can call the (mostly unauthenticated, per CR-D04) API from a browser. Note: `allow_credentials` is not set (defaults False), which limits cookie-based attacks, but combined with no auth on catalogue routers this is still a meaningful surface.
- **Suggested fix:** Require an explicit `CORS_ORIGINS` in prod (fail-closed or warn loudly when `*`); document the production value in `.env.example`.

### CR-D06 — Test coverage unknown (no coverage tool) + a mis-marked test
- **Severity: dette**
- **Evidence:** No coverage tooling (CR-D01). 30 test files exist (`tests/`) covering AI, sync, tv-auth, plex, db, storage, etc. — breadth looks reasonable, but the actual coverage % is unmeasured. Additionally `tests/test_embedding_worker.py:73` `test_register_job_in_place_update` is a **sync** function decorated `@pytest.mark.asyncio` → pytest warns and the asyncio mark is a no-op (the test still runs as a plain function, so it's not silently skipped, but the mark is wrong).
- **Impact:** No quantified confidence in coverage; the mis-marked test signals copy-paste decorator drift.
- **Suggested fix:** Add `pytest-cov` and set a coverage floor in CI. Remove the spurious `@pytest.mark.asyncio` on the sync test.

### CR-D07 — Pre-existing flaky base64 test deselected in CI (and `filterwarnings` suppressing a real teardown warning)
- **Severity: dette**
- **Evidence:** `.github/workflows/tests.yml:33` deselects `tests/test_utilities.py::TestBase64Decode::test_text_that_looks_like_base64_but_decodes_to_garbage`. `pyproject.toml:12-15` `filterwarnings` ignores `PytestUnhandledThreadExceptionWarning` ("the pre-existing thread-pool teardown warning"). The base64 heuristic itself lives at `api/live.py:24-35` (`_try_base64_decode`).
- **Impact:** A known-broken test and a known thread-teardown warning are masked rather than fixed; technical debt accumulates and the masks can hide *new* failures of the same shape.
- **Suggested fix:** Fix the base64 heuristic test (it tests a genuinely ambiguous decode — tighten the control-char/printable-ratio gate in `_try_base64_decode`) and the thread-pool teardown (await/join the `_image_pool` on shutdown), then drop both masks.

### CR-D08 — Broad `except Exception` used pervasively in request paths swallowing/re-wrapping errors
- **Severity: dette**
- **Evidence:** ~50 `except Exception` sites. Two patterns of note in request paths: (a) `api/categories.py:38,70,153` and `api/accounts.py:63,183` catch `Exception` and re-raise as `HTTPException(500/400, detail=str(e))` — this can leak internal error text (e.g. DB/driver messages) to the client. (b) Migrations (`migrations.py`) catch-all is intentional/idempotent and fine. (c) `recommendation_service.py:47`, `mapping.py:78`, `storage.py:25` swallow exceptions silently.
- **Impact:** (a) Internal error strings surfaced to clients (information disclosure, minor). Broad catches also hide programming errors behind generic 500s. Inconsistent error-handling discipline vs the "HTTPException at boundaries" convention which implies *specific* exceptions are caught.
- **Root cause:** Defensive copy-paste `try/except Exception` around service calls.
- **Suggested fix:** In request handlers, catch the specific expected exceptions (e.g. `xtream` auth errors, `ValueError`) and return a generic client-facing message while logging the detail server-side; avoid `detail=str(e)` for unexpected exceptions.

---

## Additional undocumented observations (low severity)

- **`hashlib.md5` for account-id derivation** — `accounts.py:25-27` and `main.py:128-130` derive the 8-char account id via MD5 of `base_url+username`. Not a security boundary (just a deterministic id), but MD5 + 8-hex truncation (32-bit space) has a non-trivial collision probability across many accounts; `create_account` does guard with a 409-on-exists check, so a collision would surface as "account already exists" rather than data corruption. Worth a comment noting the intent.
- **Magic numbers are mostly named** — good: `nfo_import_service.py:58,77-79` (`_BUSY_TIMEOUT_MS`, `_LOCK_RETRY_*`), `tv_auth.py:55-58` (`_USER_CODE_*`, `_CLEANUP_GRACE_MS`), `db_retry.py:21` (`DEFAULT_DELAYS`). The decay/weight constants in `ai.py:323` (`max(0.1, 1.0 - 0.1*i)`) are inline but documented in the docstring.
- **No `TODO`/`FIXME`/`HACK` markers in `app/`** — only `ttXXXXXXX` placeholder text in an admin template (`templates/admin/_movie_row.html:20`), which is a UI hint, not debt.
- **`asyncio_default_fixture_loop_scope="function"`** is set (`pyproject.toml:7`) — good hygiene avoiding a known asyncio-fixture deprecation.

---

### Summary for the Manager

- **Dimension score: 68/100**
- **Findings by severity:** P1 = 3 (CR-C01, CR-D04, CR-D05*), P2 = 3 (CR-C02, CR-C03, CR-C04), dette = 8 (CR-C05, CR-C06, CR-D01, CR-D02, CR-D03, CR-D06, CR-D07, CR-D08). (*CR-D05 is P1-in-prod / dette-in-dev.)
- **Top 3:**
  1. **CR-C01 (P1)** — CI is red on `main`: `test_ai_status.py:113` asserts an obsolete model name; CI deselects the wrong test, so the pipeline fails on every push and the green-CI signal is dead.
  2. **CR-D04 (P1)** — Catalogue/sync/plex/admin routers are entirely unauthenticated (`X-API-Key` only on `/api/ai` + `tv-auth/approve`); `DELETE /api/accounts/{id}` and `POST /api/sync/*` are open.
  3. **CR-C02/C03 (P2)** — `sync.py` (8 endpoints), `categories.py`, `media.py` return raw untyped dicts, with snake_case keys leaking in `categories.py:145`, violating the "no raw dict / camelCase boundary" convention with no OpenAPI contract.
