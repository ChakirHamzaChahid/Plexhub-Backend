# 10 — Architecture (§2) — Clean-room audit 2026-07-11

**Verdict:** 3/5 — Solid layered bones (a genuine shared dedup core, clean DI, a real
service/worker/plex_generator split), undermined by legacy routers that embed business
logic + raw SQL, triplicated generation/pipeline orchestration, and two god-files.
**Summary:** The codebase has a coherent package topology (`api` → `services`/`workers` →
`db`/`models`) and the newer modules (`media_service`, `category_service`,
`api_key_service`, the whole `plex_generator` package) honour the stated rule that
routers only validate + delegate. But a cluster of older routers (`live`, `accounts`,
`categories`, `stream`, `tv_auth`) still carry ingestion logic, cascade deletes, and raw
DB queries in the request handler. The Plex-generation wiring and the end-to-end pipeline
are each copy-pasted across three entry points (one of which reaches into a private symbol
of `app.main`), and `sync_worker.py` / `ai.py` have grown into multi-responsibility
god-files. None of these actively corrupts data on the happy path today, so there is no
P0 — but the duplication is a live divergence hazard.

## Findings

### CR-A01 — Business logic and raw DB access embedded in routers (violates §2 layering rule)  (P1)
- **Where:**
  - `app/api/live.py:149-246` — `get_channel_epg` fetches from Xtream, base64-decodes titles, constructs `EpgEntry` ORM rows, `db.add()` + `db.commit()`. This is a full write-through ingest path living in the router.
  - `app/api/live.py:41-89` and `:249-275` — channel/EPG listing build `select`/`func.count`/sort/pagination inline (no `live_service`).
  - `app/api/accounts.py:124-155` — `delete_account` performs a manual cascade delete across 6 tables (`Media`, `EnrichmentQueue`, `XtreamCategory`, `LiveChannel`, `EpgEntry`, `XtreamAccount`) with raw `delete()` statements in the handler; `:36-94` `create_account` authenticates, builds the entity and commits inline.
  - `app/api/categories.py:75-155` — `refresh_categories` fetches VOD+series categories from Xtream and runs the `upsert_category` loop + commit in the router (the reusable service functions `get_categories`/`bulk_update_categories` exist next door, but this path bypasses that pattern).
  - `app/api/stream.py:20-32`, `app/api/live.py:118-121,178-181` — `server_id` → `account_id` parsing (`"xtream_"` prefix stripping) is re-implemented inline in ≥3 handlers instead of using `utils/server_id.parse_server_id`.
- **What:** The project rule (CLAUDE.md §2 closing note, and the `deps.py`/`admin.py` docstrings that boast "business logic isn't duplicated") is only partially observed. `media.py`, `admin.py`, `health.py` delegate cleanly to `media_service`; the routers above do not.
- **Impact:** The EPG-ingest and account-cascade logic cannot be unit-tested, reused by workers, or called from the CLI; identical `server_id` parsing drifts per-handler; a schema change (new child table on account delete) must be remembered in the router rather than one service.
- **Fix direction:** Extract `live_service`/`epg_service`, an `account_service.delete_account_cascade()`, and route `refresh_categories` through `category_service`. Replace inline prefix stripping with `parse_server_id`.

