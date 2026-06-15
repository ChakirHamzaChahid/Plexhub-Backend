# CR — Performance & Latency

**Dimension score: 68 / 100**

Rationale: hot paths are async-correct, indexed for the common case, and blocking work is offloaded — but the media-list endpoint pays a self-inflicted ~18–20ms penalty on a low-selectivity COUNT because 4 compound indexes declared in the model were never actually created in the live DB, and `title_*`/deep-OFFSET pagination degrades to a TEMP B-TREE / full re-scan (325ms measured at OFFSET 10000).

## Where the time goes (component model, measured against the real 173.8 MB / 102,721-row DB)

The measured app-handler `GET /api/media/movies?limit=50` median of **28.9ms** decomposes (raw-SQLite timing, ORM/serialization on top):

| Component | Measured (raw sqlite) | Notes |
|---|---|---|
| COUNT subquery (`type=movie AND visible`) | **~18–19ms** | dominant cost; uses low-selectivity `ix_media_category_visible` |
| Fetch 50 rows `ORDER BY added_at DESC` | **~0.3ms** | well-indexed (`ix_media_type_added`), cheap |
| ORM hydration + 50× `MediaResponse.model_validate` + JSON | **~9–10ms** | the gap between 19ms SQL and 29ms handler |
| ilike `%star%` search variant | +~10ms | COUNT(ilike) ~20.6ms + fetch ~4.5ms; full scan, no FTS |

So roughly **two-thirds of the media-list latency is the COUNT query**, and the other third is Pydantic v2 row validation. The fetch itself is negligible. Trivial endpoints (health 3.7ms, accounts 1.6ms, live 2.5ms — 0 live rows) confirm framework overhead is ~2–4ms; everything above that on media is the COUNT + validation.

---

## Findings

### CR-P01 — 4 compound indexes declared in the model are MISSING from the live DB (no migration creates them)
- **Severity: P1**
- **Evidence:** `app/models/database.py:102-104` declares `Index("ix_media_server_type", "server_id", "type")`, `ix_media_server_visible`, `ix_media_parent_visible`, and `app/models/database.py:105` `ix_media_grandparent`. Verified against the running 173.8 MB `data/plexhub.db`: `SELECT name FROM sqlite_master WHERE tbl_name='media'` returns **none** of these four (present: `ix_media_type_added`, `ix_media_type_rating`, `ix_media_title_sort`, `ix_media_category_visible`, … — the four compound ones are absent). `app/db/migrations.py` (chain 001→009) contains **no** `CREATE INDEX` for them.
- **Impact:** `Base.metadata.create_all` (`app/db/database.py:86`) only emits `CREATE INDEX` when it creates the table. On a DB whose `media` table already existed before these `__table_args__` were added, the new indexes are silently never created — `create_all` does not diff/alter. Result: the COUNT for `GET /api/media/movies` falls back to `ix_media_category_visible` (a boolean column where ~all rows = visible → near-full index scan), measured **~18–19ms**, i.e. ~65% of the 28.9ms handler time. `ix_media_grandparent` absence also slows episode-by-series queries (`media_service.get_media_list` line 66) and Plex generation episode grouping.
- **Root cause:** Reliance on `create_all` for schema evolution instead of explicit idempotent migrations; index additions to `__table_args__` are dead on any non-fresh DB.
- **Suggested fix:** Add `_migration_010_add_media_compound_indexes` at the **end** of `run_migrations()` with `CREATE INDEX IF NOT EXISTS` for all four. Critically, replace `ix_media_category_visible` reliance with a compound `(type, is_in_allowed_categories)` index so the COUNT can be index-only:
  ```sql
  CREATE INDEX IF NOT EXISTS ix_media_type_visible ON media(type, is_in_allowed_categories);
  CREATE INDEX IF NOT EXISTS ix_media_server_type ON media(server_id, type);
  CREATE INDEX IF NOT EXISTS ix_media_grandparent ON media(grandparent_rating_key);
  ```
  Expected: COUNT drops from ~18ms toward ~1–3ms; media-list handler ~29ms → ~12–14ms.

