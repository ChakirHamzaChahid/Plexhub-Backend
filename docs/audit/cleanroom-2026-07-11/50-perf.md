# Clean-room audit — Performance / Latency / Scalability

**Verdict:** 2.5 / 5

**Scope note on the numbers.** The live latencies handed to me (`/api/health` 3.5 ms, `/api/media/movies?limit=500` 2.7 ms, `/api/media/movies/unified` 2.2 ms, `/api/live/channels` 2.2 ms, `init_db` 74 ms) were measured **in-process against an EMPTY temp DB**. They are a *floor*, not a load profile: an empty table means zero rows to scan, hydrate, aggregate or serialize. Every finding below is graded by how the path **scales with row count** (tens of thousands of media rows, thousands of channels), not by the floor numbers.

Summary: the single-item and index-friendly list paths are fine, and the batch workers (sync, enrichment, validation) are well-batched with parallel I/O and chunked commits. The problem is concentrated in the **flagship deduped catalog endpoints** (`/movies/unified`, `/shows/unified`), which load the *entire* category-allowed catalog into memory and run a Python aggregation **on the event loop** for every request — pagination gives zero relief. A second, latent risk is index provisioning: most ORM-declared `media` indexes are created only by `create_all` on a fresh DB and are never re-ensured by a migration, so a long-lived/upgraded DB silently loses them. Search paths do double full scans via leading-wildcard `ILIKE` + COUNT-over-subquery.

---

### CR-P01 — `/movies/unified` & `/shows/unified` load the WHOLE catalog and aggregate on the event loop, per request (P0)

**Where.** `app/services/media_service.py:128-147` (`get_unified_list`) → `app/services/aggregation_service.py:215-221` (`aggregate_movies`) → `app/api/media.py:150-214` / `:217-275`.

**What.** `get_unified_list` builds `select(Media).where(Media.type == …, Media.is_in_allowed_categories == True [+ optional search/genre/year])` with **no `LIMIT`/`OFFSET` at the DB level**, then materializes *every* matching row:
```
rows = list((await db.execute(query)).scalars().all())   # media_service.py:143
groups = aggregate_movies(rows)                            # :144  full in-memory grouping
groups.sort(key=lambda g: (g.best.added_at or 0), ...)     # :145  sort ALL groups
total = len(groups)                                        # :146
return groups[offset:offset + limit], total                # :147  page sliced AFTER the fact
```
`aggregate_movies` then runs, synchronously and on the request's event-loop turn: a dict build (O(n)), `_converge` = union-find `_merge_by_shared_ids` iterating every row's id-tokens (`aggregation_service.py:130-175`) + `_absorb_title_groups` calling `best_row` and `calculate_unification_id` per group (`:178-203`), and a final `best_row` per group (`:221`). None of this is offloaded via `asyncio.to_thread`.

These are the **primary endpoints the Android app uses** to browse the deduped library. The `unification_id`-filtered branch (`get_unified_group`, `media_service.py:149-179`, indexed by `ix_media_unification`) is efficient — the pathology is only the **list/pagination** branch.

**Scale impact.** At tens of thousands of movie rows across N accounts:
- **Full row load every call.** SQLite scan/fetch of all matching rows + SQLAlchemy ORM hydration of every `Media` object (~60 columns each). ORM hydration alone is on the order of tens of µs per row → a 50 k-row catalog is a **hundreds-of-ms to multi-second event-loop stall** *per request*, blocking all other in-flight requests on the single-threaded async loop.
- **Pagination buys nothing.** `limit`/`offset` are applied *after* the full load + grouping + sort (`media_service.py:147`), so page 50 costs exactly as much as page 1. There is **no caching** — every request re-loads and re-aggregates from scratch.
- **Memory O(catalog) per in-flight request.** Concurrent calls multiply the resident set against the 2 GB container cap (`docker-compose.yml`).

**Fix direction.** Precompute the unification grouping into a denormalized "group" table (or a cache) refreshed at sync/enrichment time, so the endpoint pages over already-grouped rows with a DB `LIMIT`. Short term: memoize the aggregated result with a TTL keyed by a catalog-version stamp (max `updated_at` / row count) so repeated pages and concurrent callers reuse one aggregation; and offload the CPU grouping to `asyncio.to_thread` if it must stay per-request. `unification_id` is already indexed and persisted — a DB-side `GROUP BY unification_id` with a windowed page eliminates the full materialization for the common (no-convergence-needed) case.