**Statut : PARTIELLEMENT RÉSOLU (2026-07-12, cleanroom-fixer).** Extraction pure (comportement inchangé — mêmes réponses/codes/side-effects) de `app/api/live.py`, `app/api/accounts.py`, `app/api/categories.py` vers de nouveaux modules service :
- `app/services/live_service.py::ingest_short_epg` (nouveau) — le bloc d'ingestion EPG (fetch Xtream + décodage base64 + parsing timestamps + construction `EpgEntry`) déménagé hors de `get_channel_epg`. Le routeur ne fait plus que la lecture-cache DB + délégation + `commit_with_retry` (CR-C04 inchangé). `_try_base64_decode` déménagé avec (re-exporté pour `tests/test_utilities.py`, import mis à jour).
- `app/services/account_service.py` (nouveau) — `create_account` (auth Xtream + construction entité), `update_account`, `delete_account_cascade` (delete 6 tables), `test_account_connection` extraits de `accounts.py`. Le routeur garde le `commit_with_retry` (CR-C04) et traduit les exceptions du service (`AccountAlreadyExistsError`/`AccountAuthenticationError`/`AccountNotFoundError`) en les mêmes `HTTPException` (codes + messages identiques).
- `app/services/category_service.py::refresh_categories_from_provider` (nouveau) — la boucle fetch VOD/series + upsert déménagée hors de `refresh_categories`.
- `server_id` parsing dédupliqué dans `live.py` (`get_channel_stream`, `get_channel_epg`) via `utils/server_id.parse_server_id` au lieu du `startswith("xtream_")` + `[7:]` inline.
- **CR-C10 (moitié `accounts.py`) résolue au passage** : la classe jetable `TempAccount` remplacée par le dataclass partagé `app.services.xtream_credentials.XtreamCredentials`.

**Résiduel (hors périmètre de cette passe)** : `live.py::list_channels`/`get_epg_now` (query/sort/pagination) restent inline — simples lectures paramétrées, pas de la logique métier, jugées faible priorité pour ce contained fix ; `app/api/stream.py:20-32` (server_id dupliqué) non touché — fichier hors scope de cette session ; `main.py`'s `_Acc` (auto-provision Xtream) reste une classe jetable distincte — reste du CR-C10, `main.py` hors scope de cette session.

Preuve : `pytest tests/test_accounts_retry.py tests/test_categories_refresh_camelcase.py tests/test_live_channels_query.py tests/test_utilities.py tests/test_auth_guard.py tests/test_account_service.py tests/test_live_service.py tests/test_category_service_refresh.py tests/test_db_layer.py` → **78 passed** (comportement inchangé + 18 nouveaux tests unitaires sur les fonctions de service extraites, `tests/test_account_service.py`/`test_live_service.py`/`test_category_service_refresh.py`). App bootable : `import app.main` + `GET /api/health` → 200 via ASGI transport (boot `uvicorn` réel non exécutable nativement sur cette machine Windows — piège §9.7 fcntl POSIX, limitation préexistante non liée à ce changement). Aucune migration touchée (pas de changement de schéma).

### CR-A02 — Plex-generation wiring and the full pipeline are each triplicated; a router reaches into `app.main` internals  (P1)
- **Where:**
  - **Generation wiring** (`storage` + `DatabaseSource` + `PlexLibraryGenerator` + `generate()`) is independently reconstructed in three places: `app/main.py:108-112`, `app/api/plex.py:56-59`, `app/cli.py:41-47`.
  - **Pipeline orchestration** (sync → enrichment → validation → Plex generation) is re-sequenced three times: `app/main.py:249-260` (`scheduled_sync_enrich_generate`), `app/main.py:328-335` (`initial_sync_then_enrich`), and `app/api/sync.py:67-79` (`_full_pipeline`).
  - `app/api/sync.py:74` does `from app.main import _auto_generate_plex_library` — a router importing a private coroutine from the application entrypoint (a layering inversion and a latent circular-import trap: `app.main` imports every router at module load, `sync.py` lazily imports back into `app.main`).
- **What:** Four subtly different copies of "how to generate a library" and three copies of "how to run the pipeline". The copies already differ: `plex.py`/`cli.py` thread `strm_only` and `DryRunStorage`; `main.py`'s copy hardcodes `LocalStorage` + full options and swallows exceptions locally; the pipeline copies differ in logging and error wrapping.
- **Impact:** Any change to generation behaviour (pruning, dry-run semantics, account scoping) or pipeline ordering must be mirrored in 3–4 sites or they silently diverge — exactly the class of bug that produces "works from the CLI, not from the scheduler". The `sync.py`→`app.main` reach-in couples the HTTP layer to the entrypoint.
- **Fix direction:** Introduce `services/plex_generation_service.generate_library(account_ids, output, *, strm_only, dry_run)` and `services/pipeline_service.run_full_pipeline()`; have `main.py`, `plex.py`, `cli.py`, and `sync.py` all call these. Remove the `app.main` import from `sync.py`.

