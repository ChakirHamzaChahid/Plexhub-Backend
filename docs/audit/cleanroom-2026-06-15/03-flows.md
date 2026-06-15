# CR — Key Flows End-to-End (§5)

**Dimension score: 72 / 100**
Rationale: the pipeline is genuinely well-engineered for resilience (per-account locks, savepoints, incremental hashing, circuit breaker, atomic writes, one-shot pairing). It loses points for two real correctness gaps (episode cleanup never runs; a scheduler/initial-run unbounded-overlap window), a daily-limit accounting bug that under-counts TMDB calls, and an EPG flow that does not exist in the scheduled pipeline despite being documented.

## Mental model of the pipeline

Master worker (elected via `fcntl.flock`, POSIX only) runs one APScheduler interval job `scheduled_sync_enrich_generate` that chains, **in series**, four stages sharing no transaction:

```
sync_worker.run_all_accounts()        # per-account: categories→VOD→series→episodes→live ; incremental by dto_hash ; differential cleanup ; enqueue_for_enrichment
  → enrichment_worker.run()           # drains enrichment_queue: TMDB search/details, daily-limit budget, attempts<=3
    → health_check_worker.run_pipeline_validation()  # HEAD→Range GET probe, magic bytes, per-account circuit breaker
      → _auto_generate_plex_library() # DatabaseSource → PlexLibraryGenerator → LocalStorage (.strm + NFO + images), idempotent via .plex_mapping.json
```

Plus crons (master): `run()` health-check (h2), `_cleanup_stale_epg` (h3), backup (h4). A non-blocking `initial_sync_then_enrich` fires the **same chain** once at boot via `create_background_task`. AI ranking (`/api/ai/rank*`) is a **separate, synchronous request path** that lazily hydrates the `ai_tmdb_cache`/`ai_embeddings` vector store. TV pairing (`/api/tv-auth/*`) is an independent device-flow state machine over `tv_auth_sessions`.

Runtime confirmation: DB holds 102,721 media (20,475 movie / 4,465 show / 77,781 episode / 18,416 TMDB-enriched), 1 xtream account, `ai_*` tables empty. Logs (`logs/plexhub.log`) show a prior full enrichment run ("Enrichment batch complete: 6750 TMDB API calls used") and active read traffic at 21:20. **Note:** the *currently running* server appears to be an older build — its log shows two distinct interval jobs `run_all_accounts` and `run` at `interval[6:00:00]`, whereas HEAD `3c8beef` has a single combined `scheduled_sync_enrich_generate` job + an `h2` cron. Findings below are judged against the HEAD source, not the running process.

---

## §5.1 SYNC