---

### CR-P02 — ORM-declared `media` indexes are created only by `create_all` on a fresh DB, never re-ensured by a migration (P1, latent)

**Statut : RÉSOLU (2026-07-11, cleanroom-fixer).** Migration **015** ajoutée en fin de chaîne (`app/db/migrations.py`, fonction `_migration_015_add_missing_media_indexes`, appelée dans `run_migrations()` après la 014) : crée en idempotent (`CREATE INDEX IF NOT EXISTS`, un statement par index, chacun dans sa propre transaction/try pour qu'un échec n'en bloque pas d'autres) les 16 index déclarés sur l'ORM `Media.__table_args__` qui n'étaient créés par AUCUNE migration existante — `ix_media_guid`, `ix_media_type_added`, `ix_media_imdb`, `ix_media_tmdb`, `ix_media_server_lib`, `ix_media_unification`, `ix_media_type_rating`, `ix_media_parent`, `ix_media_title_sort`, `ix_media_broken`, `ix_media_updated`, `ix_media_server_type`, `ix_media_server_visible`, `ix_media_parent_visible`, `ix_media_grandparent`, `uix_media_pagination` (unique — isolée dans sa propre transaction : sur une DB avec des doublons de pagination préexistants, seul cet index serait sauté avec un warning, sans bloquer les autres). `ix_media_category_visible`/`ix_media_adult`/`ix_media_tvdb` restent gérés par les migrations 003/013/014 (déjà OK, hors périmètre). Additif pur, aucune colonne/donnée touchée. Preuve : `tests/test_media_indexes_migration.py` (4 tests — création complète sur une table "DB longue durée" simulée sans les index ORM, idempotence par double run, correspondance exacte nom+colonnes+flag unique via `PRAGMA index_info`/`index_list`, et tolérance à des lignes de pagination dupliquées préexistantes) + vérification manuelle : chaîne `run_migrations()` exécutée deux fois sur une DB fraîche (`create_all` + migrations) → 21 index sur `media` (19 ORM + `ix_media_stream_validation`/007 + l'auto-index de la PK), aucune erreur, chaîne désormais **001→015**. `pytest tests/test_media_indexes_migration.py tests/test_api_health.py tests/test_ai_migration.py` : 10 passed.

**Where.** `app/db/database.py:92` (`Base.metadata.create_all`), `app/models/database.py:104-125` (index declarations), `app/db/migrations.py:20-36` (migration chain).

**What.** `init_db` provisions schema in two ways: (1) `Base.metadata.create_all`, and (2) `run_migrations`. `create_all` runs with `checkfirst=True` semantics — if the `media` table **already exists**, SQLAlchemy skips its `CREATE TABLE` DDL block, and the `CREATE INDEX` statements for that table are emitted *inside* that skipped block, so **indexes are not added to a pre-existing table**. The migrations only create this subset of `media` indexes: `ix_media_category_visible` (`migrations.py:101-104`), `ix_media_stream_validation` (`:229-232`, note: not even declared on the ORM), `ix_media_adult` (`:405-408`), `ix_media_tvdb` (`:459-461`).

Every *other* ORM-declared `media` index — `ix_media_type_added`, `ix_media_type_rating`, `ix_media_title_sort`, `ix_media_updated`, `ix_media_server_type`, `ix_media_server_visible`, `ix_media_parent_visible`, `ix_media_grandparent`, `ix_media_imdb`, `ix_media_tmdb`, `ix_media_unification`, `ix_media_server_lib`, `ix_media_broken`, `ix_media_guid`, `uix_media_pagination` (`models/database.py:105-124`) — exists **only** if `create_all` created the table fresh. On a DB whose `media` table predates the addition of these declarations, they are silently absent and nothing ever back-fills them.

**Scale impact.** On an upgraded/long-lived DB (the realistic production case), the hot list queries lose their supporting indexes:
- `get_media_list` `ORDER BY added_at`/`title_sortable`/`display_rating`/`year` (`media_service.py:82-95`) → **full scan + filesort** without `ix_media_type_added` / `ix_media_type_rating` / `ix_media_title_sort`.
- `/episodes` filter on `grandparent_rating_key` (`media_service.py:70`) → full scan without `ix_media_grandparent`.
- The COUNT and the `is_in_allowed_categories` / `unification_id` / `imdb_id` / `tmdb_id` filters degrade similarly.

