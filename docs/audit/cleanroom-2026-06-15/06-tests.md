# CR — Tests & Coverage

**Dimension score: 58 / 100**

Rationale: a competent, well-structured suite (28 files, ~299 tests, real-SQLite + respx integration patterns) is undermined by a **broken CI signal** — `main` is RED because a stale test was not updated after a model change, and the CI `--deselect` masks only an unrelated flaky test. Coverage of the AI / tv-auth / plex-generator subsystems is genuinely strong, but the heaviest production-critical modules (the 1314-line `sync_worker` orchestration, the entire `health_check_worker`, and 7 of the catalogue API routers) have **no behavioral tests at all**, and there is **no coverage tooling installed** so the real number is unknown.

Suite health assessment: The suite runs fast (~10 s) and is genuinely deterministic where it covers code — the AI, tv-auth, plex-generator, tmdb-service, db-retry and string-normalizer areas are tested with care (real engines, respx HTTP mocks, edge cases). But the green-looking local run hides a red CI: 1 real assertion failure currently fails the pipeline on every push to `main`, and the test pyramid is heavily skewed toward small pure-function helpers while the orchestration code that actually moves data (sync, enrichment scheduling, stream validation) is untested. Net: the tests that exist are good; the gaps are large and concentrated in the highest-risk modules.

---

## Module → test coverage map

