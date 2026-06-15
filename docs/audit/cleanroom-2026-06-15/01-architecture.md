# CR — Architecture & Module Boundaries (§2)

**Dimension score: 72 / 100** — Solid layered skeleton (clean `db`/`models`/`plex_generator` abstractions, disciplined async DB-session conventions, well-managed httpx/threadpool/task lifecycle), dragged down by inconsistent service-layer enforcement (several routers carry business logic + raw DB access), triplicated Plex-generation orchestration, leaky private-state access into the AI subsystem, and a master/slave election that doesn't gate the mutating HTTP endpoints it was built to protect.

**Mental model (verified from code):** A single FastAPI app (`app/main.py`) composes ~11 routers under `/api` (+ `/admin` HTML, + `/api/ai`). On boot, `lifespan` runs `init_db()` (WAL PRAGMAs + sqlite-vec extension load + migrations), then elects a single "master" process via `fcntl.flock` on `DATA_DIR/server_start.lock`; only the master starts the APScheduler pipeline (sync→enrich→validate→generate) and crons. Request handlers get a per-request `AsyncSession` via the `get_db` dependency; background workers and the plex_generator open their own sessions via `async_session_factory`. Heavy work (sync, enrichment, validation, embeddings, Plex generation) lives in `workers/` (module-function style with in-memory job dicts) and `plex_generator/` (the only place with proper OO abstraction: `MediaSource`/`DatabaseSource`, `Storage` hierarchy). Services are a mix of class-singletons and bare functions. fastembed/sqlite-vec form an isolated AI sub-stack reached only through `/api/ai` (the one auth-gated router).

---

## Findings

### CR-A01 — `fcntl.flock` master election does not gate the mutating endpoints it exists to coordinate (P1)
- **Evidence:** Election sets `is_master` and starts the scheduler only on master (`app/main.py:224-235,241-320`). But `POST /api/sync/xtream`, `/xtream/all`, `/enrichment`, `/validate-streams`, `/full-pipeline` (`app/api/sync.py:11-79`), `POST /api/plex/generate` (`app/api/plex.py:34`), and `POST /api/accounts` (which fires `sync_account` in background, `app/api/accounts.py:91-92`) run on **any** worker with **no master check and no auth** (verified: no `is_master`/`Depends` guard in those routers).
- **Impact:** In the documented multi-worker Docker target (the only reason flock election exists), a sync/generate/account request landing on a *slave* spawns the exact heavy pipeline the slave was elected out of — duplicating the master's work, double-writing the same SQLite DB, and contending on locks. The election protects the *scheduled* path but leaves the *on-demand* path uncoordinated.
- **Root cause:** Master-ness is a lifespan-local boolean, never exposed to the request layer; concurrency control is per-`asyncio.Lock` *within a process* (`sync_worker._account_locks`, `app/workers/sync_worker.py:34`), which gives no cross-process guarantee.
- **Suggested fix:** Expose master status (e.g. `app.state.is_master`) and have mutating workers route through a single owner — either reject/redirect on slaves, or replace per-process `asyncio.Lock` with a DB-row advisory lock so any worker serializes against the master.

### CR-A02 — Plex-generation orchestration triplicated across composition root, router, and CLI (P1 / dette)
- **Evidence:** The "select active accounts → loop `DatabaseSource`+`PlexLibraryGenerator`+`LocalStorage`/`DryRunStorage` → aggregate report" sequence is implemented three times, near-identically: `app/main.py:93-117` (`_auto_generate_plex_library`), `app/api/plex.py:56-94` (`generate_plex_library`), `app/cli.py:43-68` (`_run_generate`). `app/api/sync.py:74` even re-imports the private `_auto_generate_plex_library` from `app.main` to get a fourth entry point.
- **Impact:** Any change to generation policy (filtering, storage choice, per-account output layout) must be made in 3-4 places; they already diverge (only `plex.py`/`cli.py` honor `strm_only`/`dry_run`; `main.py` hard-codes `LocalStorage`). `sync.py` importing from `main.py` creates a router→composition-root dependency edge (latent circular-import risk).
- **Root cause:** No `PlexGenerationService` (or `generate_for_accounts(...)` use-case) owning the orchestration; each caller wires the pieces by hand.
- **Suggested fix:** Extract one `services/plex_generation_service.generate(account_id|all, output, strm_only, dry_run) -> AggregateReport`; have main, the router, and the CLI all call it. Remove the `sync.py`→`main.py` import.