Because the measured floor uses a **fresh** empty DB, `create_all` created every index there — which is exactly why this risk is invisible in the floor numbers and only bites after schema evolution.

**Fix direction.** Add one idempotent migration at the end of the chain that issues `CREATE INDEX IF NOT EXISTS` for **every** ORM-declared index (single source of truth), instead of relying on `create_all` for index provisioning. Verify against the live DB with `SELECT name FROM sqlite_master WHERE type='index'` vs the ORM set.

---

### CR-P03 — Search path does double full-table scans: leading-wildcard `ILIKE '%term%'` + COUNT-over-subquery (P1)

**Where.** `app/services/media_service.py:52` & `:55` (title/genre `ILIKE`), `:77-79` (count); `app/api/live.py:63` (name `ILIKE`), `:66-67` (count); also `get_unified_list` search at `media_service.py:132-137`.

**What.** Search filters use `Media.title.ilike(f"%{safe}%")` / `LiveChannel.name.ilike(f"%{safe}%")`. A **leading** `%` makes the predicate non-sargable — no B-tree index can be used, so it is always a full scan. The total is then computed as `select(func.count()).select_from(query.subquery())` where the subquery is `SELECT media.* FROM media WHERE …` — i.e. it wraps a **full-column** select just to count it, and (with the wildcard search in the WHERE) requires a **second** full scan.

**Scale impact.** Every search request = **two full scans** of a tens-of-thousands-row table (one for COUNT, one for the page fetch), on the event loop, followed by Pydantic validation of up to `limit` rows (default 500). Throughput collapses under concurrent search traffic.

**Fix direction.** Use an FTS5 virtual table (or trigram index) for title/name search. For the count, use `select(func.count()).select_from(Media).where(*filters)` (narrow, no full-column subquery), or drop the exact total entirely and derive `has_more` by fetching `limit + 1` rows — that removes the count scan on every list/search call.

---

### CR-P04 — OFFSET-based deep pagination scans and discards the skipped prefix (P2)

**Where.** `app/api/media.py` list endpoints via `media_service.py:98` (`query.offset(offset).limit(limit)`); `app/api/live.py:81`; `app/api/live.py:268` (EPG).

**What.** SQLite `OFFSET n` walks and throws away the first `n` matching rows before returning the page — cost is O(offset). With `limit` defaulting to 500 (`media.py:76`, `live.py:43`), deep offsets on a large catalog get progressively slower.

**Scale impact.** Early pages are cheap; the tail of a tens-of-thousands-row list degrades linearly with page depth. Moderate, but compounds with CR-P03 when search + deep paging combine.

**Fix direction.** Keyset (seek) pagination on the sort key (`added_at`, `title_sortable`) using a cursor (`WHERE added_at < :last_seen ORDER BY added_at DESC LIMIT n`) instead of `OFFSET`.

---

### CR-P05 — Plex generator & pipeline validation materialize the entire catalog in memory (P2, background paths)