### CR-A03 — God-files / god-function: `sync_worker.py`, `ai.py`, `nfo_import_service.py`  (P1)
- **Where:**
  - `app/workers/sync_worker.py` — 1390 LOC. `sync_account` spans `:920-1366` (~446 lines in one function) and mixes: category-config loading, Xtream fetch, VOD/series/episode/live DTO mapping, batch upserts, five `differential_cleanup*` variants, EPG persistence, and adult-flag recompute. The module also owns four `map_*_to_media` mappers, three hashers, and the in-memory job tracker.
  - `app/api/ai.py` — 1228 LOC. One router file carries ~20 Pydantic schemas plus 13 endpoints across five unrelated concerns: vector ranking (`/rank`, `/rank-multi`), semantic search + RAG assistant (`/search`, `/assistant`), embedding admin (`/embed/*`), LLM chat/describe (`/describe`, `/chat`, `/llm/status`), subtitle translation (`/subtitles/translate`), and blurbs (`/blurb`).
  - `app/services/nfo_import_service.py` — 888 LOC mixing XML parsing (~15 `_extract_*`/`_validate_*` helpers), fill-missing diffing, and per-account movie/show import orchestration with its own lock-retry helpers.
- **What:** These files bundle many responsibilities behind a single import surface; cohesion is low and change-blast-radius is high.
- **Impact:** Hard to review, test in isolation, or reason about locking/transaction scope; the 446-line `sync_account` in particular is a maintainability cliff. A regression in subtitle translation forces re-review of the entire AI surface because it shares a file with vector ranking.
- **Fix direction:** Split `ai.py` into an `api/ai/` package of sub-routers (`reco`, `search`, `llm`, `subtitles`, `blurb`) sharing schemas. Decompose `sync_account` into explicit phase functions (`_sync_vod`, `_sync_series`, `_sync_live`, `_sync_epg`, `_finalize`). Separate NFO parsing from import orchestration.

### CR-A04 — Three inconsistent router-mounting patterns; auth relies on per-router discipline, not central enforcement  (P2)
- **Where:** `app/main.py:398-438`.
  - Pattern A (most routers): `include_router(x.router, prefix="/api", dependencies=_guard)` — `accounts`, `categories`, `live`, `media`, `stream`, `sync`, `plex`.
  - Pattern B (public): `prefix="/api"`, no guard — `health` (`:398`), `tv_auth` (`:408`, self-guards only `/approve`).
  - Pattern C (bare mount, self-prefixed): `ai` (`:434`) and `api_keys` (`:438`) declare their own full `/api/...` prefix and carry a module-level `dependencies=[Depends(...)]` inside the router, mounted with no `_guard`.
- **What:** Whether an endpoint is authenticated depends on which of three mounting conventions the author picked, with the guard sometimes at the mount site and sometimes inside the router module.
- **Impact:** There is no single place that guarantees "every `/api/*` route is authenticated". A future router added in Pattern C style that forgets its module-level dependency ships publicly, and code review must catch it because nothing structural will. (Today all three self-guarding routers *are* guarded — this is a latent risk, not a present hole.)
- **Fix direction:** Standardise on one convention (prefix + explicit `dependencies` at the mount site), or add a startup assertion / test that walks `app.routes` and fails if any `/api/*` route lacks an auth dependency.

### CR-A05 — `main.py` concentrates too many responsibilities; entrypoint owns business coroutines  (P2)
- **Where:** `app/main.py` (442 LOC). At import time it configures logging (`:38-74`); it then defines three business coroutines — `_auto_generate_plex_library` (`:77-119`), `_auto_provision_xtream_account` (`:122-177`), `_cleanup_stale_epg` (`:180-192`) — plus the lifespan with master election, five scheduler job definitions, two pipeline coroutines, middleware wiring, router mounting, and the Basic-Auth docs re-exposition (`:423-430`).
- **What:** The application factory doubles as a home for domain logic. `_auto_generate_plex_library` is a workflow that CR-A02 shows is imported back into a router; `_auto_provision_xtream_account` and `_cleanup_stale_epg` are worker-grade tasks living in the entrypoint.
- **Impact:** Lifespan/scheduler concerns can't be tested without importing the whole app (and its import-time logging + fcntl usage); domain coroutines are stranded in a module other layers must reach into.
- **Fix direction:** Move the three coroutines to `workers/`/`services/`; extract scheduler registration into `app/scheduler.py`; keep `main.py` to factory + lifespan glue.