### CR-A03 — Business logic + raw DB access in routers (service layer bypassed) (P1 / dette)
- **Evidence:**
  - `app/api/live.py:149-246` — `get_channel_epg` does query construction, Xtream fetch orchestration, base64/timestamp parsing of EPG listings (`_try_base64_decode`, `app/api/live.py:24-35`), `EpgEntry` construction, and `db.commit()` — all inline; no `live_service` exists.
  - `app/api/accounts.py:36-185` — account creation (auth probe + entity build), cascade DELETE across 6 tables (`app/api/accounts.py:135-155`), and update all use `db.execute(select/delete/update)` directly; `_generate_account_id` (`accounts.py:25-27`) is itself duplicated in `app/main.py:128-130`.
  - `app/api/categories.py:75-155` — `refresh_categories` carries the fetch+upsert+commit loop inline.
  - `app/api/tv_auth.py:177-388` — the entire device-flow state machine (expire/approve/status/complete, crypto calls, commits) lives in the router; no `tv_auth_service`.
- **Impact:** §2's stated rule ("logique métier dans services/workers, api = validation + délégation") is violated in 4 routers. This logic is untestable without the HTTP layer, can't be reused (e.g. CLI/worker), and the cascade-delete in `accounts.py` is the kind of multi-table invariant that belongs behind a service boundary.
- **Root cause:** Service layer was applied selectively (`media`/`stream`/`categories.list` delegate cleanly; `live`/`accounts`/`tv_auth` grew in-router).
- **Suggested fix:** Introduce `live_service`, `account_service`, `tv_auth_service` (or move the logic into existing services); routers keep only validation + delegation + HTTP mapping. Dedupe `_generate_account_id` into one shared function.

### CR-A04 — Routers reach into private module state of the AI/db subsystems (P2)
- **Evidence:** `app/api/ai.py:417-421` reads `embedding_service._model`, calls `embedding_service._resolve_model_name()` (both underscore-private), and reads `app.db.database._VEC_LOADED`; `app/api/deps.py:19,37` also reads `_VEC_LOADED`. `embed_status` additionally runs four raw `text("SELECT COUNT(*) ...")` queries inline (`app/api/ai.py:395-406`).
- **Impact:** The "private" markers are fiction — model-load state and vec-load state are de-facto public contract consumed by the auth dependency and a diagnostics endpoint. Any refactor of `embedding_service`/`database` internals silently breaks auth (503 path) and `/embed/status`. Raw SQL in the router bypasses the model/service layer.
- **Root cause:** No public status accessor (e.g. `embedding_service.status()` / `database.vec_status()`); diagnostics counts have no `ai_status_service`.
- **Suggested fix:** Add public `embedding_service.model_loaded()`/`model_name()` and `database.vec_status()`; move the COUNT queries into a small AI-status service. `deps.py` consumes the public accessor.

### CR-A05 — `sync_worker` is a 1314-line god-module mixing mapping, hashing, DB I/O, orchestration, and in-memory job state (P2 / dette)
- **Evidence:** `app/workers/sync_worker.py` (1314 lines) holds DTO→row mappers (`map_vod_to_media` etc., `:86-355`), hash helpers (`:383-527`), batch upserts + 4 differential-cleanup variants (`:393-665`), category-config logic (`:666-852`), the 400-line `sync_account` (`:854-1289`), `run_all_accounts`, and an in-memory job tracker (`_sync_jobs`/`_record_sync_job`, `:30-52`). `nfo_import_service.py` (693) and `strip_titles_pollution.py` (552) are similarly oversized.
- **Impact:** Single file far exceeds the 200-LOC guidance; the mapping/hashing pure functions can't be unit-tested or reused without importing the whole worker; `sync_account` is one giant function. High change-risk surface.
- **Root cause:** Organic growth without splitting; mappers/hashers never extracted to a `sync/mapping.py` + `sync/hashing.py`.
- **Suggested fix:** Split into a `workers/sync/` package: pure mappers, pure hashers, persistence helpers, and the orchestration coroutine — each independently testable.