| App module | Lines | Test file(s) | Qualitative coverage |
|---|---|---|---|
| `workers/sync_worker.py` | 1314 | `test_sync_worker.py`, `test_db_layer.py` (`upsert_media_batch`) | **thin** — only pure helpers (`_parse_duration_ms`, `_safe_duration`, `_get_account_lock`, `_compute_dto_hash`, job tracking) + `upsert_media_batch`. `run_all_accounts`/`sync_account`/`differential_cleanup*`/EPG/Live ingest: **none** |
| `workers/health_check_worker.py` | 470 | — | **none** — HEAD→Range GET, magic bytes, circuit breaker, `run_pipeline_validation`, broken-threshold, recheck-window: zero tests |
| `workers/enrichment_worker.py` | 243 | (only the `tmdb_service` it calls, via `test_tmdb_service_mocked.py`) | **thin/none** — `run()`, Phase1/Phase2, daily-limit accounting, `MAX_ATTEMPTS`, concurrency 8: not tested. Enrichment *queue selection* tested in `test_db_layer.py` |
| `workers/embedding_worker.py` | 146 | `test_embedding_worker.py` (6), `test_ai_jobs.py` (5) | **good** — pending-row processing, cursor pagination (no OFFSET), FIFO eviction at cap, abort-on-unavailable, job registry |
| `services/xtream_service.py` | 264 | `test_retry_logic.py` (retry constants only) | **thin** — only `_RETRY_DELAYS`/`_RETRYABLE` constants; no `player_api.php` request/parse path tested |
| `services/tmdb_service.py` | 329 | `test_tmdb_service_mocked.py` (5), `test_retry_logic.py` | **good (partial)** — search/cache/429-retry/not-configured well covered; `find_by_imdb_id`, `append_to_response` details path not directly tested here (exercised indirectly in AI tests) |
| `services/recommendation_service.py` | 233 | `test_recommendation_service.py` (7) | **good** — `load_cached_vectors`/`hydrate_misses`/`cosine_rank`, HYDRATE_CAP |
| `services/embedding_service.py` | 131 | `test_embedding_service.py` (6) | **good** — L2-normalize, centroid, dim, unavailable error (model load mocked) |
| `services/media_service.py` | 231 | `test_db_layer.py` | **thin** — search path against real SQLite; not all query branches |
| `services/category_service.py` | 394 | — | **none** |
| `services/stream_service.py` | 71 | — | **none** — `build_stream_url`/`parse_rating_key` untested |
| `services/nfo_import_service.py` | 693 | `test_nfo_import.py` (13) | **good** |
| `api/health.py` | 37 | `test_api_health.py` (3) | **good** — incl. RequestId middleware echo |
| `api/ai.py` | 423 | `test_ai_rank/rank_multi/status/jobs/deps/503_detail/openapi/migration` (~34) | **good** — the best-covered router |
| `api/tv_auth.py` | 387 | `test_tv_auth.py` (17) | **good** — full device-flow, TTL, one-shot, encryption-at-rest, scrub, auth, migration 009 |
| `api/admin.py` | 292 | `test_admin.py` (3) | **thin** — 3 HTML smoke tests only |
| `api/accounts.py` | 184 | — | **none** (no endpoint test) |
| `api/categories.py` | 155 | — | **none** |
| `api/live.py` | 275 | `test_utilities.py` (`_try_base64_decode` only) | **thin** — one helper; no endpoint test |
| `api/media.py` | 137 | — | **none** |
| `api/stream.py` | 36 | — | **none** |
| `api/sync.py` | 96 | — | **none** |
| `api/plex.py` | 96 | — | **none** — `POST /api/plex/generate` (unauthenticated, runs generation) untested at endpoint level |
| `plex_generator/*` | ~1200 | `test_plex_generator.py` (51), `test_storage_atomic.py` (7) | **good** — models, naming, mapping, NFO, storage atomicity, generator classify; not a full DB→disk end-to-end run |
| `db/migrations.py` | — | `test_ai_migration.py`, `test_tv_auth.py` (M009) | **partial** — M008 + M009 idempotence/shape; M001–M007 not directly asserted |
| `db/database.py` | — | (exercised by AI/tv fixtures) | **thin** — no direct test of WAL PRAGMA / `register_sqlite_vec_listener` failure path |
| `utils/db_retry.py` | — | `test_db_retry.py` (7) | **good** |
| `utils/ttl_cache.py` | — | `test_ttl_cache.py` (8) | **good** |
| `utils/string_normalizer.py` | — | `test_string_normalizer.py` (43) | **good** |
| `utils/tasks.py` | — | `test_utilities.py` | **good** |
| `utils/payload_crypto.py` | — | `test_tv_auth.py` (encryption-at-rest) | **partial** — exercised via tv-auth; no direct unit test of key-resolution fallbacks |
| `utils/server_id.py` | — | `test_sync_worker.py` + 4 others | **good** |
| `utils/unification.py` | — | — | **none** |
| `utils/request_context.py` | — | `test_api_health.py` (indirect, X-Request-ID echo) | **thin** — contextvar/middleware only smoke-tested |
| `utils/metrics.py` | — | — | **none** |
| `scripts/backup_db.py` | — | `test_backup_db.py` (6) | **good** |
| `scripts/strip_titles_pollution.py` | — | `test_strip_titles_pollution.py` (18) | **good** |
| `main.py` lifespan / master-election (`fcntl.flock`) | — | — | **none** — explicitly skipped by `conftest.py:52` |
| `config.py` | — | `test_utilities.py` (`_safe_int`) | **thin** |

---

## Findings

### CR-T01 — CI is RED on `main`: stale `test_status_shape` asserts the wrong model name
- **Severity: P0**
- **Evidence:**
  - `tests/test_ai_status.py:113` asserts `data["modelName"] == "intfloat/multilingual-e5-small"`.
  - Endpoint returns `embedding_service._resolve_model_name()` at `app/api/ai.py:418`, which resolves to `DEFAULT_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"` (`app/services/embedding_service.py:26`).
  - Reproduced: `pytest tests/test_ai_status.py::test_status_shape` → `AssertionError: 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2' == 'intfloat/multilingual-e5-small'`.
  - CI (`.github/workflows/tests.yml:31-33`) deselects **only** the base64 test, not this one → full CI run = **`1 failed, 297 passed, 1 deselected`** (reproduced locally with the exact CI deselect). The pipeline fails on every push/PR to `main`.
