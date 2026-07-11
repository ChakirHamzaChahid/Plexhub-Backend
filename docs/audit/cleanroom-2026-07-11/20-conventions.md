# Clean-room audit — Conventions / Code-quality dimension

**Scope:** conventions & code-quality only (Pydantic-at-frontier / camelCase contract, blocking-call
discipline, error-handling patterns, migration hygiene, logging/secrets, lint tooling, dead code).
Judged only from current code at HEAD; no prior audit consulted. Every finding cites `file:line` read
this session.

**Verdict:** 3/5

The JSON API's core discipline is genuinely good: Pydantic v2 models with `to_camel` aliases and
`populate_by_name` are applied consistently across `schemas.py`, the `/api/ai` router is an exemplary,
uniformly-typed surface with a coherent 503 taxonomy, auth dependencies use constant-time compares, and
migrations are idempotent (`IF NOT EXISTS` / guarded `ADD COLUMN`). Secrets are not logged in clear (TMDB
key is truncated; the tv-auth payload and API-key plaintext are never logged). But several systematic
convention gaps drag the score down: a real **snake_case leak on the wire**, a broad **raw-dict / no-`response_model`**
pattern in `sync.py`/`media.py`/`categories.py`, **blocking fsync file I/O run directly on the event loop**
during library generation, **schema truth duplicated between the ORM model and migrations** (noisy
duplicate-column warnings on every fresh DB), **write-retry discipline applied to workers but not to
request-path writes**, and **no linter/formatter wired at all**.

---

### CR-C01 — Blocking fsync file I/O executed directly on the event loop (P1)

**Where:** `app/plex_generator/generator.py:192` (`async def generate`) calls, synchronously and un-offloaded:
`storage.write_strm` (`app/plex_generator/storage.py:108-110`), `storage.write_file`
(`storage.py:112-116`), `storage.read_strm` (`storage.py:150-154`), `storage.delete_file`
(`storage.py:145-148`), `self.mapping.save()` (`app/plex_generator/mapping.py:59-77`) and
`storage.prune_orphan_dirs` (`storage.py:170-196`). Each write goes through `_atomic_write_bytes`
(`storage.py:16-32`) which does `f.flush()` + **`os.fsync(f.fileno())`**; `mapping.save` also fsyncs;
`prune_orphan_dirs` does `rglob("*")` + `shutil.rmtree`. Entry points that `await gen.generate()` on the
main loop: `app/main.py:112` (`_auto_generate_plex_library`, boot + every `SYNC_INTERVAL_HOURS` pipeline),
`app/api/plex.py:59`, and `app/api/sync.py:75`.

**What:** House rule (c) — "tout appel bloquant passe par `asyncio.to_thread` ; ne jamais bloquer la boucle
d'événements". Only image downloads are offloaded (`submit_image_download` → `_image_pool`,
`storage.py:137-143`). The `.strm`/`.nfo` writes, mapping serialization and orphan-dir scan are **not**. For
a catalogue of thousands of movies/episodes this is tens of thousands of fsync syscalls plus a full-tree
walk, all on the single event loop of the master worker.

**Impact:** During auto-generation (boot and each scheduled pipeline) the event loop is starved: concurrent
HTTP requests (including `/api/health` used for monitoring) stall for the duration of the write phase.
Contradicts the stated async I/O convention that the codebase otherwise honours (embedding inference,
sqlite backup).

**Fix direction:** Wrap the disk-facing phase of `generate()` in `asyncio.to_thread`, or make the storage
methods coroutines that offload their fsync writes to a thread pool (the image path already shows the
pattern). At minimum offload `mapping.save()` and the per-title write loop.

---

### CR-C02 — snake_case field names leak onto the wire (P1)

**Where:** `app/api/categories.py:145-150` — `refresh_categories` returns a raw dict
`{"message": …, "vod_count": …, "series_count": …, "total": …}`.