### CR-F01 — Delisted episodes are never cleaned up (orphan rows accumulate)  · P1
**Evidence:** `app/workers/sync_worker.py:1120-1180` (episode sync) — episodes are upserted but there is **no** `differential_cleanup*` call for `type="episode"`. The three cleanups target only `media_type="movie"` (`:1014,1019`), `media_type="show"` (`:1110,1113`) and live (`:1245,1248`). `grep dto_hash` confirms `map_episode_to_media` (`:309-352`) never sets a `dto_hash`, and episodes are only re-fetched when the **parent series** DTO hash changes (`changed_series_dtos`, `:1121`).
**Impact:** When a provider removes an episode from a series whose top-level series DTO is unchanged, the episode row stays forever. Worse, when episodes *are* re-fetched (series changed) the new set is upserted but the removed ones are not deleted — so an episode that was S01E10 and is delisted, or whose `id`/`container_extension` changed (new `rating_key`), leaves a stale row + a stale `.strm`/NFO downstream in Plex (the generator's mapping cleanup only removes items not in `source.get_series()`, so a dead episode that is still in DB but whose stream URL 404s persists as a broken `.strm`). On a 77,781-episode catalog this is the largest source of silent drift.
**Root cause:** episode incrementality is keyed entirely off the parent-series hash; there is no per-series episode set reconciliation.
**Suggested fix:** after upserting `episode_batch` for a changed series, compute the API episode `rating_key` set for that series and `delete(Media).where(type=="episode", grandparent_rating_key==f"series_{sid}", rating_key.notin_(api_keys))`. Also add a periodic full episode reconciliation for series whose hash is unchanged (e.g. weekly).

### CR-F02 — `differential_cleanup(filter_val="all")` ignores `is_in_allowed_categories` and can mass-delete on a partial API failure  · P1
**Evidence:** `app/workers/sync_worker.py:624-661`. In `filter_mode=="all"`, cleanup selects **all** rows for `server_id`+`media_type` and deletes everything not in `api_rating_keys` (`:644-645`). `api_rating_keys` (`all_vod_keys`/`all_series_keys`) is built from whatever the fetch returned. The VOD/series fetch swallows errors and yields `[]` (`:925-927`, `:1039-1041`), and per-category whitelist fetch also returns `[]` on failure (`_fetch_one`, `:778-781`).
**Impact:** If the Xtream endpoint returns a **partial** list (some categories succeed, some 500) but `all_vod_keys` is still non-empty, cleanup deletes every movie whose category failed to fetch — a transient provider hiccup silently purges thousands of rows (and on the next Plex generation, deletes their `.strm`/NFO too). The guard `if all_vod_keys:` (`:1012`) only protects against a *total* empty fetch, not a partial one.
**Root cause:** cleanup trusts the fetched key set as ground truth with no sanity floor / per-category success accounting.
**Suggested fix:** track which categories actually returned data; only run cleanup for categories confirmed reachable. Add a safety ratio guard (abort cleanup if it would delete > X% of existing rows, mirroring the validation circuit breaker).

### CR-F03 — EPG is documented as part of the scheduled sync but is never synced by the pipeline  · dette (doc/behavior mismatch, P2)
**Evidence:** `grep -n "epg|EpgEntry|get_xmltv|get_short_epg"` in `sync_worker.py` returns only field-name hits (`:369,386`) — **no** `EpgEntry` insert and **no** `get_xmltv`/`get_short_epg` call anywhere in the sync flow. EPG ingestion exists only as on-demand API handlers (`app/api/live.py:190` `get_short_epg`, `:226` `EpgEntry(...)`). CLAUDE.md §5.1 and §2 claim "VOD+séries+épisodes+Live+EPG".
**Impact:** EPG is populated lazily, per-channel, only when a client calls the live EPG endpoint; the scheduled pipeline never backfills it. The `_cleanup_stale_epg` cron (`main.py:178-190`) prunes past entries with nothing replenishing them in bulk. Runtime DB shows **0 EPG entries** — consistent. Not a runtime bug, but the documented contract is wrong and operators will expect EPG after a sync.
**Root cause:** EPG bulk sync was never implemented in the worker (or was removed); docs not updated.
**Suggested fix:** either implement XMLTV bulk ingestion in the sync pipeline (the `get_xmltv` client method already exists, `xtream_service.py:242`), or correct §5.1/§2 to state EPG is on-demand only.

### CR-F04 — `_load_category_config` return signature contradicts its own type hint/docstring (latent footgun)  · P2
**Evidence:** `app/workers/sync_worker.py:666` declares `-> tuple[str, dict, dict]` and the docstring says "Tuple of (filter_mode, allowed_vod_categories, allowed_series_categories)", but the function returns **four** values `return filter_mode, allowed_vod, allowed_series, allowed_live` (`:702`), and the caller correctly unpacks four (`:904`).
**Impact:** No runtime bug today, but the lie in the signature is exactly the kind of thing that breaks when someone trusts the hint and unpacks three, or a type checker is later wired in.
**Root cause:** `allowed_live` was added without updating the annotation/docstring.
**Suggested fix:** fix to `-> tuple[str, dict, dict, dict]` and update the docstring.

### CR-F05 — Cross-coroutine event-loop concurrency uses one shared `db` session via savepoints; concurrency fan-out is bounded but the session is single-threaded — OK, but the per-batch `commit_with_retry` after `begin_nested()` defeats batch atomicity intent  · P2
**Evidence:** `:998-1006` — `async with db.begin_nested(): await upsert_media_batch(...)` then `await commit_with_retry(db)`. The savepoint protects only the upsert; the subsequent commit is on the outer transaction. The parallel `fetch_vod_info_safe` tasks (`:994`) do **not** touch the session (snapshot pattern, correctly documented `:885-895`), so there's no aiosqlite race there.
**Impact:** Low — the design is sound. The only subtlety: between the savepoint release and the commit, no other writer can interleave (single session), so atomicity holds. Listing this to confirm it was checked and is **not** a bug, against the §3 "commit_with_retry" convention.
**Root cause:** n/a.
**Suggested fix:** none required.

### CR-F06 — Hash-collision / unchanged-skip relies on MD5 over a small field subset; legitimate metadata edits to non-hashed VOD fields are invisible  · P2
**Evidence:** `_compute_dto_hash` (`:509-515`) hashes only `name, added, stream_icon, rating, category_id, container_extension`. `get_vod_info` detail fields (plot, duration, genres, tmdb_id) are fetched only when this hash changes (`:957-959`). `_compute_series_dto_hash` (`:518-524`) includes `last_modified`, which is good; VOD has no such field.
**Impact:** If a provider corrects a movie's plot/duration/tmdb_id without touching name/rating/icon, the row is treated as unchanged and the detailed `get_vod_info` is never re-fetched — stale metadata persists until `name`/`rating`/`added` changes. MD5 collision risk itself is negligible.
**Root cause:** the cheap hash is a deliberate API-call-saving heuristic but omits any "detail version" signal for VOD.
**Suggested fix:** include any available `last_modified`/`series_no`/`added` change signal for VOD; or periodically force a full re-fetch (e.g. every Nth sync) to repair drift.

---

## §5.2 ENRICHMENT

### CR-F07 — `ENRICHMENT_DAILY_LIMIT` under-counts real API calls; details-fetch calls are billed as the search call only  · P1
**Evidence:** `app/workers/enrichment_worker.py`. `api_used` is the **4th** tuple element. Scenario 4 (both IDs absent) does a `search_*` **and** a `get_*_details` (2 HTTP calls) but returns `api_used=2` only when a match is found (`:54`), and `api_used=1` when search returns no match (`:55`). Scenario 2 (tmdb present) returns `api_used=1` for one `get_movie_details` call (`:42`) — correct. **But** `get_movie_details`/`get_tv_details` internally call `_request` which, on a 429/5xx, **retries up to 4 times** (`tmdb_service.py:106`, `_RETRY_DELAYS=(1,2,4)`), each a real HTTP request — none counted. The daily budget `used += batch_used` (`:192`) therefore tracks *logical operations*, not *real API calls*, contradicting CLAUDE.md §5.2 "compté en appels API réels".
**Impact:** Under rate-limiting the real call count can be 2–4× the budgeted `used`, so the worker keeps going well past the intended daily ceiling, risking TMDB throttling/ban. Conversely a found Scenario-4 match correctly costs 2 but a not-found one costs 2 real calls? No — not-found does only the search (1 real call) so `api_used=1` is right there. The systematic undercount is the retry amplification.
**Root cause:** `api_used` is a static estimate computed at the call-site, decoupled from the actual number of `client.get` invocations in `_request`.
**Suggested fix:** increment a real counter inside `_request` (it already increments `tmdb_requests_total` per HTTP attempt — reuse that), and have the worker read the delta of that counter as `used`, instead of summing static estimates.

### CR-F08 — Scenario 3 (imdb present, tmdb absent) burns an attempt every run but never makes progress; reaches MAX_ATTEMPTS and is permanently skipped without ever resolving  · P1
**Evidence:** `enrichment_worker.py:45-47` (movie) / `:81-83` (series): when `existing_imdb and not existing_tmdb`, it returns `(item, None, None, 0)` — i.e. no enrichment, status set to `"skipped"`, `attempts += 1` (`_apply_enrichment_results :143-145`). Phase 1/2 re-select `skipped` items only `WHERE attempts < MAX_ATTEMPTS` (`:168,206`). The codebase **has** `tmdb_service.find_by_imdb_id` (`tmdb_service.py:192`) that could resolve imdb→tmdb, but the worker never calls it for Scenario 3.
**Impact:** Every item that has an IMDb id but no TMDB id is "processed" into a no-op, incrementing attempts, and after 3 syncs is permanently dropped from the queue — yet it was trivially resolvable via `/find`. These items stay un-enriched forever (no overview/poster/cast). They're also counted as work done.
**Root cause:** the `/find` resolver added for the AI flow was never wired into the enrichment worker's Scenario 3.
**Suggested fix:** in Scenario 3, call `find_by_imdb_id(imdb, media_type)`; on hit, fetch details (1 call) and enrich; on miss, mark skipped. At minimum, do not increment `attempts` for a deliberate no-op that has no path to success.

### CR-F09 — `_apply_enrichment_results` overwrites `unification_id`/`history_group_key` unconditionally, decoupling enriched items from their pre-enrichment grouping  · P2
**Evidence:** `enrichment_worker.py:106,113-114` sets `unification_id = f"imdb://{imdb}" or f"tmdb://{tmdb}"` and `history_group_key = new_unif`. The sync worker computed `unification_id` via `calculate_unification_id(title, year, tmdb_id)` (`sync_worker.py:164`) and `history_group_key` via `calculate_history_group_key(unif, rating_key, server_id)` (`:198`) — a different scheme.
**Impact:** After enrichment, the grouping/dedup key format changes from the sync scheme to an `imdb://`/`tmdb://` scheme. If any read path joins enriched and not-yet-enriched items on `history_group_key`/`unification_id` (cross-account unification, watch-history grouping), enriched and unenriched copies of the same title no longer group. Episodes set `unification_id=""` at sync (`:349`) and are never enriched (movies/shows only), so they're unaffected. Needs a read-path audit to confirm severity; flagged P2.
**Root cause:** two independent unification-key conventions that don't agree.
**Suggested fix:** make enrichment use the same `calculate_*` helpers, or make all read paths key off a single canonical column.

### CR-F10 — TMDB search confidence threshold + fr-FR language can systematically reject correct matches  · P2
**Evidence:** `tmdb_service.py:149,167` send `language=fr-FR` (default `config.py`), so `r_title` is the French title; `_best_match` (`:295`) fuzzes the (English-ish) Xtream `title` against the French title. Threshold is `>= 0.85` (`:323`, mirrored at worker `:52,88`).
**Impact:** A French provider title vs French TMDB title is fine, but mixed-language catalogs (English Xtream name, French TMDB localized name) get low `fuzz.ratio` and fall below 0.85 → no match → Scenario 4 returns `api_used=1`, status skipped, attempts burned (compounds CR-F08). Many legitimately-present titles never enrich.
**Root cause:** single-language search with a strict fuzzy gate; no fallback to `language=en-US` or to the original-title field.
**Suggested fix:** search both default and en-US; compare against both `title` and `original_title`; or lower threshold with a year-anchored secondary check.

---

## §5.3 STREAM VALIDATION

### CR-F11 — Circuit-breaker sample uses `==` exact equality; if the first result for an account is filtered (`is_broken is None`/`no_url`) the breaker can be evaluated one short or skipped  · P2
**Evidence:** `health_check_worker.py:371-374`: `if account_checked == circuit_breaker_sample and account_checked > 0`. `account_checked` is only incremented for non-None results (`:367`), but the breaker is checked **after** incrementing and **before** the rest of the loop body for that same result; the `account_broken` used in the ratio (`:375`) is incremented **later at `:424`**, i.e. *after* the breaker check. So on the iteration where `account_checked` first hits 50, `account_broken` reflects only the **first 49** results' broken count — the 50th result's broken-ness is not yet counted.
**Impact:** Off-by-one in the breaker ratio: `failure_rate = account_broken(49) / account_checked(50)`. A marginal account at exactly the 90% boundary can mis-trip or fail to trip by one sample. Also, because it's strict `==`, if for any reason `account_checked` jumps past 50 in a single non-incrementing path (it can't here, but it's brittle), the breaker would never fire. Minor, but the ordering bug is real.
**Root cause:** breaker evaluated before the current item's broken flag is folded into `account_broken`.
**Suggested fix:** move the `account_broken` increment above the breaker check, or compute the ratio over results already finalized; prefer `>=` over `==`.

### CR-F12 — Circuit-breaker rollback discards already-committed progress for the account and re-validates from scratch next run, but `last_stream_check` was committed for the first ~200 → uneven state  · P2
**Evidence:** `:386` `await db.rollback()` on trip rolls back only **uncommitted** updates. But `commit_interval=200` (`:331`) may have already committed the first batch(es) of the same account before the 50-sample breaker fires? No — breaker fires at `account_checked==50` < 200, so no commit yet for this account; the rollback is clean. **However** the breaker is per-account inside a shared session that may already hold **committed** rows from a *previous* account in `items_by_account`. Those are correctly kept. The subtle issue: after trip, `pending_updates=0` and the loop `break`s, but the `streams_alive_ratio` gauge is skipped (`:454` guarded by `not account_tripped`) — so a flapping account leaves a **stale** alive-ratio gauge from a prior run.
**Impact:** Observability drift: Prometheus `plexhub_streams_alive_ratio{account}` shows the last good value while the account is actually down (breaker-tripped). Operators may not notice an outage.
**Root cause:** gauge only updated on the happy path.
**Suggested fix:** on trip, set the gauge to the observed (very low) failure-implied ratio, or emit a separate `breaker_tripped` gauge/counter.

### CR-F13 — `run()` cron and `run_pipeline_validation()` share a module-level httpx client and per-call semaphores but can execute concurrently, doubling effective concurrency against the provider  · P2
**Evidence:** `_get_client()` returns a process-wide singleton (`:18-34`). `run()` is scheduled `cron hour=2` (`main.py:273`) and `run_pipeline_validation()` runs inside the `interval` pipeline (`main.py:254`). Each builds its **own** `asyncio.Semaphore(concurrency)` (`:216`, `:330`). If the 6h interval pipeline overlaps the 02:00 cron, two independent semaphores each allow `STREAM_VALIDATION_CONCURRENCY` (default 20) in-flight → up to 40 concurrent probes, plus shared client pool sized `max(50, concurrency*2)` may saturate.
**Impact:** Provider may rate-limit/ban under 2× the intended probe load; false-positive `timeout`/`connect_error` failures inflate broken counts (transient, needs threshold, so not immediately marked broken — mitigated by `_DEFINITIVE_PREFIXES` excluding timeouts). Low likelihood (both are `max_instances=1` independently, but they're different jobs so APScheduler won't serialize them against each other).
**Root cause:** no global validation lock across the two entrypoints.
**Suggested fix:** a module-level `asyncio.Lock` guarding both validation entrypoints, or a single shared semaphore.

### What's solid (5.3)
Definitive-failure classification (`_DEFINITIVE_PREFIXES`, `:92-103`) correctly distinguishes permanent (404/403/error-CT/empty/magic-fail) from transient (timeout/connect/503) — transient needs `STREAM_BROKEN_THRESHOLD` consecutive failures. The HEAD→Range-GET escalation with `content-length=="0"` distrust (`:131-138`) is a thoughtful guard against Xtream's "200 + empty body" dead streams. Cancelled tasks are awaited to release connections (`:390-392`).

---

## §5.4 PLEX GENERATION

### CR-F14 — Stale-delete in the generator removes shared series-level metadata files (`tvshow.nfo`, `poster.jpg`, `fanart.jpg`) when a single episode is delisted  · P1
**Evidence:** `generator.py:219-238`. For each stale `source_id` (episodes are individual entries in the mapping), it deletes `entry.path`, the same-stem `.nfo`, **and** the well-known files in the **same directory**: `("poster.jpg", "fanart.jpg", "movie.nfo", "tvshow.nfo")` (`:233`). For an episode, `entry.path` is `Series/<Show>/Season 01/<...>S01E01.strm`, so `parent` is the `Season 01` folder — `poster.jpg`/`tvshow.nfo` aren't there, so the season folder deletion is mostly inert. **But** for a movie whose folder also (incorrectly) housed nothing else this is fine; the real hazard is `cleanup_empty_dirs` (`:235`) climbing up and removing the season dir, while the series `tvshow.nfo`/poster at `Series/<Show>/` survive only because they're one level up. The blanket per-file delete list applied to whatever `parent` is means a future layout change silently nukes series art.
**Impact:** Today: limited (season-level), but the deletion of `tvshow.nfo`/`poster.jpg` keyed off an *episode's* parent dir is a latent footgun; combined with CR-F01 (episodes never marked stale from DB) the generator's own stale-detection (`mapping ids − seen ids`) is the only thing pruning episodes, and it deletes per-episode without series-level reconciliation.
**Root cause:** delete logic assumes the media file's parent dir is the metadata-bearing dir, which is true for movies but not for episodes (metadata is two levels up).
**Suggested fix:** scope the well-known-file deletion to movie entries only; for episodes delete just the `.strm` + episode `.nfo` and let series-level metadata be removed only when the *show* source_id goes stale.

### CR-F15 — `write_file` and `download_image` preserve any pre-existing file, so updated NFO/poster after re-enrichment is never re-written  · P2
**Evidence:** `storage.py:104-108` `write_file` returns early `if full.exists()`; `:110-114` `download_image` returns `True` if exists; `:133-135` `submit_image_download` returns `None` if exists. Comment says "Preserve existing file (enriched by Tiny Media Manager)". The generator only ever calls `write_file` for NFO on create/move (`generator.py:307`), never on the URL-only update branch (`:294-299`).
**Impact:** When enrichment later fills overview/cast/poster for a movie that already had a `.strm` generated, the NFO/poster are **not regenerated** because the file already exists and the path didn't change (so it hits the `unchanged`/URL-update branch, which doesn't write NFO). The Plex library keeps the pre-enrichment (often empty) NFO. This is the intended "don't clobber TMM edits" behavior but it also blocks the backend's own metadata refresh.
**Root cause:** no content-hash on NFO/image; existence is the only freshness signal, and metadata updates don't change the path so the generator doesn't even attempt a rewrite.
**Suggested fix:** track an NFO content hash in the mapping entry; rewrite when source metadata changed and the file is backend-managed (distinguish from TMM-managed via a marker), or expose a `--force-metadata` regeneration mode.