### CR-A06 — Inconsistent service paradigm: class-singletons vs bare module functions (P2 / dette)
- **Evidence:** Class-instance singletons: `media_service = MediaService()` (`media_service.py:231`), `tmdb_service = TMDBService()` (`tmdb_service.py:329`), `xtream_service = XtreamService()` (`xtream_service.py:264`). Bare module-function services: `category_service` (`get_categories`/`bulk_update_categories`/`upsert_category`), `stream_service` (`build_stream_url`/`parse_rating_key`), `recommendation_service`, `embedding_service`, `nfo_import_service`. Workers are all module-function style with module-global mutable state (`sync_worker._sync_jobs`, `embedding_worker` jobs, `health_check_worker._client`).
- **Impact:** No DI seam anywhere — every consumer imports the concrete singleton/function directly, so swapping or mocking requires monkeypatching module globals. Two stylistic camps make the codebase harder to navigate and the "what's a service" rule ambiguous.
- **Root cause:** No agreed convention; both patterns coexist.
- **Suggested fix:** Pick one (module-function services read cleanest here and match the stateless ones). Where shared state is needed (httpx clients), keep the lazy-singleton accessor but standardize the shape. Document the chosen convention in §3.

### CR-A07 — Shared `now_ms()` utility bypassed; timestamp logic re-implemented (P2 / dette)
- **Evidence:** `app/utils/time.py` provides `now_ms()` (used in `accounts.py:84`, `strip_titles_pollution.py`). But `tv_auth.py:150-151` defines its own `_now_ms`, `live.py:161,257` inlines `int(time.time()*1000)` (also at `:196`), and `main.py:171,184` inlines it twice.
- **Impact:** Minor, but it is exactly the "util exists, three modules ignore it" smell; any future change to time semantics (monotonic, injected clock for tests) has 5 call sites to find.
- **Root cause:** Util added after the inlined versions; never back-ported.
- **Suggested fix:** Replace all inline `int(time.time()*1000)` and `tv_auth._now_ms` with `from app.utils.time import now_ms`.

### CR-A08 — `pydantic-settings` declared but a hand-rolled `Settings` class is used; config side-effects at import (P2 / dette)
- **Evidence:** `app/config.py:21-81` is a plain class reading `os.getenv` with a custom `_safe_int`; `pydantic-settings` is in `requirements.txt` but unused here. `Settings.__init__` performs filesystem side-effects (`mkdir` on DATA_DIR/LOG_DIR, `config.py:71-72`) at module-import time (`settings = Settings()`, `:81`).
- **Impact:** Re-implements typed env parsing/validation that pydantic-settings does for free (NIH); import-time `mkdir` couples config import to a writable filesystem (already bites tests/CLI that import `config` transitively). No validation of required secrets, no typed coercion beyond ints/bools.
- **Root cause:** Custom settings predate (or ignore) the declared dependency.
- **Suggested fix:** Migrate to `pydantic_settings.BaseSettings` (gets typing, validation, `.env` loading); move directory creation out of import into `init_db`/lifespan startup.

### CR-A09 — Heavy reliance on in-function imports (58 occurrences) including the lifespan hot path (P2 / dette)
- **Evidence:** 58 `from app...`/`import app...` statements inside function bodies (grep over `app/`), with `app/main.py` alone holding 18 (e.g. `import fcntl` at `main.py:196`, scheduler/worker imports at `:244-245,292,319,328,339-341,348`). `sync.py` uses local imports for every endpoint; `accounts.py:91,135`, `categories.py:89-91`, `plex.py:52-61`, `ai.py:391-393` likewise.
- **Impact:** Some are deliberate and correct (`import fcntl` deferred so non-POSIX boot fails late, not at import; lazy fastembed). But the bulk are used as an ad-hoc circular-import workaround (`sync.py`→`main.py`, routers→workers), which hides the real dependency graph and makes cycles invisible to tooling. It also masks CR-A02's main↔sync coupling.
- **Root cause:** No clear layering contract, so cycles are dodged per-call-site rather than designed away.
- **Suggested fix:** Keep the intentional platform/lazy-load deferrals (`fcntl`, fastembed, sqlite-vec); hoist the rest to module top once the service-extraction (CR-A02/A03) removes the underlying cycles.

