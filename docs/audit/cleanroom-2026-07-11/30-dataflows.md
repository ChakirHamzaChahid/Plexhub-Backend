# Clean-room audit — Data-flows / End-to-end correctness

**Scope:** sync idempotency & cleanup, scheduler overlap, enrichment budget, dedup
convergence, TV pairing contract, stream-validation state machine, sync↔enrichment
`unification_id` consistency. Judged only from current code (branch `develop`).

**Verdict:** 3/5

The incremental design is fundamentally sound: per-account `asyncio.Lock` serialisation
(`sync_worker.py:34-41`), savepoints + `commit_with_retry` per batch, `dto_hash`/`content_hash`
skip logic, enrichment-owned identity preserved across content changes
(`upsert_media_batch` `sync_worker.py:597-610`), a deterministic and false-merge-safe
union-find convergence (`aggregation_service._merge_by_shared_ids`), and a stream-validation
state machine with a working recovery path. But there are several real correctness/idempotency
hazards that bite under *realistic* provider churn rather than exotic edge cases: an episode
differential-cleanup gap, a pagination-slot eviction that can delete still-listed rows on
provider reorder, no mutual exclusion between the boot pipeline and the interval pipeline,
a TMDB budget counter that under-counts real HTTP spend, and a `?unification_id=` lookup
that under-reports versions for exactly the split-identity groups the convergence exists to fix.

---

### CR-F02 — Pagination-slot eviction deletes unchanged, still-listed rows on provider reorder (P1)

**Where:** `app/workers/sync_worker.py:557-576` (Phase-1 eviction in `upsert_media_batch`);
`page_offset` sourced from the enumerate index in `map_vod_to_media` (`:184`, `"page_offset": index`)
and `map_episode_to_media` (`:332`). Unique key is only `(rating_key, server_id, filter, sort_order)`
(`:613`) — `page_offset` is NOT part of the identity.

**What:** For every *changed* row being upserted, Phase 1 deletes any DB row that shares
`(server_id, library_section_id, filter, sort_order, page_offset)` but has a different
`rating_key`. `page_offset` is the item's position in the *current* Xtream response
(`enumerate(vod_streams)`). Unchanged items are skipped by the `dto_hash` check
(`sync_worker.py:1024-1026`) and therefore keep their *first-seen* `page_offset` in the DB.
When a provider reorders a category, a changed item's new offset can equal an unchanged item's
stale offset (same `filter`), and the unchanged item is deleted — even though its `rating_key`
is still in `all_vod_keys`, so `differential_cleanup` (`:1081`) will not restore it. It only
reappears on the *next* sync, when its hash is missing → it takes the changed path and is
re-INSERTed fresh. A fresh INSERT bypasses the `ON CONFLICT` identity-preservation
(`:597-610`), so `tmdb_id` / `imdb_id` / `unification_id` revert to sync defaults.

**Impact:** Titles transiently disappear for up to `SYNC_INTERVAL_HOURS` (default 6h,
`config.py:42`); flickering items are repeatedly re-enriched (double-spending the already
mis-counted TMDB budget — see CR-F03); `unification_id` temporarily reverts to a title-based
key, perturbing dedup. Precondition: reorder + offset alignment within the same category, which
recurs across large catalogues.

**Fix direction:** Don't evict by `page_offset` (it isn't part of the identity); or only evict
a slot whose occupant is genuinely absent from the current API `rating_key` set; or refresh
`page_offset` for unchanged rows via a cheap standalone UPDATE instead of a delete.

---

### CR-F01 — Episodes are never differential-cleaned; orphans accumulate forever (P1)

**Where:** `app/workers/sync_worker.py` calls `differential_cleanup*` for `media_type="movie"`
(`:1081/:1086`), `media_type="show"` (`:1177/:1180`) and live (`:1312/:1315`) — **never** for
`type="episode"`. Episodes are only upserted (`:1238`). Orphan episodes are merely flipped to
`is_in_allowed_categories=False` by `category_service.py:348-377` (their `grandparent_rating_key`
no longer matches any visible show), never deleted.