### CR-F16 — Image-download futures are awaited with a per-future 30s timeout but a failed `future.result(timeout=30)` raises `TimeoutError` that is bucketed but the underlying thread keeps running  · P2
**Evidence:** `generator.py:202-216`. `future.result(timeout=30.0)` — on timeout the future is **not** cancelled (the pool task keeps downloading), it's just counted as an image failure. Pool is shared, `max_workers=8` (`storage.py:36`).
**Impact:** A few slow hosts can leave up to 8 zombie download threads running after `generate()` returns, holding connections; across repeated generations these accumulate until `shutdown_image_pool()` at app shutdown. Bounded (8 workers) so not catastrophic, but a slow CDN can stall the whole generation's tail (the loop awaits each future serially, `:202`).
**Root cause:** serial `future.result` wait with no aggregate deadline; no cancellation.
**Suggested fix:** use `concurrent.futures.wait(..., timeout=...)` with a single global deadline, then cancel stragglers.

### What's solid (5.4)
Atomic writes via tempfile+`os.replace`+`fsync` (`storage.py:11-27`) genuinely prevent half-written `.strm` from being scanned by Plex. The mapping store streams JSON and writes atomically (`mapping.py:59-81`), with corruption-tolerant load (`:55-57`). Name-collision resolution (`_resolve_movie_names`/`_resolve_series_names`, `generator.py:51-121`) is a careful suffix→short-id disambiguation. `DatabaseSource` streams with `yield_per=1000` (`source.py:54`) — memory-safe on the 77k-episode table. Idempotency for the create/move/update/unchanged states is correct given a stable mapping.

