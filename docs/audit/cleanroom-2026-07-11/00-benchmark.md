# 00 — Benchmark & Measurement

**Date:** 2026-07-11 · **HEAD:** `develop` @ `1879f83` (v1.1.5) · **Host:** Windows 11 (win32), Python 3.13
**Method:** CI-equivalent `pytest` + in-process ASGI latency probe (native `uvicorn` boot is impossible on
this host — see §3).

---

## 1. Test suite (CI-equivalent)

```
pytest -q --deselect tests/test_utilities.py::TestBase64Decode::test_text_that_looks_like_base64_but_decodes_to_garbage
```

**Result: `484 passed, 1 deselected, 6 warnings in 24.36s`** ✅ (green)

- 1 deselected = the single flaky base64-heuristic test that CI also deselects (`.github/workflows/tests.yml`).
- 6 warnings, all benign but real signal for the conventions/tests dimensions:
  - 3× `DeprecationWarning: 'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated` (FastAPI/Starlette) — `tests/test_ai_blurb.py`, `test_ai_rank.py`, `test_ai_rank_multi.py`.
  - 3× `PytestWarning`: `tests/test_embedding_worker.py::{test_make_job_id_format, test_fifo_eviction_at_cap, test_register_job_in_place_update}` are marked `@pytest.mark.asyncio` but are **sync** functions (mis-marked; the mark is a no-op).

## 2. In-process ASGI latency probe

Driven via `httpx.ASGITransport` against `app.main:app` (bypassing the `fcntl` lifespan), against a **fresh
empty temp SQLite DB** created by calling `init_db()` directly (real schema: `create_all` + the 14-migration
chain + sqlite-vec load). `AI_API_KEY` set to a probe value so the `X-API-Key` guard can be exercised.

- `init_db()` cold cost: **73.9 ms** (schema create + 14 migrations + sqlite-vec).
- **sqlite-vec loaded: `true`** (vector search available).

| Endpoint | Auth | Status | median | p90 |
|---|---|---|---|---|
| `GET /api/health` | public | **200** | 3.54 ms | 4.12 ms |
| `GET /api/media/movies?limit=500` | X-API-Key | **200** | 2.68 ms | 3.09 ms |
| `GET /api/media/movies/unified` | X-API-Key | **200** | 2.21 ms | 2.47 ms |
| `GET /api/live/channels` | X-API-Key | **200** | 2.22 ms | 2.75 ms |
| `GET /api/ai/embed/status` | X-API-Key | **200** | 2.95 ms | 3.28 ms |
| `GET /api/health` (wrong key) | public | **200** | 3.47 ms | — |
| `GET /api/media/movies` (no key) | — | **401** | 0.53 ms | — |

### Reading these numbers (important caveats)
- **Floor, not real-world.** The DB is empty (0 rows), so these measure framework + query-plan + serialization
  overhead only. Real latency for `movies` / `movies/unified` scales with row count: both go through
  `media_service` with **in-memory Python aggregation** for the `/unified` variants — the perf dimension must
  judge that path against realistic volumes (the real catalog is thousands of rows), not this floor.
- **No network stack.** ASGITransport is in-process; add real uvicorn + socket overhead for production figures.
- **AI cold start excluded.** The `~30 s` fastembed ONNX first-load is **not** on any hot path measured here
  (`/embed/status` doesn't load the model). It is a documented separate cold-start cost, not folded in.

### Auth model confirmed empirically (fail-closed)
- Public `/api/health` returns **200** regardless of key (correct — monitoring).
- A guarded endpoint (`/api/media/movies`) with **no** `X-API-Key` returns **401** in ~0.5 ms (rejected before
  handler/DB). This confirms `verify_backend_secret` is wired fail-closed on the JSON API — a material change
  vs. the older "catalogue open" model.

## 3. Runtime observations (factual, feed perf/ops + conventions dimensions)

- **`uvicorn app.main:app` cannot boot on native Windows.** The lifespan does `import fcntl`
  (`app/main.py:198`), POSIX-only → `ModuleNotFoundError` at ASGI startup. The module still *imports* cleanly
  (the import is deferred inside the lifespan), which is why the test suite and this probe run on Windows.
  Deployment target is Docker/Linux, so this is a **dev-experience / portability** note, not a prod outage —
  but it means no native-Windows `uvicorn`/`curl` smoke test is possible; the ASGI probe is the substitute.
- **Migrations are no-ops on a fresh DB but log warnings.** `Base.metadata.create_all` already builds the
  columns that migrations **013** (`is_adult`) and **014** (NFO metadata: `imdb_rating`, `imdb_votes`,
  `tmdb_rating`, `tmdb_votes`, `cast_json`, …) then try to `ADD COLUMN`, producing
  `WARNING: Migration 014: <col> may already exist: duplicate column name`. The try/except idempotency guard
  swallows them correctly, but on every fresh boot the log is noisy and the model↔migration duplication is a
  smell (schema truth lives in two places). Feeds the conventions/migrations dimension.

---

*Prior audits under `docs/audit/**` were deliberately NOT consulted (clean-room). Numbers and observations
above come solely from the running code on 2026-07-11.*