**What:** Two orphan sources: (1) a delisted series — `differential_cleanup(media_type="show")`
deletes the show row, but its episodes (`type="episode"`, `grandparent_rating_key=series_<id>`)
survive with no owner and no cleanup pass; (2) episodes removed/renumbered inside a season keep
their old `ep_<id>` rows since nothing prunes stale episode `rating_key`s.

**Impact:** Unbounded growth of dead episode rows; the health-check cron `run()`
(`health_check_worker.py:253-266`) does not filter `is_in_allowed_categories`, so it wastes
validation cycles probing orphan episode streams; if a `series_<id>` rating_key is ever reused
for a different show, orphaned episodes could re-attach in `aggregate_series`
(`aggregation_service.py:230-243`, matched by `(server_id, grandparent_rating_key)`).

**Fix direction:** Add an episode cleanup pass — delete `type="episode"` rows whose
`grandparent_rating_key` is not among the currently-synced show `rating_key`s for the server,
and (for changed series) diff the fetched episode `rating_key`s against DB episodes of that show.

---

### CR-F04 — Boot pipeline and interval pipeline share no mutual exclusion (enrichment double-spend + racy Plex generation) (P1)

**Where:** boot task `initial_sync_then_enrich` (`app/main.py:328-338`) created via
`create_background_task` — *outside* the scheduler. Interval job
`scheduled_sync_enrich_generate` (`:249-274`) with `max_instances=1`, which governs only that
job's self-overlap. `enrichment_worker.run()` has no lock (`enrichment_worker.py:248`);
`_auto_generate_plex_library` has no lock (`main.py:77`); no generation lock exists in
`plex_generator/*`.

**What:** If the boot pipeline runs longer than `SYNC_INTERVAL_HOURS` (6h — realistic: a large
first sync + up to 50 000 enrichment items at concurrency 8 + a full stream-validation pass),
the interval job fires concurrently. Sync is protected (per-account lock → the second run's
`sync_account` returns `..._skipped`, `sync_worker.py:926-928`) and stream validation is
serialised (`_VALIDATION_LOCK`, `health_check_worker.py:215`), **but enrichment and Plex
generation are not.** Two concurrent `enrichment_worker.run()` pull overlapping queue items
(same `WHERE status IN (pending, skipped) ORDER BY created_at LIMIT daily_limit`) → duplicate
TMDB fetches and concurrent `UPDATE Media` on the same rows. Two concurrent generators write the
same tree + `.plex_mapping.json`: the JSON save is atomic (`mapping.py:59-77`, `os.replace`), so
the file itself is not corrupted, but the runs race on filesystem create/prune
(`prune_orphan_dirs` can delete files the other run just wrote) and the surviving mapping loses
one run's entries. `POST /api/plex/generate` can also run concurrently with the pipeline
generation.

**Impact:** Doubled TMDB spend, redundant DB writes, and a racy/incomplete generated library on
a slow first boot or a manual generate during the pipeline.

**Fix direction:** Guard the whole pipeline (at minimum enrichment + generation) with a shared
`asyncio.Lock` so the boot task, the interval job, and the manual generate endpoint are mutually
exclusive.

---

### CR-F03 — Enrichment "daily limit" under-counts real TMDB calls; item vs call semantics mixed (P1)

**Where:** `app/workers/enrichment_worker.py` — `api_used = n_search + 1`
(`:104-110`, `:84`); `_search_with_fallback` returns `len(attempts)` (`:60-64`); `used` gate at
batch boundaries `if used >= daily_limit: break` (`:277`, `:314`); query `.limit(daily_limit)`
caps *items* (`:271`, `:308`). `tmdb_service._request` retries 4 attempts
(`_RETRY_DELAYS=(1,2,4)` + final, `tmdb_service.py:16`, `:161`) on 429/timeout/5xx, and each
retry is a real HTTP GET — none are added to `api_used`.