---

## §5.5 AI RECS

### CR-F17 — `rank` allows a candidate equal to the ref only when computing, but `rank-multi` with `exclude_refs=False` lets refs appear in results AND the centroid, self-biasing the ranking  · P2
**Evidence:** `ai.py:234-237` (`rank`) explicitly drops `tid == ref_tmdb` from candidates — correct. `ai.py:289-293` (`rank-multi`): when `exclude_refs=False`, refs remain in `cand_ids`, and `:327-328` builds `cand_vecs` from `cand_set` **including** the ref ids that are also in `vectors`. So a ref that is also a candidate gets ranked against a centroid that *contains itself*, yielding an inflated near-1.0 score and topping the list.
**Impact:** With `exclude_refs=False` the user's own seed items dominate the recommendations (score ≈ 1.0), which is rarely the desired "more like these" behavior. Documented as a flag, so arguably intended, but the self-inclusion in the centroid makes the score semantically off (a ref scored against a centroid built partly from itself).
**Root cause:** centroid and candidate set overlap is not handled when `exclude_refs` is off.
**Suggested fix:** even when `exclude_refs=False`, exclude each ref from being scored against a centroid that includes it, or document that `exclude_refs=False` is for diagnostics only.

### CR-F18 — `hydrate_misses` cap+timeout drops are correct, but a single `EmbeddingUnavailableError` mid-batch aborts the whole request after other tasks already wrote to the cache  · P2
**Evidence:** `recommendation_service.py:182-189`. `asyncio.gather(..., return_exceptions=True)` collects all; if any result is `EmbeddingUnavailableError` it re-raises (`:189`) → endpoint returns 503 (`ai.py:214-219`). But `_fetch_and_store_one` (`:120-150`) commits each cache+embedding row **before** the gather completes; an `EmbeddingUnavailableError` from `embed_passages` is raised *before* any DB write (`:113`), so partial cache writes from *successful* siblings are already committed.
**Impact:** Mostly benign — successful siblings legitimately populated the cache. The 503 is returned even though some hydration succeeded; the client retries and those hits are now cached. The contract (3×503 patterns) holds. No data corruption. Listed to confirm the cold-start/503 path is coherent.
**Root cause:** n/a (acceptable).
**Suggested fix:** none required; optionally return 200 with `cacheMissesDropped` reflecting the unavailable ones instead of a hard 503 when at least the ref resolved.