### CR-P02 — Every media-list request runs a separate COUNT scan (double query, COUNT dominates)
- **Severity: P1**
- **Evidence:** `app/services/media_service.py:72-75` builds `count_query = select(func.count()).select_from(query.subquery())` and executes it before the page fetch on every call. Measured COUNT alone = ~18–19ms vs page fetch ~0.3ms.
- **Impact:** The COUNT is the single largest contributor to the 28.9ms (movies) / 28.0ms (shows) medians. It re-scans the filtered set on every page request, even for deep pages where `total` rarely changes. With `ix_media_category_visible` as the only usable index (CR-P01), it cannot be index-only.
- **Root cause:** Unconditional exact-count on a 102k-row table to populate `total`/`has_more`.
- **Suggested fix:** (1) Fix indexes (CR-P01) so COUNT is index-only. (2) Optionally make exact COUNT opt-in (`with_count` query param) and derive `has_more` from fetching `limit+1` rows when the caller doesn't need an exact total — removes the COUNT scan entirely for paging-only clients.

### CR-P03 — Title/rating/year/deep-OFFSET sorts fall back to TEMP B-TREE + full re-scan (325ms at OFFSET 10000)
- **Severity: P1**
- **Evidence:** `app/services/media_service.py:82-91` supports `title_asc/desc`, `rating_desc`, `year_desc`. `EXPLAIN QUERY PLAN` for `type='movie' … ORDER BY title_sortable LIMIT 50 OFFSET 10000` shows `SEARCH media USING INDEX ix_media_category_visible` + **`USE TEMP B-TREE FOR ORDER BY`**. Measured: OFFSET 0 = **19.2ms**, OFFSET 10000 = **325.4ms** (raw sqlite). `ix_media_title_sort` is a single-column index on `title_sortable` but the query filters on `type`/`is_in_allowed_categories` first, so the planner cannot use it for ordering.
- **Impact:** Sort-by-title on movies (20,475 rows) costs ~17× a default page even at OFFSET 0, and deep pagination is O(offset) — 325ms per page near the tail. `rating_desc`/`year_desc` have the same shape (no compound index leading with `type`).
- **Root cause:** No compound index `(type, is_in_allowed_categories, <sort_col>)`; OFFSET pagination re-walks all skipped rows.
- **Suggested fix:** Add compound indexes leading with the filter then the sort column: `ix_media_type_visible_title (type, is_in_allowed_categories, title_sortable)`, similarly for `display_rating` and `year`. For deep pagination, move to keyset/seek pagination (`WHERE title_sortable > :last ORDER BY title_sortable LIMIT n`) instead of OFFSET — the client already has a stable sort key.

### CR-P04 — ilike `%term%` search is a full scan with no FTS
- **Severity: P2**
- **Evidence:** `app/services/media_service.py:46-48` `Media.title.ilike(f"%{safe_search}%")`; genre line 51 same. `EXPLAIN` for the search query still uses `ix_media_type_added (type=?)` then filters titles linearly. Measured `?search=star` handler = **39.1ms** (median) vs 28.9ms baseline; raw COUNT(ilike) = 20.6ms, fetch+ilike = 4.5ms.
- **Impact:** +~10ms per search request today; scales linearly with movie count. The leading `%` wildcard makes any B-tree index on `title` useless, so this only worsens as catalogs grow.
- **Root cause:** Substring search via `LIKE '%…%'` on a non-FTS column.
- **Suggested fix:** Add an SQLite FTS5 virtual table over `title` (+ optionally `genres`, `cast`) kept in sync on upsert, and route `search` through `MATCH`. Quantified expectation: substring/prefix match drops from ~25ms (count+fetch) to low single-digit ms and stays flat with catalog growth. If FTS is too heavy, at minimum make the COUNT index-only (CR-P01) so the search COUNT isn't a second full scan.