**What:** (1) `api_used` counts *logical* search-chain attempts (up to 4) + a details call, but
not the up-to-4 HTTP retries inside `_request`; under sustained 429/5xx the real HTTP volume can
be ~4× the counted `used`. (2) The gate mixes semantics: `.limit(daily_limit)` bounds queue
*items* while `used` accumulates *logical calls*, and each item is 1–5 calls — so `used` hits
the limit after far fewer than `daily_limit` items (or the item cap dominates). (3) "Daily" is a
misnomer: the counter resets every `run()` (each `SYNC_INTERVAL_HOURS`), so it is a per-run cap.

**Impact:** Effective TMDB API volume can exceed the configured limit by 2–4× under exactly the
rate-limited conditions the limit exists to smooth. Not corruption, but the budget control is
ineffective and its unit is ambiguous.

**Fix direction:** Count real HTTP calls (increment a counter inside `_request`, or return an
attempt count and add it to `api_used`), separate "max items per run" from "max API calls," and
rename/clarify the daily-vs-per-run scope.

---

### CR-F05 — `/media/{movies,shows}/unified?unification_id=` under-reports versions for split-identity groups (P1)

**Where:** `app/services/media_service.py:167-179` (`get_unified_group` filters
`Media.unification_id == unification_id`); the list path aggregates ALL rows
(`get_unified_list` `:143-144`) so `_converge` folds `imdb://` / `tmdb://` / `title_` twins into
one group and the response advertises `unification_id=g.key` (`api/media.py:200`, `:262`) — the
representative (imdb-priority) key.

**What:** The list endpoint returns a group whose members were converged across *divergent*
`unification_id`s (Passes A/B in `aggregation_service.py:130-212` — per the code comments the
common case, ~1279 movie groups). When the client then calls
`/unified?unification_id=<representative key>`, `get_unified_group` fetches only rows literally
carrying that exact id, so `aggregate_movies` sees a single group key and `_converge` folds
nothing → the tmdb-only / title-based twin rows are dropped. The by-id response therefore has
fewer `versions[]` and a smaller `version_count` than the same group in the list.

**Impact:** User-facing inconsistency — the by-id lookup shows fewer playable sources/qualities
than the list did for the same title, precisely for the split-identity titles the convergence
machinery was built to unify.

**Fix direction:** Resolve the representative row(s), then re-query the full candidate superset
(rows sharing the same `imdb_id`/`tmdb_id`, plus the `title_<norm>_<year>` key) and run
`aggregate_movies` over that superset — or fetch the unfiltered candidate set and select the
converged group whose `key == unification_id`, mirroring the list path exactly.

---

### CR-F06 — TV pairing `GET /status` uses snake_case `device_code` while `start`/`complete` use camelCase `deviceCode` (P2)

**Where:** `app/api/tv_auth.py:298` — `device_code: str = Query(...)` (a Query param carries no
camelCase alias) vs `StartResponse.device_code` → alias `deviceCode` (`:88` + `_CAMEL_CONFIG`
`:76`) and `CompleteRequest.device_code` → body alias `deviceCode` (`:111`).

**What:** `start` returns `deviceCode`, `complete` accepts `deviceCode` in the JSON body, but the
poll requires `?device_code=` in snake_case. A client that reuses the camelCase key
(`?deviceCode=`) gets FastAPI 422 (missing required `device_code`) and never resolves status.

**Impact:** Contract inconsistency / foot-gun; a mismatched client cannot complete pairing. It
"works" today only because the Android client hard-codes the snake_case query.

**Fix direction:** Add `alias="deviceCode"` to the Query (accept both), or standardise on one
casing across all four endpoints.

---

### CR-F07 — TV pairing single-delivery is not atomic; concurrent polls can deliver the payload twice (P2)