### CR-F19 — `rank-multi` reports `cacheHits = len(cached)` over the union of refs+candidates, conflating ref and candidate hit accounting  · dette (P2)
**Evidence:** `ai.py:296-298` `all_ids = list({*ref_ids, *cand_ids})`; `cache_hits = len(cached)`. Same in `rank` (`:204-205`). The response field `cacheHits` thus counts ref vectors too, not just candidate hits.
**Impact:** A client using `cacheHits`/`cacheMisses` to reason about catalog coverage gets numbers inflated by the (small) ref set. Cosmetic/contractual, not functional.
**Root cause:** combined query for efficiency, stats not split.
**Suggested fix:** document that the counts include refs, or split ref vs candidate accounting.

### CR-F20 — `embedding_worker` rebuild advances cursor on empty-content rows and marks them processed-skipped, but never sets `embedded_at`, so empty rows are re-scanned every rebuild  · P2
**Evidence:** `embedding_worker.py:84-90`: `cursor = tmdb_id` (advances), then `if not overview and not genres: continue` — `embedded_at` stays NULL. The page query is `WHERE embedded_at IS NULL AND tmdb_id > :cursor` (`:76`). Within one rebuild run the cursor moves past them, so they aren't re-read in that run; but the **next** rebuild starts `cursor=0` and reads them again (they're still `embedded_at IS NULL`).
**Impact:** Rows with no overview/genres are permanently un-embeddable yet permanently `pending`, so every `/embed/rebuild` re-scans (and re-skips) them — O(empty_rows) wasted reads each rebuild, and `pending_embed` in `/embed/status` (`ai.py:401`) is permanently > 0, misleading operators into thinking work remains.
**Root cause:** "skip" path doesn't durably mark the row as terminally skipped.
**Suggested fix:** set `embedded_at` (or a sentinel/`skipped` flag) on empty-content rows so they leave the pending set.