### CR-P05 — Pydantic v2 row validation is ~1/3 of media-list latency
- **Severity: P2**
- **Evidence:** `app/api/media.py:37,68,87` `items=[MediaResponse.model_validate(i) for i in items]` — 50 model_validate calls per page over a 40+ column entity (`app/models/database.py:11-106`). Gap between raw-SQL 19ms and 29ms handler ≈ 9–10ms is dominated by ORM hydration + per-row validation + JSON encoding.
- **Impact:** ~9–10ms fixed cost per page regardless of query tuning; grows linearly with `limit` (max 5000 → multi-hundred-ms responses).
- **Root cause:** Full ORM object hydration + re-validation of trusted DB rows into Pydantic on the hot path.
- **Suggested fix:** Use `MediaResponse.model_construct(**row)` (skip re-validation for trusted DB data) or select only the columns the response needs (the entity has many columns the list view doesn't return). For large `limit`, consider `ORJSONResponse`. Lower-risk: cap the default `limit` (currently 500) — most clients page at 50.

### CR-P06 — `/embed/status` issues 4 sequential aggregate queries on the request path
- **Severity: dette**
- **Evidence:** `app/api/ai.py:395-406` runs `COUNT(*) ai_embeddings`, `COUNT(*) ai_tmdb_cache`, `COUNT(*) WHERE embedded_at IS NULL`, `MAX(embedded_at)` as four separate `await db.execute(...)` calls, plus `psutil.Process(...).memory_info()` (line 408). Measured 3.3ms today only because the AI caches are **empty (0 rows)**.
- **Impact:** Benign now; with a populated `ai_tmdb_cache` (post-rebuild) the `embedded_at IS NULL` count and `MAX` will scan/index-walk. `ix_ai_tmdb_cache_embedded_at` exists (`migrations.py:300`) so it stays cheap, but the 4-round-trip pattern is wasteful.
- **Root cause:** One round-trip per metric.
- **Suggested fix:** Collapse into a single query: `SELECT (SELECT COUNT(*) FROM ai_embeddings), COUNT(*), SUM(embedded_at IS NULL), MAX(embedded_at) FROM ai_tmdb_cache`.

### CR-P07 — AI cold start ~30s blocks the first `/rank` caller (singleton load)
- **Severity: P2**
- **Evidence:** `app/services/embedding_service.py:65-82` lazy singleton; `_load_model_blocking` (line 50) downloads + loads ONNX weights inside `asyncio.to_thread` (good — doesn't block the loop) but is guarded by `_MODEL_LOCK` (line 74). First `/rank`/`/rank-multi` awaits the full ~30s load before responding; concurrent callers serialize on the lock.
- **Impact:** First AI request after boot (or after any model-cache miss in a fresh container — `AI_EMBED_CACHE_DIR` empty by default = ephemeral, `config.py:31`) sees ~30s latency. Documented but unmitigated; in a container without a persistent cache dir this recurs on every restart and re-downloads the model.
- **Root cause:** Lazy load + no warm-up + ephemeral default cache.
- **Suggested fix:** Optional non-blocking warm-up task at master boot (fire-and-forget `embed_query("warmup")` so the 30s happens before first user). Mandate `AI_EMBED_CACHE_DIR` to a persistent volume in `docker-compose.yml` so the model is downloaded once, not per restart.

### CR-P08 — `/rank` hydrate does N parallel TMDB round-trips + N separate DB sessions/commits on the request path
- **Severity: P2**
- **Evidence:** `app/services/recommendation_service.py:155-201` `hydrate_misses` fan-out up to `HYDRATE_CAP=20` (line 28) `_fetch_and_store_one`, each: a TMDB HTTP call (`tmdb_service.get_movie_details`, line 94) + embedding + its **own** `async_session()` with 3 statements and a `commit()` (lines 120-150), `HYDRATE_PER_TASK_TIMEOUT_S=10` (line 29).
- **Impact:** On a cold cache, a single `/rank` can block up to ~10s (per-task timeout) and open 20 concurrent write sessions doing DELETE+INSERT on the `vec0` virtual table. Against SQLite (single writer) these 20 commits serialize on the write lock; `commit_with_retry` is **not** used here (raw `session.commit()`), so a lock contention surfaces as an unhandled error rather than a retry. The cap+timeout is a sane guard, but synchronous hydration on the user request is inherently slow on cold cache (mitigated by the documented "client re-calls once cache is warm").
- **Root cause:** Cache-miss hydration is inline on the request rather than pre-warmed.
- **Suggested fix:** (1) Wrap the hydrate commits in `commit_with_retry` for the SQLite write-lock path. (2) Encourage pre-warming via `POST /embed/rebuild` so `/rank` is a pure cache read. (3) Consider a single batched write session for the hydrated batch instead of 20 independent sessions.

### CR-P09 — sqlite-vec is loaded but cosine ranking is done in Python (no ANN), full candidate materialization
- **Severity: dette**
- **Evidence:** `app/services/recommendation_service.py:59-74` `load_cached_vectors` does `SELECT … FROM ai_embeddings WHERE tmdb_id IN (…)` and deserializes every blob into Python lists; `cosine_rank` (line 208-233) loops candidates computing `np.dot` one at a time. The `vec0` table's KNN/`MATCH` capability is not used — ranking is bounded by the client-supplied candidate list (≤200 via `limit` validation, plus refs).
- **Impact:** Acceptable at current scale (candidate lists are small, client-supplied) but the per-candidate Python loop and per-row blob deserialization don't scale to "rank against the whole catalog". No measured regression today (AI caches empty).
- **Root cause:** Design ranks a client-provided candidate set, not a global KNN — so sqlite-vec's index is unused.
- **Suggested fix:** If global recommendation is ever needed, use sqlite-vec `MATCH`/KNN. For the current candidate-set design, vectorize cosine: stack candidate vectors into one `(N,384)` numpy matrix and do a single `matrix @ q` dot product instead of the per-item loop.

### CR-P10 — `mmap_size=256MB` < DB size (173.8MB now, growing); cache_size 64MB fine, but WAL checkpoint cadence unmanaged
- **Severity: dette**
- **Evidence:** `app/db/database.py:80-85` sets WAL, `synchronous=NORMAL`, `cache_size=-64000` (64MB), `temp_store=MEMORY`, `busy_timeout=5000`, `mmap_size=268435456` (256MB). DB is 173.8MB today; mmap covers it. No `wal_autocheckpoint` tuning and the deep-OFFSET sort (CR-P03) uses `temp_store=MEMORY` which is good but large `limit` temp B-trees consume RAM.
- **Impact:** Once the DB exceeds 256MB (likely as episodes/EPG grow), reads past the mmap window fall back to read() syscalls — gradual read-latency creep. Not a problem at current size; flagged for headroom.
- **Root cause:** Static mmap_size set below expected growth.
- **Suggested fix:** Raise `mmap_size` to e.g. 512MB–1GB (cheap; just address space), and set an explicit `PRAGMA wal_autocheckpoint` aligned with the write-heavy sync window. `synchronous=NORMAL`+WAL is the correct durability/throughput tradeoff — keep it.

---

## What's solid

- **Event loop is not blocked on the hot path.** ONNX init + inference are offloaded via `asyncio.to_thread` (`embedding_service.py:78,124,130`); `sqlite3.backup` runs in a thread (per CLAUDE map, master cron); image downloads use a dedicated `ThreadPoolExecutor` (`plex_generator/storage.py`). No synchronous I/O on request handlers found.
- **Default media page fetch is correctly indexed.** `ix_media_type_added` serves `ORDER BY added_at DESC` with **no** TEMP B-TREE (EXPLAIN confirms `SEARCH … USING INDEX ix_media_type_added`); the 50-row fetch is ~0.3ms.
- **Plex generation streams rows** with `db.stream(...).execution_options(yield_per=1000)` (`plex_generator/source.py:53-55,90-108`) instead of materializing 100k rows — episodes grouped in a single streaming pass, bounded memory.
- **Embedding rebuild is cursor-paginated** (`tmdb_id > :cursor`, `PAGE_SIZE=50`, `embedding_worker.py:67-80`) — O(1) memory regardless of queue size, no OFFSET drift.
- **httpx client is reused** with sane pool limits (`tmdb_service.py:65-76`: `max_connections=50`, `max_keepalive_connections=35`, `keepalive_expiry=30`, `timeout=10s`) and exponential-backoff retry + 429 `Retry-After` handling (lines 106-138). Search and imdb→tmdb lookups are TTL-cached (24h / 7d, bounded 5000 entries).
- **Sync/enrichment avoid per-row DB chatter:** upserts and cleanups are chunked (`for i in range(0, len, 200)` batches, `sync_worker.py:398,577,612,837`); enrichment fans out TMDB HTTP in parallel (`Semaphore(8)`) with sequential DB writes — no DB-level N+1. Series-episode fetch is one Xtream call per *changed* series (incremental by `dto_hash`), batched 50-concurrent — inherent to the Xtream API, not a code defect.
- **Concurrency knobs are reasonable** for the 2GB/single-node target: stream validation 20, enrichment 8, image pool 8. The AI `HYDRATE_CAP=20` + 10s/task timeout correctly bounds worst-case `/rank` latency.

---

## Summary (return)

- **Dimension score: 68 / 100**
- **Findings by severity:** P0 = 0 · P1 = 3 (CR-P01, CR-P02, CR-P03) · P2 = 4 (CR-P04, CR-P05, CR-P07, CR-P08) · dette = 3 (CR-P06, CR-P09, CR-P10) — **10 total**
- **Top 3:**
  1. **CR-P01** — 4 compound indexes declared in `models/database.py:102-105` are MISSING from the live 173.8MB DB (no migration creates them; `create_all` doesn't alter existing tables) → COUNT falls to low-selectivity `ix_media_category_visible`, ~18ms of the 29ms.
  2. **CR-P02** — every media-list call runs a separate full COUNT scan (`media_service.py:73`) that dominates latency and is the second query on a double-scan path.
  3. **CR-P03** — title/rating/year and deep-OFFSET pagination degrade to TEMP B-TREE + O(offset) re-scan: measured **325ms** at OFFSET 10000 vs 19ms at OFFSET 0; no compound filter+sort index, OFFSET-based paging.
