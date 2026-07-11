# Clean-room audit — Tests / Coverage / Quality-gates

**Verdict:** 3 / 5

A broad, genuinely-green suite (**484 passed, 1 deselected** this session, 24 s) with strong,
well-structured coverage on the AI stack, plex-generator, NFO import, subtitle translation,
TMDB scoring and dedup/convergence. Good discipline: respx HTTP mocks, isolated in-memory DB,
service-level + HTTP-level tests, meaningful regression comments.

But the green is undermined by four serious gaps that let real regressions ship undetected:
1. the single most important security control — **fail-closed auth on the entire JSON API** — has
   **no rejection test** (a regression that re-opens the catalogue/sync/plex API ships green);
2. the **largest module** (`sync_worker.py`, 1390 LOC) has **no orchestration test** — only pure helpers;
3. **all startup wiring** (lifespan / master-election / scheduled pipeline / auto-generate) is untested
   because the `api_client` fixture deliberately skips the lifespan;
4. the "flaky" test that CI permanently deselects is **not flaky — it catches a real, deterministic,
   live bug** in EPG base64 decoding.

There is also **no coverage tooling and no enforced gate** — the suite cannot be measured or thresholded.
This is a soft 3: excellent breadth, but the untested critical gates + a masked real bug pull it down.

---

## Coverage map (high-risk modules → tested?)

| Module | LOC | Test file(s) | Real coverage |
|---|---|---|---|
| `workers/sync_worker.py` | 1390 | `test_sync_worker.py` | **partial** — only pure helpers (`_parse_duration_ms`, `_compute_dto_hash`, `_get_account_lock`, `cleanup_orphan_enrichment_queue`, sync-job registry). `run_all_accounts`/`sync_account` orchestration, incremental upsert, `differential_cleanup*`, adult-flag integration = **untested** |
| `app/main.py` (lifespan/scheduler) | 442 | none | **n** — fixture skips lifespan; election/scheduler/auto-provision/auto-generate never run |
| `workers/health_check_worker.py` | 558 | `test_health_check_concurrency.py` | **partial** — `_account_concurrency` clamp + semaphore plumbing only; `_check_one` (HEAD→Range, magic bytes, circuit breaker, broken threshold) fully mocked |
| `workers/enrichment_worker.py` | 340 | `test_enrichment_scraping.py` | **partial** — cache short-circuit + `_apply_enrichment_results`; `run()` orchestration, daily-limit, retry/attempts = untested |
| `services/tmdb_service.py` | 646 | `test_tmdb_service_mocked.py` | **y** — scoring, year guard, original-title, token-set, summary tie-break, 429 retry |
| `services/subtitle_service.py` | 625 | `test_subtitle_translate.py` | **y** (621-line test) |
| `services/nfo_import_service.py` | 888 | `test_nfo_import.py` | **y** — `import_nfo` exercised end-to-end |
| `services/aggregation_service.py` | 253 | `test_plex_dedup.py`, `test_plex_convergence.py` | **y** |
| `services/recommendation_service.py` | 461 | `test_recommendation_service.py` + AI HTTP tests | **y / partial** |
| `services/api_key_service.py` | 130 | (stubbed in `test_ai_deps.py`) | **n** — `resolve`/mint/revoke/expiry/IP logic never executed |
| `plex_generator/*` | ~1100 | `test_plex_generator.py`, `test_plex_dedup.py`, `test_storage_atomic.py` | **y** |
| `api/deps.py` (auth) | 140 | `test_ai_deps.py` | **partial** — only `verify_api_key`; `verify_backend_secret` / `verify_master_key` / `verify_admin_basic_auth` rejection paths untested |
| `api/live.py` | 275 | `test_utilities.py` (helper only) | **n** — endpoints untested; base64 helper test deselected (see CR-T01) |
| `api/accounts.py` | 184 | none | **n** — incl. `DELETE /api/accounts/{id}` |
| `api/sync.py` | 96 | none | **n** |
| `api/plex.py` | 69 | none | **n** — `POST /api/plex/generate` + client `outputDir` untested |
| `api/categories.py` / `api/stream.py` / `api/api_keys.py` | 155/71/108 | none | **n** |
| `embedding_worker`, `db_retry`, `payload_crypto`, `string_normalizer`, `ttl_cache`, `backup_db`, `strip_titles` | — | dedicated tests | **y** |

---

### CR-T01 — The "flaky" deselected test is not flaky: it masks a real, deterministic EPG-decode bug (P1)