### What's solid (5.5)
The 3 contractual 503 patterns are correctly placed: `AI_API_KEY` empty + sqlite-vec unloaded in `deps.py:32-41` (router-level dep), model-load failure → `EmbeddingUnavailableError` → 503 in `ai.py:216-219,306-311`. Cold start is genuinely deferred (`embedding_service.py:65-82`, double-checked lock). `HYDRATE_CAP=20` + 10s/task timeout (`recommendation_service.py:28-29,172-180`) bounds per-request work. `DELETE-then-INSERT` on the `vec0` virtual table (`:142-149`, worker `:108-116`) is the correct workaround for UPSERT-unsupported virtual tables. Rebuild never runs at boot (only via `enqueue_rebuild`), `JOBS_CAP=100` FIFO eviction (`embedding_worker.py:43-45`), cursor pagination is memory-safe. Vectors are L2-normalized so cosine == dot product (`cosine_rank`, `recommendation_service.py:225-230`).

---

## §5.6 TV PAIRING

### CR-F21 — Race between `/status` (deliver-once) and `/complete`: payload can be delivered, then `/complete` scrubs, but a concurrent second `/status` poll can re-read before `payload_delivered` commit lands  · P2
**Evidence:** `tv_auth.py:332-349`. The deliver-once guard is `status == APPROVED and not payload_delivered`; it decrypts, sets `payload_delivered=True`, then `await db.commit()`. Two concurrent polls (TV retries aggressively) both load the row with `payload_delivered=False` (separate sessions, SQLite default read), both pass the guard, both decrypt and return the payload, then both commit `payload_delivered=True`. SQLite serializes the writes but the **reads** already happened.
**Impact:** The "delivered exactly once" security property can be violated under concurrent polling — the encrypted payload (e.g. Plex token) is returned twice. Low practical risk (same TV, same payload) but it breaks the documented one-shot contract and widens the exposure window. The 32-byte deviceCode is the only secret gating this.
**Root cause:** read-check-write is not atomic; no `SELECT ... FOR UPDATE` equivalent / conditional UPDATE.
**Suggested fix:** make delivery a conditional UPDATE that flips `payload_delivered` and returns affected-row count (`UPDATE ... SET payload_delivered=1 WHERE id=? AND payload_delivered=0`); only decrypt+return if rowcount==1.