### CR-A10 — Duplicated auth dependency and broad error-leaking handlers (P2 / dette)
- **Evidence:** `verify_pairing_api_key` (`tv_auth.py:72-87`) duplicates `verify_api_key` (`deps.py:22-49`) minus the sqlite-vec check — same constant-time compare, same 503/401 shape, copy-pasted (comment at `tv_auth.py:68` admits it). `categories.py:38-40,70-72,153-155` use `except Exception ... raise HTTPException(500, detail=str(e))`, echoing internal exception text to clients.
- **Impact:** Two auth implementations to keep in sync (a security-relevant primitive); generic `detail=str(e)` is an information-disclosure smell and an inconsistent error contract vs the rest of the API.
- **Root cause:** Auth helper not parameterized (e.g. `verify_api_key(require_vec: bool)`); category endpoints wrap everything defensively.
- **Suggested fix:** Parameterize one `verify_api_key` factory (`Depends(verify_api_key(require_vec=False))` for pairing). Drop `detail=str(e)`; log the exception, return a generic message.

### CR-A11 — `health.py` hard-codes `version="1.0.0"` instead of `APP_VERSION` (dette)
- **Evidence:** `app/api/health.py:31` returns `version="1.0.0"` as a literal; the single source of truth is `APP_VERSION = "1.0.0"` in `app/main.py:17`.
- **Impact:** Version reported by `/api/health` will silently drift from the app version on the next bump (two places to edit).
- **Root cause:** Constant not centralized (importing from `main` would create a cycle — itself a symptom of `APP_VERSION` living in the composition root rather than in `config`/a `version.py`).
- **Suggested fix:** Move `APP_VERSION` to `app/config.py` (or `app/__init__.py`); both `main` and `health` import it.

---

## What's solid (strengths)

- **DB-access discipline is consistent and correct.** Request handlers use the `get_db` dependency (commit/rollback wrapper, `db/database.py:57-65`); workers, the plex_generator, and scripts open their own sessions via `async_session_factory` (verified across all of `workers/*` and `scripts/*`) — exactly right for non-request-scoped code. No stray `sqlite3.connect` except the legitimate online `.backup()` in `scripts/backup_db.py:34`.
- **`plex_generator/` is a model of clean architecture:** `MediaSource` ABC + `DatabaseSource` (`source.py:16-159`), `Storage`/`LocalStorage`/`DryRunStorage` hierarchy, atomic writes (`storage.py:11-31`), and streaming reads with `yield_per=1000` for the 100k-row table. This is the abstraction quality the rest of the app should aim for.
- **Resource lifecycle is carefully handled.** httpx clients are lazy double-checked-lock singletons each with `close()` (`xtream_service`, `tmdb_service`, `health_check_worker:22-41`); thread-local image clients tracked for shutdown (`storage.py:38-60`); all of them plus the image threadpool and background tasks are torn down in the lifespan `finally` (`main.py:326-349`) — and the shutdown ordering (cancel tasks → release flock → stop scheduler → close clients → stop pool) is sensible.
- **Background-task hygiene is real:** `utils/tasks.py` keeps strong refs (prevents GC cancellation), logs failures via done-callback, supports cancel-by-name, and drains with a timeout on shutdown.
- **Scheduler config is correct for the SQLite single-writer reality:** `max_instances=1`, `coalesce=True`, `misfire_grace_time`, blocking `sqlite3.backup` wrapped in `asyncio.to_thread` (`main.py:296`), serial pipeline.
- **Middleware ordering is intentional and documented** (`main.py:358-366`): RequestId added last so it wraps GZip/CORS and sets `request_id` before anything else logs.
- **AI sub-stack is well-isolated:** the only auth-gated router (`/api/ai`, module-level `Depends(verify_api_key)`), with graceful 503 degradation when fastembed/sqlite-vec are unavailable — the heavy ML dependency does not leak into the catalogue/sync paths.