**Where:** `app/api/tv_auth.py:314-331` — reads `session.payload_delivered`, decrypts, sets
`payload_delivered=True`, commits; no compare-and-set / no `WHERE payload_delivered=False` guard.

**What:** Two concurrent `GET /status` requests (separate `get_db` sessions) for an
approved-not-yet-delivered session both observe `payload_delivered=False`, both decrypt and
return the payload, both set the flag. The documented "delivered EXACTLY once" invariant
(`:14-15`) is racy.

**Impact:** A double-polling TV (or anyone holding the 32-byte `deviceCode`) can receive the
encrypted config payload more than once. Low exploitability (the deviceCode is secret) but the
invariant is not enforced.

**Fix direction:** Make delivery a conditional UPDATE
(`UPDATE tv_auth_sessions SET payload_delivered=1 WHERE device_code=? AND payload_delivered=0`)
and only return the payload when `rowcount == 1`; or lock the row for the read-modify-write.

---

### CR-F08 — Stream-validation circuit breaker fires once at exactly 50 checks; blind to late outages and small accounts (P2)

**Where:** `app/workers/health_check_worker.py:459-462` — `if account_checked == circuit_breaker_sample`
(`== 50`), evaluated a single time. Accounts with fewer than 50 streams never evaluate it.
Definitive failures (`head_403`/`head_404`/`ct_error`/`empty`/`magic_fail`, `:93-104`) bypass
`STREAM_BROKEN_THRESHOLD` and mark broken immediately — *not* gated by the breaker.

**What:** The per-account breaker trips only if the failure rate is ≥90% precisely at the 50th
check. A provider that dies *after* 50 OK checks, or a small account (<50 streams) hit by mass
definitive failures — e.g. a transient `403` "access revoked" that is really a temporary auth
hiccup (`head_403` is definitive) — can mass-mark streams broken with no breaker. Recovery does
occur on the next `STREAM_VALIDATION_RECHECK_HOURS` pass (default 24h, `config.py:51`;
`run_pipeline_validation` re-selects on `last_stream_check < cutoff` regardless of `is_broken`,
`:385-388`), so it is not permanent.

**Impact:** A brief provider auth/HTML-error blip can wrongly hide content for up to ~24h;
the breaker covers neither mid-run outages nor small accounts.

**Fix direction:** Evaluate the failure rate on a rolling window (every N checks and at the end),
and consider gating definitive-failure marking behind the same breaker.

---

### CR-F09 — Convergence Pass B absorbs a `title_` twin non-deterministically when two id-groups share the same title+year (P2)

**Where:** `app/services/aggregation_service.py:186-198` (`_absorb_title_groups`) —
`if tkey in groups and tkey not in remap: remap[tkey] = k`; iteration over `groups` follows dict
insertion order (query order), and `get_unified_list` feeds `aggregate_movies` with no `ORDER BY`
(`media_service.py:128-143`).