### CR-F22 — `complete` does not require the payload to have been delivered; a TV can complete (scrub) a session whose payload it never read  · P2
**Evidence:** `tv_auth.py:361-387`. `complete` accepts any `APPROVED` session and scrubs `payload_encrypted` (`:383`). It does not check `payload_delivered`.
**Impact:** If a client calls `/complete` before its single `/status` delivery (or a malicious holder of the deviceCode races it), the payload is scrubbed and the legitimate TV's `/status` will then return `APPROVED` with **no** payload — pairing silently fails with no recovery (session is `COMPLETED`, payload gone). The TV gets stuck.
**Root cause:** complete is decoupled from delivery.
**Suggested fix:** require `payload_delivered is True` before allowing complete, or have complete itself deliver-and-scrub atomically.

### CR-F23 — Expired-session scrub in `_expire_if_needed` commits inside a GET handler that may also be mutating, and on TTL boundary returns expired even though the payload was valid moments ago — acceptable, but `start` cleanup deletes by `expires_at < now - grace`, not by status  · dette (P2)
**Evidence:** `tv_auth.py:177-189` commits the expiry flip. `start`'s opportunistic cleanup (`:215-217`) deletes `WHERE expires_at < now - _CLEANUP_GRACE_MS` regardless of status — a `COMPLETED` session within the grace window lingers (fine), but a long-lived `APPROVED`-but-unpolled session past TTL+grace is hard-deleted, freeing its `user_code`. Correct, but means an approved-yet-never-polled pairing is unrecoverable after ~16 min with no audit trail.
**Impact:** Minor; expected for a device flow. No security issue (payload was scrubbed on expiry at `:187`).
**Root cause:** n/a.
**Suggested fix:** none required; optionally log purges for audit.

### What's solid (5.6)
`start` is unauthenticated and returns 201 with a 32-byte urlsafe deviceCode + 8-char unambiguous userCode, with collision-retry on the UNIQUE userCode (`:222-247`). `approve` is constant-time authenticated (`verify_pairing_api_key`, `:72-87`), rejects empty payloads (`:283`), and only transitions PENDING→APPROVED (`:296`). Payload is Fernet-encrypted at rest (`payload_crypto.py`), scrubbed on expiry (`:187`) and on complete (`:383`). Status correctly never re-delivers after `payload_delivered` (the flag check), and the decrypt-failure path returns 503, not 500-with-leak (`:336-343`). Key resolution order (explicit Fernet key → derived from AI_API_KEY → None→503) is sound.

---

## Scheduled pipeline & initial boot run