**What:** The entire public API is camelCase (every model in `schemas.py` sets
`alias_generator=to_camel`, and responses use `response_model_by_alias=True`). This endpoint bypasses the
model layer and emits **`vod_count` / `series_count`** — snake_case — directly to the client. A camelCase
consumer (the Android app, per the house contract "alias camelCase … jamais de dict brut en réponse
publique", §3) cannot read these two fields under the expected key.

**Impact:** Contract inconsistency on a client-facing endpoint: the two counts are silently unreadable by a
strict camelCase client. Low traffic, but it is a genuine wire-format break, not just a style nit.

**Fix direction:** Return a typed `CategoriesRefreshResponse(BaseModel, alias_generator=to_camel)` with
fields `vodCount`/`seriesCount`/`total`/`message`, matching the rest of the surface.

---

### CR-C03 — Raw untyped dicts returned from public JSON endpoints (no `response_model`) (P2)

**Where:**
- `app/api/sync.py` — 7 of 8 endpoints return bare dicts and declare no `response_model`:
  `trigger_sync:20`, `trigger_sync_all:30`, `cancel_sync:39`, `trigger_enrichment:48`,
  `trigger_stream_validation:57`, `trigger_full_pipeline:79`, `list_sync_jobs:96`.
- `app/api/media.py:353` — `rescrape_media` returns `{"status": "queued"}`.
- `app/api/categories.py:67` (`update_categories`) and `:145` (`refresh_categories`, see CR-C02).
- Untyped inner shapes in otherwise-typed models: `SyncStatusResponse.progress: Optional[dict]`
  (`schemas.py:327`) and `CategoryUpdateRequest.categories: list[dict]` (`schemas.py:361`).

**What:** Violates §3 "jamais de dict brut en réponse publique". These bodies keep camelCase keys manually
(`jobId`, `message`) so the wire happens to be acceptable, but there is no Pydantic validation and — more
importantly — **these responses are absent from the OpenAPI schema** (they appear as untyped `200`), so the
generated client / Swagger contract is incomplete.

**Impact:** No compile-time or schema-time guarantee on these payloads; drift risk (a future edit could
reintroduce a snake_case key à la CR-C02 unnoticed); weaker generated-client typing.

**Fix direction:** Introduce small typed response models (`JobAcceptedResponse{jobId}`,
`JobListResponse{jobs}`, `MessageResponse{message}`, `RescrapeResponse{status}`) and attach
`response_model=`. Type the `dict` inner fields (`progress`, `categories`) with real models.

---

### CR-C04 — Write-retry discipline applied to workers but not to request-path writes (P2)

**Where:** `commit_with_retry`/`run_with_retry` (`app/utils/db_retry.py`) is used in
`app/workers/sync_worker.py`, `enrichment_worker.py`, `health_check_worker.py`, `app/api/ai.py`,
`app/services/subtitle_service.py`, `app/cli.py`. It is **not** used by the request-path / other writers,
which call plain `await db.commit()`: `app/api/tv_auth.py` (start/approve/status/complete, `:220,286,327,366`),
`app/api/live.py:241` (EPG cache write), `app/api/accounts.py:88` (plus `delete_account:124` relying on
`get_db`'s auto-commit and `update_account:115` on `flush`), `app/api/categories.py:140`,
`app/services/category_service.py` (4×), `app/services/api_key_service.py` (3×),
`app/services/nfo_import_service.py`, `app/services/recommendation_service.py`.

**What:** House rule (f) / §3 DB row: "écriture concurrente protégée par `commit_with_retry`". The heavy
concurrent writers comply; the interactive writers do not. The pooled `busy_timeout=60000`
(`app/db/database.py:15,90`) absorbs most contention, which is why this is P2 and not higher, but the
explicit rule "ne jamais ouvrir plusieurs writers sans retry" (§9-8) is only half-followed.

**Impact:** Under a long WAL-writer hold (e.g. stream validation), an interactive write can still surface a
raw `OperationalError: database is locked` to the client instead of being retried. Inconsistent transaction
handling across endpoints (`create` commits, `update` flushes, `delete` relies on the dependency) also
hurts readability.

**Fix direction:** Route interactive writes through `commit_with_retry`, and standardize on one
commit-vs-flush convention in the accounts/categories routers.

---

### CR-C05 — Schema truth duplicated between ORM model and migrations (P2) — **RÉSOLU 2026-07-11**

**Where:** `Base.metadata.create_all` (`app/db/database.py:92`) builds the full ORM schema first, including
columns declared in `app/models/database.py` — `is_adult:102`, `is_in_allowed_categories:101`,
`original_title:80`, `tagline:81`, `tvdb_id:86`, `imdb_rating:88`, `cast_json:92`, `cast`, etc. Migrations
then run `ALTER TABLE media ADD COLUMN` for the **same** columns: `_migration_003:96-99`,
`_migration_005:141-144`, `_migration_013:400-403`, `_migration_014:447-455` (13 NFO columns).

**What:** Two independent sources of truth for the same schema. On a fresh DB, `create_all` already made the
columns, so every one of those `ADD COLUMN` statements raises `duplicate column name`, is swallowed by the
per-column `try/except`, and logged as a WARNING at every cold start (observed in this session's test run).

**Impact:** Noisy, alarming-looking startup logs on a healthy fresh DB; and a genuine drift hazard — because
the failure is caught and logged as "may already exist", a column that exists in the ORM model but whose
migration is broken (or vice-versa) fails **silently**. Maintainers must keep both lists in lock-step by
hand.

**Fix direction:** Pick one source of truth. Either keep additive columns out of the ORM model and let
migrations own them, or (simpler) probe `PRAGMA table_info(media)` before `ADD COLUMN` so the idempotency is
an explicit no-op instead of an exception-swallow, and downgrade the "already exists" log to DEBUG.

**Résolution (2026-07-11) :** ajout d'un helper `_column_exists(conn, table, column)` (`app/db/migrations.py:49`,
`PRAGMA table_info`) sondé **avant** chaque `ALTER TABLE … ADD COLUMN` — migrations 002 (`category_filter_mode`),
003 (`is_in_allowed_categories`), 004 (`existing_tmdb_id`/`existing_imdb_id`), 005 (`cast`), 010
(`existing_summary`), 013 (`is_adult`) et 014 (13 colonnes NFO). Sur une DB fraîche (colonnes déjà créées par
`create_all`), la colonne est détectée présente → **aucun** `ALTER` tenté, **aucun** WARNING ; sur une DB
existante (colonne manquante), l'`ADD COLUMN` s'exécute normalement. Le `try/except` autour de l'`ALTER` est
conservé comme filet de sécurité (course possible avec un autre process — `init_db()` tourne dans chaque
worker, cf. CLAUDE.md piège 7), mais ne se déclenche plus jamais dans le cas nominal. Les `CREATE INDEX IF NOT
EXISTS` associés (003/013) sont désormais exécutés inconditionnellement (déjà silencieusement idempotents,
ne changent pas le schéma final). **Preuve** : script one-shot chargeant l'ancien (`git show HEAD:…`) vs le
nouveau `migrations.py` sur une DB fraîche identique → ancien = **20** WARNINGs `duplicate column name`,
nouveau = **0** (fail-pre/pass-post confirmé). Test de garde :
`tests/test_migrations_no_duplicate_warning.py` (4 tests : chaîne complète silencieuse sur DB fraîche,
re-run idempotent toujours silencieux, helper `_column_exists` correct, chaque migration formerly-noisy
prend le court-circuit). Schéma final **inchangé** (mêmes colonnes/index, DB fraîche ⇆ DB migrée convergent
toujours) — uniquement la façon dont l'idempotence est sondée/loggée a changé, conformément au fix direction.

---

### CR-C06 — No linter / formatter wired anywhere (debt)

**Where:** `requirements-dev.txt` = pytest / pytest-asyncio / respx only. `pyproject.toml` has only
`[tool.pytest.ini_options]` — no `[tool.ruff]`, `[tool.black]`, `[tool.isort]`, `[tool.mypy]`. No
`.ruff.toml` / `ruff.toml` / `setup.cfg` / `.flake8` / `.pre-commit-config.yaml` at the repo root.
`.github/workflows/tests.yml` runs `pytest` only — no lint step.

**What:** There is zero automated style/lint/type enforcement. This is why local drift (unused imports,
inline imports, inconsistent commit patterns, snake_case leak) goes uncaught.

**Impact:** No mechanical guardrail for the very conventions this dimension audits. Every convention breach
above must be caught by human review.

**Fix direction:** Add `ruff` (lint + format) to `requirements-dev.txt`, a minimal `[tool.ruff]` config in
`pyproject.toml`, and a `ruff check` + `ruff format --check` step in `tests.yml` before `pytest`.

---

### CR-C07 — `pydantic-settings` declared but never used (debt)

**Where:** `requirements.txt:12` pins `pydantic-settings>=2.1`. No `pydantic_settings` / `BaseSettings`
import exists anywhere in `app/` (grep = 0 hits). `app/config.py:21` is a hand-rolled `class Settings` using
`os.getenv` + `python-dotenv`.

**What:** Dead runtime dependency — installed (and its transitive weight shipped in the 2 GB image) but
unreferenced.

**Fix direction:** Either drop the dependency, or migrate `Settings` to `pydantic-settings` `BaseSettings`
(which would also give free type coercion + validation and remove the bespoke `_safe_int`).

---

### CR-C08 — Dead code: `sanitize_edition_label` / `_EDITION_INVALID_CHARS` (debt)

**Where:** `app/plex_generator/naming.py:7` (`_EDITION_INVALID_CHARS`) and `:24`
(`sanitize_edition_label`). Grep across `app/` finds no caller outside the definition itself; the comment at
`naming.py:176` confirms "no Plex-only edition tag" — version labels are ` - Label` (Plex + Jellyfin), the
`{edition-…}` scheme is gone.

**What:** Orphaned helper + regex retained after the feature that used them was removed.

**Fix direction:** Delete both, or add a test that pins the intended future use if kept deliberately.

---

### CR-C09 — Deprecated Starlette status constant + test-marker hygiene (debt)

**Where:** `app/api/ai.py` uses `status.HTTP_422_UNPROCESSABLE_ENTITY` at `:330,365,416,423,463,1142`
(and `422` literals elsewhere). In current Starlette this constant is renamed
`HTTP_422_UNPROCESSABLE_CONTENT` and the old name emits a `DeprecationWarning` (the deprecation warnings seen
in the test run originate from this usage, mirrored in the AI test files). Separately, `pyproject.toml`
enables `asyncio_mode = "auto"`, yet some synchronous tests in `tests/test_embedding_worker.py` carry a
redundant/mis-applied `@pytest.mark.asyncio`, producing 3 further warnings.

**What:** Low-impact forward-compat + test-hygiene debt. No behavioural bug today; the deprecation will
become an error on a future Starlette bump (which is currently pinned indirectly via the FastAPI
`>=0.115,<0.116` bound, so not imminent).

**Fix direction:** Prefer plain integer `422` (already used in `tv_auth.py:266`, `admin.py:188,359`) or the
new constant name; drop the stray `@pytest.mark.asyncio` from the sync tests.

---

### CR-C10 — Ad-hoc anonymous attribute-bags for auth + migration-008 out-of-file-order definition (debt) — **migration-008 half RÉSOLU 2026-07-11**

**Where:**
- `app/api/accounts.py:50-56` builds a throwaway `class TempAccount: pass` then bolts on
  `.base_url/.port/.username/.password` to pass to `xtream_service.authenticate`; `app/main.py:143-150`
  duplicates the exact same pattern with `class _Acc:`. Both stand in for a typed account object.
  **Still open** — out of scope for the migration-focused fix below (separate `TempAccount`/`_Acc` dedup
  concern, cf. task scoping).
- ~~`_migration_008_ai_embeddings` is **defined** at the bottom of `app/db/migrations.py:467`, ~200 lines
  after `_migration_014`, while being **executed** mid-chain (`migrations.py:28-29`) on a dedicated
  connection. A maintainer reading top-to-bottom sees the chain jump 007 → 009 with 008 far below 014.~~
  **Fixed** — see résolution below.

**What:** Untyped dynamic attribute bags defeat the "type hints at boundaries" intent and duplicate a shape
that a small `@dataclass` (or a Pydantic `AuthTarget`) would express once. The migration definition ordering
is a readability trap (execution order is correct; file order is not).

**Fix direction:** Extract a tiny typed `XtreamCredentials` dataclass shared by both call sites; move the
`_migration_008` definition up between 007 and 009 (keep its dedicated-connection execution).

**Résolution (2026-07-11, migration-008 half only) :** `_migration_008_ai_embeddings`'s **definition** moved
from the tail of `app/db/migrations.py` to its numeric position, right between `_migration_007_add_stream_validation_index`
and `_migration_009_create_tv_auth_sessions`. This is a pure textual move — Python resolves function names at
call time, and every `_migration_*` function is already defined by the time `run_migrations()` runs, so the
**execution order in `run_migrations()` is byte-identical** (008 still runs on its own dedicated
`engine.begin()` connection, at the same point in the call sequence, right after 007's `await` and before
009's). A clarifying comment was added at both the definition (why it takes a raw `conn` instead of an
`AsyncEngine`, and why it needs sqlite-vec loaded on that connection) and the call site in `run_migrations()`
(why it's on its own transaction block instead of joining the `await _migration_NNN(engine)` list). Verified:
all migration-008-dependent tests (`test_ai_migration.py`, `test_ai_rank*.py`, `test_ai_jobs.py`,
`test_ai_explain.py`, `test_ai_blurb.py`, `test_ai_search.py`, `test_ai_503_detail.py`, `test_ai_status.py`,
`test_ai_assistant.py`, `test_subtitle_translate.py`, `conftest_ai.py`) still import
`_migration_008_ai_embeddings` and pass unchanged (import is by name, not by file position).
**The `TempAccount`/`_Acc` dedup half of this finding remains open** (explicitly out of scope here).

---

## What's healthy

- **Pydantic-at-frontier + camelCase is consistently applied** across `schemas.py` (every model sets
  `alias_generator=to_camel`, `populate_by_name=True`; list/response models included) and the `tv_auth.py`,
  `plex.py`, `api_keys.py` local models. `response_model_by_alias=True` is used where it matters.
- **The `/api/ai` router is exemplary**: every endpoint declares `response_model` + `response_model_by_alias`,
  and the 503 taxonomy is coherent and intentional — `verify_api_key` router guards (config/sqlite-vec,
  `deps.py:76-85`), endpoint `EmbeddingUnavailableError → 503` (`ai.py:351,450,550,651`), and a distinct LLM
  `_ollama_503` (`ai.py:855`), with 422/413/404 where appropriate.
- **Secrets are not logged in clear**: TMDB key truncated to 4 chars (`config.py:118`); tv-auth logs session
  ids only, never the decrypted payload (`tv_auth.py:321,328`); the API-key plaintext is returned once and
  never logged (`api_keys.py:88`). Constant-time compares on the master secret and admin credentials
  (`deps.py:46,130-140`).
- **Migrations are idempotent**: `CREATE … IF NOT EXISTS` and guarded `ADD COLUMN` throughout
  (`migrations.py`), new migrations appended at the end of `run_migrations` (chain 001→014), with per-column
  transaction isolation in 010/014 so one guarded failure can't abort siblings.
- **Blocking discipline is correct where the rule is followed**: fastembed load + inference offloaded via
  `asyncio.to_thread` (`embedding_service.py:78,124,130`); `sqlite3.backup` offloaded
  (`main.py:313-314`); image downloads on a dedicated `ThreadPoolExecutor` with per-thread httpx clients and
  a clean shutdown hook (`storage.py:41-71`). CR-C01 is the one place this discipline lapses.
- **Logging** goes through the single `plexhub` logger with a `request_id` filter + middleware
  (`main.py:40-66`, `utils/request_context.py`), console=INFO / file=DEBUG, `propagate=False`, and a
  Windows-safe rotating handler.
- The **one `except Exception: pass`** found (`recommendation_service.py:398`) is a deliberate,
  documented JSON-parse fallback that returns `None` — not a silent error swallow.