**What:** When a `title_…` orphan group and TWO distinct id-based groups all normalise to the
same title+year, the orphan is absorbed into whichever id-group is iterated first. Row order is
not stabilised, so the absorption target can flip between runs. (Pass A never falsely merges the
two id-groups together — that part is safe; only the orphan's home is unstable.)

**Impact:** Edge case — an unenriched duplicate attaches to film A one generation and film B the
next, causing a `.strm`/folder move in the generated library (rename churn) and a shift in the
unified API.

**Fix direction:** Choose the absorption target deterministically (e.g. `min` by `_key_rank`
among id-groups sharing `tkey`).

---

### CR-F11 — Episodes of an unchanged-DTO series are never re-synced; new/removed episodes missed (P2)

**Where:** `app/workers/sync_worker.py:1148-1152` (series skipped when
`_compute_series_dto_hash` unchanged) and `:1187-1188` (episodes fetched only for
`changed_series`). `_compute_series_dto_hash` (`:527-533`) hashes
`name/cover/plot/genre/rating/category_id/backdrop_path/episode_run_time/last_modified`.

**What:** Episodes are refreshed only for series whose series-level DTO hash changed. For an
ongoing show where the provider appends episodes without changing any of those fields (many
panels only mutate episode data; not all set `last_modified`), the new episodes are never synced
and removed ones never pruned.

**Impact:** Stale episode lists for ongoing series until an unrelated series field changes.
Provider-dependent (mitigated when the panel bumps `last_modified`).

**Fix direction:** Force a full episode refresh every Nth sync, or incorporate an
episode-count / show-last-updated signal into the hash.

---

### CR-F10 — REST unified and the generator share dedup LOGIC but not INPUT rows (debt)

**Where:** `plex_generator/source.py:94-95` filters `is_broken == False` when
`settings.STREAM_FILTER_BROKEN` *before* `aggregate_movies`; `media_service.get_unified_list`
defaults `include_broken=True` (`media_service.py:121`, `:140-141`). Generator rows are also
scoped to the selected accounts (`source.py:89-93`).

**What:** Both call the same `aggregate_movies`/`_converge`, but on different row sets — the
generator can drop the only imdb-bearing row of a group (broken), changing `best_row` and even
whether Pass A/B merges a twin. The "identical dedup" guarantee holds for the algorithm, not the
resulting grouping.

**Impact:** The on-disk library and the app's unified list can group/represent a title slightly
differently when broken-filtering is on. Mostly intended, but worth documenting so it is not
mistaken for a convergence bug.

**Fix direction:** No change required for correctness; document the divergence, or feed both
consumers the same row predicate when parity matters.

---

## What's healthy

- **Per-account sync serialisation** via a module-level `asyncio.Lock` map, with a non-blocking
  "already running → skip" (`sync_worker.py:34-41`, `:925-928`).
- **Enrichment-owned identity preserved across content changes**: `upsert_media_batch` coalesces
  `tmdb_id` and keeps an id-based `unification_id`/`history_group_key` (`%://%`) on a
  content-hash-triggered UPDATE (`sync_worker.py:597-610`) — the sync↔enrichment consistency
  mechanism works for the common case.
- **Convergence Pass A is deterministic and false-merge-safe**: union-find keyed on shared
  physical `imdb:`/`tmdb:` tokens, representative chosen by `min(_key_rank)` independent of union
  order; guards against `tmdb=0` and degenerate/non-latin titles in Pass B
  (`aggregation_service.py:114-203`).
- **Incremental `dto_hash` deliberately excludes flapping fields** (`stream_icon`, `rating`) with
  a documented rationale, avoiding needless `get_vod_info` re-fetches (`sync_worker.py:502-524`).
- **Stream-validation recovery path** works: previously-broken streams are re-selected on
  `last_stream_check < recheck_cutoff` regardless of `is_broken`, and a passing recheck resets
  `stream_error_count=0` / `is_broken=False` and counts a recovery
  (`health_check_worker.py:484-494`).
- **Stream validation vs cron serialised** by `_VALIDATION_LOCK` with a skip-if-busy cron
  (`health_check_worker.py:215`, `:232-236`, `:353-354`).
- **TV pairing TTL / expiry / scrub** is coherent: lazy `_expire_if_needed` flips stale sessions
  and nulls the payload; `approve` rejects non-pending (409) and expired (410); `complete` is
  one-shot and scrubs `payload_encrypted` (`tv_auth.py:159-171`, `:276-289`, `:356-366`).
- **`.plex_mapping.json` writes are crash-safe** (temp file + `fsync` + atomic `os.replace`,
  `mapping.py:59-77`) — safe against a single-writer crash (the concurrency hazard is CR-F04).
- **Differential cleanup is category-scoped** in whitelist/blacklist mode so non-synced
  categories are never touched (`sync_worker.py:435-499`), and `cleanup_orphan_enrichment_queue`
  reconciles dead queue rows each sync (`:700-722`).