### CR-F24 — `initial_sync_then_enrich` (boot) and the interval `scheduled_sync_enrich_generate` are independent tasks with no mutual exclusion; on a slow first sync they overlap and double-run the whole pipeline  · P1
**Evidence:** `main.py:310-320` fires `initial_sync_then_enrich` as a fire-and-forget `create_background_task` at boot. The interval job is registered with `max_instances=1, coalesce=True` (`:269-271`) — but `max_instances` only serializes the **interval job against itself**, not against the boot task. If `SYNC_INTERVAL_HOURS` is small (or the first sync on a 102k-item catalog exceeds the interval), the scheduler fires `scheduled_sync_enrich_generate` while `initial_sync_then_enrich` is still running. Both call `sync_worker.run_all_accounts()` → `sync_account(id)`. The per-account `asyncio.Lock` (`sync_worker.py:37-41,860-862`) saves the **sync** stage (second caller logs "skipping"), but enrichment, validation, and Plex generation have **no** such lock and will run concurrently against the same DB/files.
**Impact:** Two concurrent `_auto_generate_plex_library()` runs write the same `.strm`/NFO/mapping files for the same account → `.plex_mapping.json` last-writer-wins corruption / lost deletes; two concurrent enrichment runs double-spend the TMDB budget; two validations double the probe load (compounds CR-F13). The atomic-write + savepoints prevent file *corruption* but not *logical* races on the mapping (loaded into memory by each generator independently, then saved — the second save clobbers the first's deletions).
**Root cause:** the per-account sync lock is the only guard; the rest of the pipeline assumes single-flight by convention, which the boot+interval overlap breaks.
**Suggested fix:** a single module-level `asyncio.Lock` (or a `max_instances=1` job that *also* runs the initial sync, e.g. `next_run_time=now`) guarding the whole `sync→enrich→validate→generate` chain so boot and interval cannot interleave.

### CR-F25 — Pipeline stages share no transaction and have no inter-stage error isolation guarantees beyond a top-level try/except; a stage that partially fails still feeds the next stage  · P2
**Evidence:** `main.py:247-258`. The chain is linear `await`s inside one try/except (`:249-258`); if `run_all_accounts` raises, enrichment/validation/generation are skipped (`:257`). But if a stage *swallows* its own errors (sync swallows per-account/per-batch errors, `sync_worker.py:1004,1106,1176`), it returns "successfully" with partial data and the next stage proceeds on incomplete state — e.g. Plex generation runs on a half-synced catalog and deletes `.strm` for items that simply failed to fetch this run (compounds CR-F02).
**Impact:** Partial sync failure silently propagates into destructive generation/cleanup. No stage records a "completeness" signal the next stage can gate on.
**Root cause:** stages communicate only through DB state, with no success/completeness contract.
**Suggested fix:** have each stage return a status (e.g. "complete" vs "partial/degraded"); skip destructive cleanup/generation when the upstream stage reports degraded.

### What's solid (pipeline)
`max_instances=1, coalesce=True, misfire_grace_time` are correctly set on every job (`main.py:264-306`); `coalesce` correctly collapses missed runs. Background tasks are tracked and cancelled+awaited on shutdown (`main.py:328-329`, `utils/tasks`). Master-only scheduling via `fcntl.flock` is atomic (no TOCTOU). Blocking calls (`sqlite3.backup`) are offloaded to `asyncio.to_thread` (`:296`). Scheduler shutdown is `wait=False` after task cancellation — clean.

---

## What's solid (cross-cutting)

- **Per-account sync lock** (`asyncio.Lock`) correctly prevents concurrent syncs of the same account and logs+skips re-entry (`sync_worker.py:860-862`).
- **Incremental dto_hash skipping** for VOD/series/live avoids the expensive `get_*_info` detail calls on unchanged items — the dominant cost saver (logs show 6750 enrichment calls, not 100k+).
- **Savepoints + `commit_with_retry`** give per-batch resilience: a poisoned batch rolls back and the loop continues (`:998-1006,1169-1177,1235-1241`).
- **`commit_with_retry`/`run_with_retry`** correctly retry only on "database is locked" and re-raise other `OperationalError` (`db_retry.py:24-53`), layered above `busy_timeout=5000`.
- **Detached account snapshot** (`SimpleNamespace`, `:890-895`) correctly avoids the aiosqlite/greenlet race when parallel fetch coroutines run during `asyncio.gather`.
- **`enqueue_for_enrichment`** on-conflict updates only the `existing_*_id` fields, **not** status/attempts (`:615-621`), so re-syncing an already-`done` item does not reset it to pending — correct idempotency.
- **httpx clients** are reused singletons with bounded pools and retry/backoff (xtream 30s, tmdb 10s, validation configurable); 429/5xx honored with `Retry-After`.

---

## Findings summary

| Severity | Count | IDs |
|---|---|---|
| P0 | 0 | — |
| P1 | 6 | CR-F01, CR-F02, CR-F07, CR-F08, CR-F14, CR-F24 |
| P2 | 17 | CR-F04, CR-F05, CR-F06, CR-F09, CR-F10, CR-F11, CR-F12, CR-F13, CR-F15, CR-F16, CR-F17, CR-F18, CR-F19, CR-F20, CR-F21, CR-F22, CR-F25 |
| dette | 2 | CR-F03, CR-F23 |

(CR-F05 and CR-F18 are "verified-not-a-bug" confirmations, included for traceability.)