- **Impact:** The CI gate is permanently red, so it provides **no actionable signal** — any *new* regression is invisible because the build is already failing. This is the single most damaging test finding: a broken gate is worse than no gate (teams start ignoring it / merging red).
- **Root cause:** The embedding model was changed L6→L12 (commit `b6ad94e`) and the production model id was also reworked away from `intfloat/multilingual-e5-small`, but `test_ai_status.py` was never updated in the same commit. No "update the test in the same commit as the behavior" discipline enforced.
- **Suggested fix:** Update line 113 to assert against `embedding_service.DEFAULT_MODEL_NAME` (import it, don't hardcode the string) so the test tracks the source of truth. Then confirm CI goes green. Add a pre-merge rule: behavior-changing commits must update affected tests.

### CR-T02 — `health_check_worker` (470 lines) has zero tests — stream-validation + circuit-breaker logic is unverified
- **Severity: P1**
- **Evidence:** No test file references `health_check_worker`, `run_pipeline_validation`, `circuit`, `magic`, or `_should_recheck` (grep over `tests/` returned nothing). Module is `app/workers/health_check_worker.py` (470 lines): HEAD→Range GET, magic-byte sniffing, `STREAM_BROKEN_THRESHOLD`, definitive-failure classification (404/403/error-CT/empty/magic-fail), `STREAM_VALIDATION_RECHECK_HOURS`, per-account 90% circuit breaker.
- **Impact:** The logic that decides whether a stream is marked broken (and the circuit breaker that can disable an entire account's validation) is completely unguarded. A regression in failure-classification could silently flap thousands of streams to "broken" or never re-check them. This is user-visible (the Android app hides broken streams) and has no safety net.
- **Root cause:** Module never had tests written; the network-mock pattern needed (respx for HEAD/Range, magic bytes) exists in `conftest.py` (`xtream_mock`) but was not applied here.
- **Suggested fix:** Add `test_health_check_worker.py` covering: (1) magic-byte accept/reject, (2) HEAD-then-Range fallback, (3) broken-after-N-failures threshold, (4) definitive-failure short-circuit, (5) circuit-breaker trips at the 90% ratio. Use `respx` for HTTP and an in-memory engine for the DB.

### CR-T03 — `sync_worker` orchestration is untested: only pure helpers covered, not the data-moving flows
- **Severity: P1**
- **Evidence:** `tests/test_sync_worker.py` (29 tests) covers only `_parse_duration_ms`, `_safe_duration`, `_get_account_lock`, `_compute_dto_hash`, sync-job tracking, and `server_id` helpers (file lines 6-15, 21-153). `upsert_media_batch` is tested in `test_db_layer.py:37`. Grep for `run_all_accounts`/`sync_account`/`differential_cleanup` in `tests/` → no matches. The module is 1314 lines (`app/workers/sync_worker.py`); the §5.1 incremental-upsert / differential-cleanup flow is the largest single module in the codebase.
- **Impact:** The core sync flow — incremental upsert by `dto_hash`/`content_hash`, `differential_cleanup*` (which *deletes* rows no longer present upstream), Live/EPG ingest — is unverified end-to-end. A bug in differential cleanup is destructive (mass deletion of catalogue rows). High blast radius, no test.
- **Root cause:** `test_tmdb_service_mocked.py:3` and `:50` explicitly call themselves "Pattern for future worker tests (sync_worker, enrichment_worker)" — i.e. the team knew these were TODO and the follow-up was never done.
- **Suggested fix:** Add `sync_account()` integration tests against an in-memory engine + `xtream_mock`: seed an account, mock `player_api.php` responses, assert upsert counts, then mock a smaller second response and assert `differential_cleanup` removes exactly the absent rows (and nothing else).

### CR-T04 — Catalogue API routers (accounts/categories/live/media/stream/sync/plex) have no endpoint tests
- **Severity: P1**
- **Evidence:** Grepping `tests/` for hit endpoint paths returns only `/api/ai/*`, `/api/health`, `/api/tv-auth/*`, `/admin` (3 HTML smoke tests). No test issues a request to `/api/accounts`, `/api/accounts/{id}/categories`, `/api/live`, `/api/media`, `/api/stream/{rating_key}`, `/api/sync`, or `/api/plex/generate`. `services/category_service.py` (394 lines) and `services/stream_service.py` (71 lines) have zero references in `tests/`.
- **Impact:** The endpoints the Android app actually consumes for the catalogue (the product's primary surface) have no request/response contract tests. Pydantic alias/serialization regressions, 404/422 handling, and pagination would not be caught. `POST /api/plex/generate` is unauthenticated and triggers full library generation — running it untested at the endpoint layer is risky.
- **Root cause:** Test investment was concentrated on the newer AI and tv-auth missions; the older catalogue surface was left at the `test_api_health.py` "demo pattern" stage (which the file header explicitly calls a pattern "for future API tests").
- **Suggested fix:** Use the existing `api_client` + `monkeypatch` of `async_session_factory` pattern (as in `test_api_health.py`/`test_admin.py`) to add at least happy-path + empty + 404 tests per router. Add a direct unit test for `stream_service.build_stream_url`/`parse_rating_key` (pure, trivial, high value).

### CR-T05 — No coverage tooling installed: real coverage is unknown and unmeasurable
- **Severity: dette (P2)**
- **Evidence:** `requirements-dev.txt` = `pytest`, `pytest-asyncio`, `respx` only — no `pytest-cov`/`coverage`. CI (`tests.yml`) runs `pytest -v` with no `--cov`. There is no coverage gate or report.
- **Impact:** "Coverage to confirm" is structurally unanswerable. Gaps like CR-T02/T03/T04 are invisible to the pipeline; nobody gets a signal that a 470-line worker is at 0%. Estimated qualitative line coverage is roughly **45–55%** — strong in AI/tv-auth/plex-gen/utils, near-zero in health-check/sync-orchestration/catalogue-routers.
- **Root cause:** Coverage was never wired into dev deps or CI.
- **Suggested fix:** Add `pytest-cov` to `requirements-dev.txt`; run `pytest --cov=app --cov-report=term-missing` in CI; publish the number (no hard gate initially to avoid blocking, then ratchet a floor once CR-T01 makes CI green).

### CR-T06 — Three `embedding_worker` tests are mis-marked async; they pass but are one `filterwarnings=error` away from breaking
- **Severity: dette (P2)**
- **Evidence:** `tests/test_embedding_worker.py:24` sets `pytestmark = pytest.mark.asyncio` at module level, but `test_make_job_id_format` (line 53), `test_fifo_eviction_at_cap` (line 60), and `test_register_job_in_place_update` (line 73) are plain `def`, not `async def`. This emits 3 `PytestWarning: ... marked with '@pytest.mark.asyncio' but it is not an async function`.
- **Verification (important):** I confirmed these tests are **NOT dead/no-op** — under `asyncio_mode=auto` pytest-asyncio still runs sync tests synchronously; `pytest tests/test_embedding_worker.py::test_make_job_id_format` reports **PASSED, 1 warning** (assertions execute). However, under `-W error::pytest.PytestWarning` all three flip to **FAILED**, proving they are fragile.
- **Impact:** Cosmetic today (3 of the suite's 5 warnings), but a latent foot-gun: if anyone tightens `filterwarnings` in `pyproject.toml` to treat PytestWarning as error (a common hardening step), these 3 currently-green tests instantly fail. Also dilutes warning hygiene.
- **Root cause:** Module-level `pytestmark = pytest.mark.asyncio` over-applies the async marker to the sync helper tests in the same file.
- **Suggested fix:** Remove the module-level `pytestmark`; rely on `asyncio_mode=auto` (which already handles the `async def` tests without any marker). The async tests need no decorator under auto mode.

### CR-T07 — App code uses deprecated `HTTP_422_UNPROCESSABLE_ENTITY` (surfaces as a test-time DeprecationWarning x5)
- **Severity: dette (P2)**
- **Evidence:** `app/api/ai.py` lines 193, 228, 272, 279, 319 use `status.HTTP_422_UNPROCESSABLE_ENTITY`. Starlette/FastAPI emit `DeprecationWarning: 'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated. Use 'HTTP_422_UNPROCESSABLE_CONTENT' instead.` (captured from `fastapi/routing.py:328` during the test run). This is the deprecation warning in the suite's "5 warnings".
- **Impact:** Forward-compat risk: a future FastAPI/Starlette major may remove the alias and break import-time/response code. It is an app-code smell exposed by tests, not a test bug. Low urgency, easy fix.
- **Root cause:** Constant renamed upstream; app not updated.
- **Suggested fix:** Replace the 5 usages with `status.HTTP_422_UNPROCESSABLE_CONTENT` (or just `422`). (This is an app-code change; flagged here only because the tests are how it is observed.)

### CR-T08 — Master-election / lifespan path (`fcntl.flock`) is entirely untested and explicitly skipped on the dev OS
- **Severity: P2**
- **Evidence:** `tests/conftest.py:52` documents that `api_client` "Skips the master-election lifespan (uses `fcntl` which doesn't exist on Windows...)". Grep for `lifespan`/`flock`/`fcntl`/`elect`/`master` in `tests/` returns only the conftest comment — no test exercises `app/main.py` lifespan, the flock acquisition, single-master scheduler gating, or graceful task shutdown.
- **Impact:** The production-critical master-election (which guarantees only one worker runs the APScheduler pipeline in Docker) has zero automated verification. A regression that lets two masters run the scheduler concurrently (double sync/enrichment/generation) would not be caught. The CI runner is `ubuntu-latest` (`tests.yml:11`) where `fcntl` *is* available, so this is testable in CI even though the dev box is Windows.
- **Root cause:** The path is awkward to test (process-level lock) and was deferred; the Windows dev environment made it easy to skip.
- **Suggested fix:** Add a Linux-only test (skip on Windows via `pytest.mark.skipif`) that opens two flocks on the same file and asserts only the first acquires it; assert the scheduler is only started by the master branch. Run it in the existing ubuntu CI job.

---

## What's solid

- **AI subsystem is genuinely well-tested.** All **3 contractual 503 patterns** are covered both at the dependency layer (`test_ai_deps.py:33-43`) and end-to-end through the router (`test_ai_503_detail.py:135-163` + the model-unavailable path). `rank`/`rank-multi` (centroid + exclude_refs), `HYDRATE_CAP`, cursor-based rebuild pagination (`test_embedding_worker.py:114` asserts no `OFFSET`), FIFO job eviction, and the OpenAPI/migration shape all have dedicated tests.
- **tv-auth is a model integration suite** (`test_tv_auth.py`, 17 tests): full device-flow cycle, TTL expiration, **payload-delivered-exactly-once** and one-shot `complete` semantics, **encryption at rest** + **scrub-after-complete**, approve-auth enforcement, user-code normalization, and migration 009 table/index creation.
- **Good HTTP-mock discipline:** `respx`-based TMDB tests (`test_tmdb_service_mocked.py`) verify caching (incl. cached-`None`) and 429+Retry-After retry against a *real* `TMDBService`, not a mock of itself — testing behavior, not mocks.
- **Real-DB integration where it matters:** `test_db_layer.py` and the AI tests run against actual in-memory / file-backed SQLite (incl. sqlite-vec via `conftest_ai.py`), and `test_storage_atomic.py` verifies atomic writes — these are not over-mocked.
- **Strong pure-function coverage:** `string_normalizer` (43 tests), `plex_generator` (51), `strip_titles_pollution` (18), `db_retry` (7), `ttl_cache` (8) are thoroughly exercised with edge cases.
- **Test isolation is sound:** per-test engines/tmp dirs, `app.dependency_overrides` for `get_db`, `monkeypatch` of `async_session_factory`, and `pyproject.toml` `asyncio_mode=auto` are used consistently — no cross-test DB bleed observed; the full run is fast (~10 s) and deterministic.