**Statut : PARTIELLEMENT RÉSOLU (2026-07-11, cleanroom-fixer/plex-generator-specialist).** `app/workers/health_check_worker.py::_run_pipeline_validation_impl` ne matérialise plus l'intégralité du candidate set multi-comptes en un seul `select(Media)...execute().scalars().all()`. Remplacé par : (1) un `SELECT count(*)` étroit (pour le log, sans hydratation ORM), (2) une projection `SELECT DISTINCT server_id` (sans hydratation des lignes `Media`) pour connaître les comptes concernés, puis (3) **un `db.stream(...execution_options(yield_per=1000))` par compte**, dans la boucle de traitement déjà séquentielle compte-par-compte — le pic mémoire est désormais borné au plus gros batch d'**un seul** compte, pas à la somme de tous les comptes. Comportement fonctionnel identique : mêmes compteurs (`total_checked`/`total_broken`/`total_recovered`/`diag_reasons`), même isolation du circuit breaker par compte (rollback d'un compte n'affecte pas les autres), mêmes commits par lot de 200. Preuve : `tests/test_health_check_worker.py::test_pipeline_validation_streams_per_account_not_whole_catalog` (nouveau — espionne `AsyncSession.stream`, assert exactement 1 appel par compte distinct, jamais un seul appel couvrant tous les comptes) + les 5 tests circuit-breaker existants + les 3 tests `test_health_check_concurrency.py` toujours verts (27 + 93 = 120 tests ciblés passés, cf. commande ci-dessous).
`app/plex_generator/source.py` (`DatabaseSource.get_movies`/`get_series`) reste **contraint par conception (non modifié)** : le fetch DB streame déjà via `db.stream(execution_options(yield_per=1000))` (`source.py:96,163,171` après ajout des commentaires) — c'est la partie « streaming » réalisable sans risque. Le résultat streamé doit toujours être matérialisé en liste Python (`rows = [row async for row in result.scalars()]`) car `aggregate_movies`/`aggregate_series`/`_converge` sont des opérations **sur l'ensemble complet** (regroupement par `unification_id`, convergence par id partagé, appariement d'épisodes par `(saison, épisode)` à travers les comptes) — un chunking casserait la dédup et produirait des groupes scindés/faux-fusionnés dans la bibliothèque générée. Documenté en commentaire in-code (`source.py:96-106`, `:151-158`) plutôt que refactoré, conformément à la clause STOP-and-propose (risque de casser la génération Plex/Jellyfin > gain mémoire).

**Where.** `app/plex_generator/source.py:96-97`, `:142-158` (`DatabaseSource.get_movies`/`get_series`, résiduel/by-design) ; `app/workers/health_check_worker.py:420-520` (`_run_pipeline_validation_impl`, **résolu**).

**What (historique).** Le générateur *streamait* déjà depuis la DB avec `db.stream(...execution_options(yield_per=1000))` (source.py:96,147,157) — bon — mais collectait tout dans une liste Python (`rows = [row async for row in result.scalars()]`) puis agrégeait en mémoire (O(n)). `run_pipeline_validation` sélectionnait **tous** les flux non vérifiés/périmés sans `LIMIT` (`health_check_worker.py:379-392`) dans une liste avant traitement.

**Scale impact (historique).** Ces tâches tournent dans le process master (scheduler). À des centaines de milliers de lignes épisode/film, c'était un pic mémoire transitoire important face au plafond 2 Go et un passage CPU O(n). Sévérité moindre que CR-P01 car tâches background/cron hors chemin de requête, et la boucle de validation committait déjà par lots de 200.

**Fix direction (réalisé pour health_check_worker, résiduel pour source.py).** Cap/stream du candidate set de validation (traitement par lots bornés avec `LIMIT`/curseur DB) — **fait**. Pour le générateur, le regroupement en mémoire est inhérent à la dédup ; si le catalogue grossit fortement, un chunking par compte ou par préfixe de titre resterait à explorer dans un futur refacto plus large (hors périmètre P2 actuel, risque de régression sur la convergence d'identité).

---

### CR-P06 — `ORDER BY random()` scans + sorts the whole candidate set for the health-check sample (P2)

**Where.** `app/workers/health_check_worker.py:263` (`.order_by(func.random()).limit(batch_size)`).

**What.** To draw a random batch, SQLite assigns `random()` to **every** candidate row (movies + episodes not checked in 7 days) and sorts them to take the top `batch_size`. That is a full scan + full sort of the candidate set each cron run.

**Scale impact.** O(n log n) per cron on a large catalog, entirely to select a small sample. Background path, so P2.

**Fix direction.** Sample by random `rowid` ranges (`WHERE rowid >= (abs(random()) % max_rowid) … LIMIT n`) or keep a shuffled cursor of ids to walk.

---

### CR-P07 — Large-page serialization: up to 500 (media) / 2000 (unified) / 5000 rows × ~60-field Pydantic validation + FastAPI response_model re-pass (P2 / debt)

**Where.** `app/api/media.py:94`, `:125`, `:144` (`[MediaResponse.model_validate(i) for i in items]`), `:199-211`, `:262-272` (unified item construction); `response_model=…` on the routes triggers FastAPI's serialization pass.

**What.** Each list response builds Pydantic models for every row (default 500, max `le=5000` for raw lists, `le=2000` for unified) and FastAPI then re-serializes them against `response_model`. Pydantic v2 is Rust-backed and fast, but at `limit=5000` this is ~300 k field operations per request, twice (construct + response serialization), all on the event loop.

**Scale impact.** Moderate CPU/latency on the largest pages; compounds with CR-P01 on the unified endpoints (where the whole catalog is already loaded).

**Fix direction.** Cap the max page more aggressively for full-object responses; consider `model_construct` for trusted internal rows and/or returning the model instances directly with `response_model=None` to avoid the double pass.

---

### CR-P08 — Vector KNN over-fetch factor may under-return under a skewed type mix (debt)

**Where.** `app/services/recommendation_service.py:239-241`, `:297-299` (`knn_k = min(limit * 4, 200)` when `media_type` is set), post-KNN type filter at `:271-273` / `:325-327`.

**What.** The vec0 table has no `media_type` column, so search over-fetches `limit*4` (capped 200) neighbors then filters by joining `ai_tmdb_cache`. If the nearest-neighbor cloud is dominated by the *other* media type, the filtered result can fall below `limit` — silently returning fewer rows. The KNN itself is efficient (native `MATCH … AND k=` index scan, bounded IN-join for metadata), so this is a correctness-at-scale nuance, not a latency issue.

**Fix direction.** Store `media_type` alongside the vector (partition/prefix or a parallel filtered index) so the KNN filters at the index, or make the over-fetch adaptive (re-query with a larger `k` when the filtered count is short).

---

## What's healthy

- **No N+1 in the hot read endpoints.** Version lists are built in pure Python from already-loaded rows (`api/media.py:27-45`, `source.py:58-80`); `account_labels` is a single query (`media_service.py:106-109`); `get_unified_episodes` uses exactly two queries (shows, then episodes) with `IN` clauses (`media_service.py:181-209`) — no per-row DB calls in loops anywhere I read.
- **Embedding inference is correctly offloaded** to `asyncio.to_thread` (`embedding_service.py:124,130`), behind a lazy singleton + `asyncio.Lock` (`:65-82`); the ~30 s cold start is off the measured hot paths.
- **Vector KNN uses the native sqlite-vec index** (`recommendation_service.py:246-252,302-308`) — an index scan with `k=`, not a full-table scan — followed by a bounded (`≤200`) `IN`-join for metadata.
- **Image downloads are offloaded** to a dedicated 8-thread pool with per-thread httpx clients and atomic writes (`storage.py:41-60,137-143,16-32`) — no image I/O on the event loop.
- **Sync worker is well-engineered for throughput**: chunked multi-row `INSERT..ON CONFLICT` (`sync_worker.py:586-617`), incremental skips via `dto_hash` (`:1023-1026`), parallel Xtream fetch under a semaphore (`:1032`), and **batched commits** (every 100/200 rows, `:1069,1091,1183,1305`) — no per-row commits.
- **httpx client reuse** for stream validation: a module-level singleton `AsyncClient` with tuned connection limits (`health_check_worker.py:18-35`), plus per-account concurrency clamped to `max_connections` (`:107-124`).
- **Bounded caches.** `TTLCache` is a size-capped LRU with per-entry TTL (`ttl_cache.py:24-55`) — predictable memory; the persistent TMDB scrape cache lives in SQLite, not RAM.
- **Writer-contention mitigations** are in place: WAL + 64 MB cache + 256 MB mmap + 60 s `busy_timeout` on every pooled connection (`database.py:15,86-91`), an `asyncio.Lock` serializing the two stream-validation writers (`health_check_worker.py:215,235,353`), and `commit_with_retry` around batch writes. The 60 s busy_timeout is a reasonable cushion for the long validation writer's inter-batch gaps rather than a mask for a pathological hot-path lock (validation commits every 200 rows, so the writer lock is released frequently).

---

## Finding count

- **P0:** 1 (CR-P01)
- **P1:** 2 (CR-P02 — **RÉSOLU 2026-07-11**, CR-P03)
- **P2:** 4 (CR-P04, CR-P05 — **PARTIELLEMENT RÉSOLU 2026-07-11** (health_check_worker) / résiduel by-design (source.py), CR-P06, CR-P07)
- **debt:** 1 (CR-P08)