### CR-A06 — Cross-module reach into private symbols; process-local mutable job stores  (P2)
- **Where:**
  - `app/workers/embedding_worker.py:18` — `from app.services.recommendation_service import _serialize_vec` (importing an underscore-private helper across the service boundary).
  - `app/scripts/strip_titles_pollution.py:25` — `from app.plex_generator.naming import _movie_folder, _series_folder` (private naming internals).
  - In-memory job stores: `sync_worker._sync_jobs` (`sync_worker.py:31`, capped 100) and `embedding_worker._ai_jobs` (`embedding_worker.py:25`, capped 100) are module-global dicts.
- **What:** Two modules depend on the private surface of another (any refactor of `_serialize_vec`/`_movie_folder` silently breaks a distant caller). Job status lives in per-process memory.
- **Impact:** Private-symbol coupling defeats the encapsulation the underscore signals. The job stores are only visible to the process that created the task — under the master/slave multi-worker model implied by the `fcntl.flock` election (`main.py:225-231`), a `POST /api/sync/xtream` handled by worker A registers a job invisible to `GET /api/sync/status` served by worker B, which returns `"unknown"`. State is also lost on restart. This is fundamentally a shared-state/architecture concern, not just reliability.
- **Fix direction:** Promote `_serialize_vec` and the naming folder helpers to public API (drop the underscore or re-export). If multi-worker is a real deployment target, back job status with the DB (a `jobs` table) instead of a module dict.

### CR-A07 — Duplicated `_build_versions` helper between the API and the generator  (debt)
- **Where:** `app/api/media.py:27-45` and `app/plex_generator/source.py:58-80` each re-implement the same "sort members by stable identity → `version_label` → `dedup_labels`" sequence, with in-code comments in both explicitly stating they must stay byte-identical to each other.
- **What:** The determinism-critical labelling wrapper is copy-pasted; only the underlying primitives (`version_label`, `dedup_labels`) are shared via `aggregation_service`.
- **Impact:** The two copies can drift, which would break the very cross-surface label/`.strm`-filename determinism they promise (API card vs on-disk library disagreeing on a version's `#n` suffix).
- **Fix direction:** Hoist a single `aggregation_service.build_versions(members, label_for)` and have both callers use it.

## What's healthy (brief, for balance)
- **`aggregation_service` is a real single source of dedup**, as claimed: it is consumed by both `media_service` (`get_unified_list`/`get_unified_group`/`get_unified_episodes`, `media_service.py:8-9,144,176,207`) and the generator (`plex_generator/source.py:14-17,100,161`). The REST API and the on-disk library genuinely share one grouping pass.
- **Clean DI and DB access discipline** where it counts: `Depends(get_db)` everywhere, sessions via `async_session_factory`, async end-to-end.
- **Auth is centralised and cohesive** in `deps.py` (fail-closed `verify_backend_secret`/`verify_api_key`/`verify_master_key` + Basic-Auth), and `admin.py` deliberately reuses `media_service` rather than duplicating catalogue logic (`admin.py:1-6,22`).
- **`plex_generator` is well-layered**: `MediaSource` ABC + `DatabaseSource`, a `LocalStorage`/`DryRunStorage` abstraction, and a `generator` that orchestrates — a textbook ports/adapters split.
- **Package boundaries are clear** (`api`/`services`/`workers`/`plex_generator`/`db`/`models`/`utils`) with no import cycles other than the `sync.py`→`app.main` reach-in flagged in CR-A02.