**Where:** `tests/test_utilities.py:30-35` (`TestBase64Decode::test_text_that_looks_like_base64_but_decodes_to_garbage`),
deselected in CI at `.github/workflows/tests.yml:29-33` ("pre-existing flaky case"). Target code:
`app/api/live.py:24-35` (`_try_base64_decode`), used on live EPG data at `app/api/live.py:221` (title) and `:224` (description).

**What:** I executed the function directly (this session):
```
'News' -> '5�,'   MANGLED
'Test' -> 'M�-'   MANGLED
'Info' -> '"w�'   MANGLED
'Kids' -> "*'l"        MANGLED
```
`_try_base64_decode` rejects only ASCII **control** chars (`ord(c) < 32`, `live.py:31`) but not the Unicode
replacement char `U+FFFD` that `.decode("utf-8", errors="replace")` (`live.py:29`) emits on invalid UTF-8.
So any short string that is valid base64 by shape ("News", "Test", "Info", "Kids" — extremely common EPG
program titles) is silently corrupted into replacement-char garbage. The test asserts the **correct**
behaviour (`_try_base64_decode("News") == "News"`) and fails **deterministically** — it is not a heuristic /
non-deterministic flake. Permanent deselection converts a genuine failing regression test into permanent
green cover for a live data-corruption bug on the `/api/live/epg` path.

**Impact:** Every push shows a false-green while EPG titles/descriptions that happen to be 4/8-char base64-shaped
words are mangled in the API response. The CI comment ("flaky … unrelated to current work") is inaccurate and
hides the defect. (The underlying decode fix is another auditor's lane; the **testing** defect — mislabelling a
real failure as flaky and excluding it — is squarely here.)

**Fix direction:** Do not deselect. Fix `_try_base64_decode` to also reject `�` (or decode with
`errors="strict"` and fall back to the original on `UnicodeDecodeError`), then re-enable the test and drop the
`--deselect`. Add table-driven cases for the mangled words above as regression guards.

---

### CR-T02 — Fail-closed auth on the whole JSON API has zero rejection tests (P1, borderline P0)

**Where:** Guard wiring `app/main.py:396-405` (`_guard = [Depends(verify_backend_secret)]` on
`accounts`/`categories`/`live`/`media`/`stream`/`sync`/`plex`), `:412`/`:423`/`:428` (`verify_admin_basic_auth`
on `/admin`, `/docs`, `/openapi.json`), `:438` (`verify_master_key` on `api_keys`). Deps at `app/api/deps.py:59,88,108`.

**What:** `grep verify_backend_secret tests/` → **0 matches.** No test asserts that any catalogue/sync/plex
endpoint returns **401 without a key** — including destructive `DELETE /api/accounts/{id}` and
`POST /api/plex/generate` (client-supplied `outputDir`). The only 401 assertions in the suite target the **AI**
router (`verify_api_key`, e.g. `test_ai_rank.py:122`, `test_ai_search.py:171`, `test_ai_status.py:129`) and
tv-auth `/approve` (`test_tv_auth.py:315,320,364`). `test_admin.py` only exercises the **happy path** with valid
Basic Auth (`:32,54,66`) — never the 401/503 rejection. `verify_master_key` (`api_keys`) has **no test at all**.
Existing catalogue HTTP tests supply auth to pass (`test_adult_classification.py:33,208`) rather than assert rejection.

**Impact:** The single most important security control (auth on the entire catalogue/sync/plex surface) is a
one-line-per-router declaration in `main.py`. If `dependencies=_guard` is dropped from any mount, or a dep
regresses, **CI stays green** and the whole API silently re-opens. The recent "supply auth to the 6 fail-closed
tests" commit patched broken tests to pass with keys but did **not** add a control asserting fail-closed behaviour.

**Fix direction:** Add negative tests per guarded router group: unauthenticated `GET /api/media/movies`,
`DELETE /api/accounts/{x}`, `POST /api/sync`, `POST /api/plex/generate` → 401; `/admin` without creds → 401 and
with empty `ADMIN_PASSWORD` → 503; `api_keys` mgmt with a per-user (non-master) key → 401. A single parametrized
"every guarded route rejects an anonymous request" test would lock the wiring.

---

### CR-T03 — `sync_worker` orchestration (1390 LOC) is untested — only pure helpers (P1)

**Where:** `app/workers/sync_worker.py` (1390 LOC) vs `tests/test_sync_worker.py` (236 LOC).

**What:** The test imports and covers only leaf helpers: `_parse_duration_ms`, `_safe_duration`,
`_get_account_lock`, `_compute_dto_hash`, `_record_sync_job`, `cleanup_orphan_enrichment_queue`
(`test_sync_worker.py:9-18`). The core pipeline — `run_all_accounts()`, `sync_account()`, the incremental
VOD/series/episode/Live/EPG upsert by `dto_hash`/`content_hash`, `differential_cleanup*`, and the
`update_media_adult_flags` integration at `sync_worker.py:1290` — is **never invoked** by any test
(`grep run_all_accounts|sync_account tests/` → only the import line). This is the highest-churn, most
business-critical module in the repo.

**Impact:** Regressions in the sync/upsert/cleanup path (the product's core data flow) ship green. Known-risky
sub-flows have no regression net: episode-cleanup gap, adult-flag reset idempotence, dto_hash volatility (the
one dto_hash edge — `test_sync_worker.py:111-126` — is well done, but it's the exception).

**Fix direction:** Add an integration test that runs `sync_account()` against a respx-mocked `player_api.php`
(xtream_mock already exists) and a real in-memory DB: assert upsert counts, incremental no-op on unchanged
`dto_hash`, and cleanup of removed items. Reuse the `xtream_mock` fixture.

---

### CR-T04 — Startup wiring (lifespan / master-election / scheduler / auto-generate) is untested (P1)

**Where:** `tests/conftest.py:48-67` — `api_client` builds `ASGITransport(app=app)` which **does not run the
lifespan** (confirmed: no `LifespanManager`, docstring at `:50-54` states the skip is intentional). Target:
`app/main.py` lifespan — `init_db()`, `fcntl.flock` election (`main.py:226-227`), APScheduler wiring, auto-provision,
`_auto_generate_plex_library` (`main.py:77-118`), initial non-blocking boot run (`main.py:311-321`),
`scheduled_sync_enrich_generate` (`main.py:248-273`).

**What:** No test triggers the lifespan or exercises any startup path. `grep _auto_generate|scheduled_sync|flock|lifespan
tests/` matches only files that `import app.main.app` — none drive startup. So master election, the serialized
scheduled pipeline, the `max_instances=1`/`coalesce` config, and boot-time library generation are all unverified.

**Impact:** Regressions in startup wiring (e.g. scheduler misconfig, a broken auto-generate, election that never
releases the flock) ship green. This is inherent to the chosen fixture design; it's a deliberate blind spot, but a
large one for a service whose correctness depends on the master electing and scheduling correctly.

**Fix direction:** Add a lifespan integration test (POSIX/CI-only, guarded by `sys.platform`) using
`httpx.ASGITransport` + `asgi-lifespan.LifespanManager` (or `TestClient` context manager) with the scheduler
monkeypatched to a no-op, asserting: election acquires/releases the flock, the scheduler registers the pipeline
job with `max_instances=1`/`coalesce=True`, and auto-generate is gated by `PLEX_LIBRARY_DIR`.

---

### CR-T05 — Stream-validation and enrichment orchestration logic is untested (P2)

**Where:** `app/workers/health_check_worker.py` (558 LOC) vs `tests/test_health_check_concurrency.py`;
`app/workers/enrichment_worker.py` (340 LOC) vs `tests/test_enrichment_scraping.py`.

**What:** `test_health_check_concurrency.py` mocks `_check_one` wholesale (`:98-109`) — so the *actual* validation
logic (HEAD→Range GET, magic-bytes sniffing, 404/403 definitive-fail, `STREAM_BROKEN_THRESHOLD` counting, the
90%-failure circuit breaker, recheck window) is never executed; only the per-account concurrency **clamp** and
semaphore plumbing are. `test_enrichment_scraping.py` covers the scrape-cache short-circuit and
`_apply_enrichment_results`, but not `enrichment_worker.run()` (Phase-1/Phase-2 orchestration, `ENRICHMENT_DAILY_LIMIT`
enforcement, `MAX_ATTEMPTS` retry).

**Impact:** The stream health-check (which decides `is_broken`, i.e. what the app hides) and the enrichment budget
enforcement can regress silently. No regression test for the known daily-limit accounting risk.

**Fix direction:** Test `_check_one` against respx responses (200 with/without magic bytes, 404, 403, empty body,
wrong content-type) asserting the broken decision; test that `run()` stops at `ENRICHMENT_DAILY_LIMIT`.

---

### CR-T06 — `api_key_service` (per-user key auth) is stubbed out, never executed (P2)

**Where:** `app/services/api_key_service.py` (130 LOC). Only reference in tests: `tests/test_ai_deps.py:39,67`
where `api_key_service.resolve` is **replaced by a stub** returning `None`.

**What:** The real per-user-key logic — key hashing, `resolve()` lookup, revoke, expiry checks, `client_ip`
handling — is a security-sensitive auth path and has **zero executing tests**. The deps test deliberately avoids
it. `api_keys` router (`app/api/api_keys.py`, 108 LOC) has no HTTP test either.

**Impact:** A bug in key hashing/expiry/revocation (e.g. an expired or revoked key still resolving) would not be
caught. Per-user keys are an authentication mechanism.

**Fix direction:** Unit-test `api_key_service` against the in-memory DB: mint→resolve roundtrip, revoked key →
None, expired key → None, unknown key → None. Add HTTP tests for the mgmt endpoints (master-only via `verify_master_key`).

---

### CR-T07 — No HTTP-level tests for 7 routers; media raw endpoints barely hit; plex path unbounded (P2)

**Where:** Routers with **no HTTP endpoint test**: `accounts.py` (184), `categories.py` (155), `live.py` (275),
`stream.py` (71), `sync.py` (96), `plex.py` (69), `api_keys.py` (108). HTTP surface actually exercised across the
suite: `/api/health`, `/api/media/movies[/unified]`, `/admin*`, `/api/tv-auth/*`, `/api/ai/*`, `/api/ai/embed/*`
(established by grepping all `*_client.<verb>(...)` calls).

**What:** The catalogue/sync/plex CRUD surface is validated only indirectly through service-layer tests
(`test_unified_by_id.py` calls `media_service`/`_build_versions` directly, bypassing HTTP and auth). The
`POST /api/plex/generate` endpoint accepts a client `outputDir` (per §10 CR-S02) and has **no test** — neither a
happy-path nor a path-confinement regression guard.

**Impact:** Response-model contracts (camelCase aliases), pagination/filter query params, and the plex-generate
path handling can regress with green CI. Given plex-generate writes to a client-named directory, the absence of a
confinement regression test is notable.

**Fix direction:** Add thin HTTP tests per router (with auth headers) asserting status + response schema; add a
`POST /api/plex/generate` test with a traversal-y `outputDir` asserting it is confined/rejected.

---

### CR-T08 — Fixture fidelity: WAL, `busy_timeout`, and lock-retry can't be exercised in-memory (P2)

**Where:** `tests/conftest.py:23-26` — `create_async_engine("sqlite+aiosqlite:///:memory:")` then
`PRAGMA journal_mode=WAL`. `tests/test_db_retry.py` (whole file). Target: `app/db/database.py:80-85`
(WAL + `busy_timeout`), `app/utils/db_retry.py` (`commit_with_retry`/`run_with_retry`).

**What:** `PRAGMA journal_mode=WAL` on an `:memory:` database is a **no-op** (SQLite silently keeps `memory`
journal mode — WAL requires a file). So no test runs against real WAL semantics. `test_db_retry.py` drives the
retry dispatch with **synthetic** `OperationalError("database is locked")` (`:10-13,45`) — it verifies the
retry/backoff *control flow* but never a real lock-under-contention (which needs two file-backed writers and the
production 60 s `busy_timeout`). The production lock/retry path is therefore validated only at the branch level.

**Impact:** Concurrency regressions that only manifest under real WAL + `busy_timeout` (e.g. writer starvation,
retry exhaustion timing) are structurally out of reach of the suite. Acceptable for unit level but leaves the
reliability-critical path with no integration net.

**Fix direction:** Add one file-backed (`tmp_path`) engine fixture and a two-writer contention test that forces a
real "database is locked" and asserts `commit_with_retry` recovers; keep it out of the default in-memory path.

---

### CR-T09 — No coverage tooling and no enforced gate (debt)

**Where:** `requirements-dev.txt` (pytest, pytest-asyncio, respx only — no `pytest-cov`/`coverage`).
`.github/workflows/tests.yml:28-33` (`pytest -v --deselect …`, no `--cov`, no threshold, no `--cov-fail-under`).
No lint/format gate (ruff/black absent from dev deps and CI).

**What:** Coverage is neither measured nor thresholded; the suite's real line/branch coverage is unknown and
unenforced. No `--strict-markers`. Combined with the gaps above, there is no automated signal that a large module
is untested.

**Impact:** Coverage can silently erode; the untested modules (sync orchestration, api_key_service, routers) are
invisible to CI. No floor prevents shipping code with no tests.

**Fix direction:** Add `pytest-cov`, run `pytest --cov=app --cov-report=term-missing --cov-fail-under=<floor>` in
CI; wire ruff as a separate CI step.

---

### CR-T10 — CI scope + marker strictness gaps (debt)

**Where:** `.github/workflows/tests.yml:3-7` — `on: push:/pull_request: branches:[main]` only.

**What:** The active development branch is `develop`; feature PRs merged into `develop` (the ~50-commit
v1.1.0→v1.1.5 delta lives there) are **not** gated by CI until a PR targets `main`. There's no `-W error` /
`filterwarnings=error`, so deprecations and warnings never fail. `pyproject.toml:12-15` additionally *ignores*
`PytestUnhandledThreadExceptionWarning` — meaning exceptions raised in the storage `ThreadPoolExecutor` or the
`SafeRotatingFileHandler` during a test are **swallowed** rather than surfaced.

**Impact:** Work can accumulate on `develop` without test gating; genuine thread-level failures are hidden by the
warning filter.

**Fix direction:** Add `develop` (or `**`) to the CI triggers; narrow/remove the thread-exception ignore or scope
it to specific known-benign teardown paths.

---

### CR-T11 — Mis-marked async tests + live deprecated constant in app code (debt)

**Where:** `tests/test_embedding_worker.py:24` (`pytestmark = pytest.mark.asyncio`) applied to the sync functions
at `:53,60,73`. Deprecated constant `status.HTTP_422_UNPROCESSABLE_ENTITY` in **app** code:
`app/api/ai.py:330,365,416,423,463,1142`.

**What:** The three sync tests (`test_make_job_id_format`, `test_fifo_eviction_at_cap`,
`test_register_job_in_place_update`) inherit the module-level `asyncio` mark. I ran them: they **do execute and
pass** (3 passed) — the mark is a no-op, so they are *not* silently skipped — but each emits a `PytestWarning`
("marked with '@pytest.mark.asyncio' but it is not an async function"). A future pytest-asyncio may promote this
to an error, at which point they'd error out. Separately, `HTTP_422_UNPROCESSABLE_ENTITY` is deprecated (Starlette
renamed it `HTTP_422_UNPROCESSABLE_CONTENT`) and is used in **production** `api/ai.py` (6 sites), not just tests —
it will become an `ImportError`/`AttributeError` in a future Starlette/FastAPI, breaking those endpoints.

**Impact:** Warning noise today; latent breakage tomorrow. The 422 constant is app-code debt, not a test-only issue.

**Fix direction:** Remove the module-level `pytestmark` (or split sync tests into their own module); migrate the 6
`ai.py` sites to the new `HTTP_422_UNPROCESSABLE_CONTENT` (or the numeric `422`).

---

## What's healthy

- **484 green, deterministic, fast** (~24 s); one deselected test — but that one is a real bug, see CR-T01.
- **AI stack is thoroughly tested** at HTTP level: `rank`, `rank-multi`, `search`, `assistant`, `blurb`,
  `explain`, `subtitles`, `embed status/rebuild/jobs`, plus 503/401 paths (`test_ai_*.py`, incl.
  `test_ai_503_detail.py`, `test_ai_deps.py`).
- **TMDB matching/scoring is well covered** — year guard, original-title, token-set word-order, summary tie-break,
  429-with-Retry-After, cache-hit-no-refetch (`test_tmdb_service_mocked.py`).
- **Plex generator / dedup / convergence / NFO import** have deep, scenario-driven tests
  (`test_plex_generator.py`, `test_plex_dedup.py`, `test_plex_convergence.py`, `test_nfo_import.py`,
  `test_storage_atomic.py`).
- **Subtitle translation** has a large, edge-aware suite (413/422/cache/mismatch fallback).
- **Good regression discipline**: dto_hash volatile-field cases (`test_sync_worker.py:111-126`), embedding
  cursor-not-OFFSET spy (`test_embedding_worker.py:114-139`), constant-time-auth grep guard
  (`test_ai_deps.py:84-96`), respx-based hermetic HTTP.
- **`embedding_worker.run_embedding_rebuild`** and **`nfo_import_service.import_nfo`** are exercised end-to-end
  (not just helpers) — the model to follow for `sync_worker`.
