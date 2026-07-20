# Double-provider enrichment (TMDB + OMDb) & blended `display_rating` — design

Phase 1 (Architecte) artefact for a **Risky** `/refacto` of the enrichment path.
Read-only design: no `app/` code is touched by this document. It encodes the
locked product decisions, the exact contracts, and a staged migration plan the
ICs implement without a meeting.

Companion to `docs/plans/2026-07-17-omdb-id-consistency-validator-design.md`
(same OMDb client / cache / budget primitives — that work already shipped:
`app/services/omdb_service.py`, `app/services/omdb_scrape_cache_service.py`,
`OmdbScrapeCache` + **migration 022**, `app/scripts/validate_id_consistency.py`).
This refacto *extends* those primitives from "validate-only" to "enrich".

## Problème / Goal

Today OMDb is consulted only as a **tie-break** for low-confidence TMDB matches
(`enrichment_worker._omdb_contradicts`, confidence `< 1.0`), and its
`imdb_rating`/`imdb_votes` are **discarded** — those two `media` columns are
populated by `nfo_import_service` only. The user wants:

1. `imdb_rating` + `imdb_votes` **systematically** enriched via OMDb on every enrichment.
2. A **manual endpoint** to backfill OMDb ratings for all "incomplete" media
   (have an `imdb_id`, missing `imdb_rating`).
3. Enrichment becomes **TMDB + OMDb**: OMDb *completes* fields TMDB missed
   (fill-missing); if TMDB fully fails (`nomatch`), attempt an **OMDb-by-title** scrape.
4. `display_rating` recomputed as **blend(imdb, tmdb)** (an IMDb rating fetched via OMDb when absent).

## Locked product decisions (do not re-litigate)

- **D-BLEND** — `blend_rating(imdb, tmdb)`: both present → `(imdb+tmdb)/2`;
  exactly one present → that one; none → `None` (leave `display_rating`
  untouched, keeping the existing `COALESCE(scraped, audience, rating, 0.0)`
  fallback from `calculate_display_rating`, `app/utils/unification.py:40`).
  **No** TMDb==10→5.0 special case. A value `<= 0` (or `NULL`) counts as absent
  (mirrors the Android `blendRating` "ignore ≤ 0").

- **D-IDENTITY** — the OMDb-by-title fallback may write
  `imdb_id`/`unification_id`/`history_group_key` **only on a STRONG match**;
  otherwise **metadata-only** (ratings/summary/genres/cast, fill-missing),
  never touching identity/grouping. Thresholds I chose (§ Thresholds below).

## Verification of current state (confirmed against code)

| Claim | Verdict | Evidence |
|---|---|---|
| `Media.imdb_rating` (Float), `imdb_votes` (Integer) exist, NFO-only | ✅ | `app/models/database.py:92-93` |
| `display_rating` (Float, `nullable=False, default=0.0`), `scraped_rating`, `audience_rating`, `rating`, `tmdb_rating`, `tmdb_votes` all exist | ✅ | `database.py:74-75, 61, 60, 94-95` |
| Enrichment sets `scraped_rating = display_rating = vote_average` (overwrite-if-present) | ✅ | `enrichment_worker.py:342-344` |
| Enrichment writes `tmdb_rating`/`tmdb_votes` via **COALESCE** (fill-missing) | ✅ | `enrichment_worker.py:365-366, 369-371` |
| `imdb_rating`/`imdb_votes` never written by enrichment | ✅ | absent from the `rich` tuple `enrichment_worker.py:355-368` |
| Sync UPSERT puts `display_rating` in `update_keys` → **clobbered** to `rating_val or 0.0` when `content_hash` flips; `scraped_rating` is **not** in the sync row dict → survives | ✅ | `sync_worker.py:193/236/346` (row dict), `:695-733` (conflict set), `:502-506` `_HASH_EXCLUDE` |
| No "ScrapePreservation"-style guard on the backend; the only re-sync preservation is the `tmdb_id`/`unification_id`/`history_group_key` `COALESCE`/`CASE` at `sync_worker.py:713-726` | ✅ | same |
| OMDb tie-break already budget-gated + batch-deduped (`omdb_batch_cache`, `put_keys`) to avoid the `UNIQUE omdb_scrape_cache.imdb_id` double-insert | ✅ | `enrichment_worker.py:181-256, 278-320` |
| `OMDbData` has **no** `imdb_id` field today | ✅ | `omdb_service.py:36-48` |
| `omdb_scrape_cache` keyed by `imdb_id` only; found 30d / not_found 3d TTL; caller commits | ✅ | `omdb_scrape_cache_service.py:26-27, 58-79` |
| `omdb_scrape_cache` table + `OmdbScrapeCache` model already exist (migration 022, end of chain) | ✅ | `migrations.py:50, 1017-1049`; `database.py:364-383` |
| in-memory 202-job pattern (`enqueue_rebuild`/`register_job`/`get_job`, `JOBS_CAP=100`, `create_background_task`) is the backfill-endpoint precedent | ✅ | `embedding_worker.py:22-150`; `ai.py:713-738` |
| Pipeline order sync → enrich → generate → `unified_group_service.rebuild_all` | ✅ | `main.py:281-310` (`scheduled_sync_enrich_generate`), `:134` (`rebuild_all`) |
| Pattern-C mount convention (self-prefix + `verify_master_key`) for a JSON admin router | ✅ | `downloads.py:33-36`; `main.py:621` |

### Migration verdict — **NO new migration**
Every column the refacto writes (`imdb_rating`, `imdb_votes`, `tmdb_rating`,
`display_rating`) already exists, and the OMDb cache table shipped at migration
022. This refacto is **behaviour + one endpoint + one OMDb search method** — no
schema change, no `_migration_023`. (Confirmed: no genuine need for a job-state
table — the backfill reuses the in-memory `JOBS_CAP` precedent.)

### Negative-cache gap for OMDb-by-title — **resolved without new state**
`omdb_scrape_cache` is keyed on `imdb_id`; a title→**miss** has no id to key on.
Rather than invent a title-keyed cache, we **piggyback on the existing
`tmdb_scrape_cache` nomatch caching** (`enrichment_worker.py:314-320`, negative
TTL 3 days). Rule: **OMDb-by-title runs only on a FRESH TMDB `nomatch`
(`fr.from_cache is False`)**. A title TMDB already failed on is cached-nomatch
and short-circuits `_resolve` (`enrichment_worker.py:122-123`) before any OMDb
call — so the title-miss is negatively cached at the TMDB layer, once per
`NEG_TTL`, and OMDb-title is naturally rate-limited to first-seen titles. A
title→**found** result *does* have an `imdb_id` and is cached positively under it
in `omdb_scrape_cache` (normal path). **No new negative cache is needed.**

## Contracts (exact)

### C1 — `app/utils/rating_blend.py` (new, pure)
```python
def blend_rating(imdb: float | None, tmdb: float | None) -> float | None:
    """D-BLEND. <=0 / None => absent. both -> (imdb+tmdb)/2; one -> that one; none -> None."""

def blend_display_rating_case(imdb_expr, tmdb_expr, current_expr):
    """SQLAlchemy `case(...)` mirroring blend_rating over two column/bindparam exprs,
    returning `current_expr` (no-op) when BOTH are absent (<=0/NULL). Single source
    of truth for display_rating so it is always derivable from persisted columns."""

def recompute_display_rating_stmt():
    """UPDATE media SET display_rating = blend_display_rating_case(
        Media.imdb_rating, Media.tmdb_rating, Media.display_rating)
       WHERE type IN ('movie','show') AND (imdb_rating > 0 OR tmdb_rating > 0).
       SQL-only, no network — heals stale/clobbered display_ratings."""
```
A **parity test** asserts `blend_display_rating_case` == `blend_rating` over a
value grid on SQLite in-memory (same discipline as the Android SQL↔fn parity).

### C2 — `app/services/omdb_service.py`
- Additive field: `OMDbData.imdb_id: str | None = None` (default keeps old cached
  payloads deserializable via `OMDbData(**json.loads(payload))`,
  `omdb_scrape_cache_service.py:52` — back-compat holds because the field has a default).
- `get_by_imdb_id` also sets `imdb_id=data.get("imdbID") or imdb_id` (consistency).
- New:
```python
async def search_by_title(self, title: str, year: int | None, media_type: str) -> OMDbData | None:
    """OMDb `?t=<title>&y=<year>&type=movie|series&plot=full` — single best match.
    media_type 'movie'|'show' -> OMDb 'movie'|'series'. Returns OMDbData with imdb_id
    populated, or None (not found / unconfigured). Counts real HTTP attempts
    (get_request_count budget); key NEVER logged (same guard as get_by_imdb_id)."""
```
**`?t=` over `?s=`**: `?t=` is one call and lets OMDb pick the best; `?s=`
doubles calls (list + per-hit detail). OMDb-title is a long-shot fallback (TMDB,
multilingual, already failed), so the cheaper single call wins.

### C3 — `enrichment_worker.py` field-flow (the core rewrite)
**One OMDb fetch per item**, in the concurrent `_resolve` phase (parallelized
under the existing `Semaphore(CONCURRENCY=8)`), so the always-fetch requirement
does not serialize network latency across a 200-item batch. `FetchResult` gains:
`omdb: OMDbData | None`, `omdb_put: tuple[str, str] | None` (imdb_id, "found"|"not_found"
to persist — `None` on cache-hit/skip), `omdb_identity: bool` (strong title match).

- **Scenarios 2/3/4-matched** (an `imdb_id` is in hand): OMDb `get_by_imdb_id`
  (cache-first fresh-session read like `enrichment_worker.py:120-123`, budget-gated).
  *This one fetch serves BOTH the tie-break AND the rating enrichment* — the
  `confidence < 1.0` contradiction check (`_omdb_contradicts`) is refactored to
  accept the pre-fetched `fr.omdb` instead of fetching itself (**requirement 6: no double-call**).
  Note scenario 3 (`existing_imdb` set, no TMDB) previously returned `skipped`
  with no data (`:112-113`); it now still skips TMDB but fetches OMDb for ratings.
- **Scenario 4 fresh `nomatch`** (`from_cache is False`): `search_by_title` →
  classify STRONG / weak / discard (§ Thresholds).
- **Apply phase** (`_apply_enrichment_results`, single session):
  - Contradiction downgrade uses `fr.omdb` (no re-fetch); a downgraded match
    writes neither identity nor ratings (unchanged intent).
  - OMDb cache `put` deduped by `imdb_id` via an `omdb_put_keys` set — **extends
    the existing double-insert fix** (`put_keys`, `:278`) to the always-fetch path.
  - `imdb_rating`/`imdb_votes` → **COALESCE fill-missing** (never clobber a richer
    NFO value; matches the house ethos and design Q1 recommendation).
  - `scraped_rating` stays = TMDB `vote_average` (unchanged; durable raw-TMDB record).
  - `display_rating = blend_display_rating_case(COALESCE(Media.imdb_rating, :new_imdb),
    COALESCE(Media.tmdb_rating, :new_tmdb), Media.display_rating)` — computed from
    the post-write persisted columns, so it is reproducible in SQL (design Q2).
  - STRONG OMDb-title → write identity (`imdb_id`/`unification_id`/`history_group_key`)
    + metadata fill-missing; weak → metadata + ratings fill-missing, **no identity**.
- **`run()`**: add `omdb_service.reset_request_count()` next to the TMDB reset
  (`:413`); at the end, execute `recompute_display_rating_stmt()` + commit (heals
  `content_hash`-flip clobbers before generation + `rebuild_all`).

### C4 — Backfill endpoint (`app/api/enrichment.py`, new; Pattern C)
Self-prefixed `/api/admin/enrichment`, `dependencies=[Depends(verify_master_key)]`
(admin-grade: spends OMDb budget + mutates the whole catalog → master secret only,
same bar as the download JSON mirrors), mounted in `main.py` next to
`downloads.router` (`main.py:621`). Pydantic v2 camelCase, `response_model`, no bare dict.
```
POST /api/admin/enrichment/omdb-backfill  -> 202 {jobId}
GET  /api/admin/enrichment/jobs/{jobId}   -> {jobId,status,scanned,omdbFetched,imdbFilled,
                                              displayRecomputed,errors,lastError,startedAt,finishedAt}
Request OmdbBackfillRequest { mediaType: 'movie'|'show'|'all' = 'all',
                              recomputeDisplayRating: bool = true, limit: int | None = null }
```
Worker `app/workers/enrichment_backfill_worker.py` mirrors `embedding_worker`
(in-memory `OrderedDict` job store, `JOBS_CAP`, `create_background_task`) plus an
**in-memory single-run guard** (reject a 2nd concurrent backfill):
- **Phase A** — keyset-paginate "incomplete" media
  (`imdb_id IS NOT NULL AND imdb_id != '' AND imdb_rating IS NULL AND type IN (...)`)
  → OMDb `get_by_imdb_id` (cache-first, budget-gated, **fail-open**) → fill-missing
  `imdb_rating`/`imdb_votes` + `display_rating` blend (reuses C1/C3 helpers).
- **Phase B** (if `recomputeDisplayRating`) — `recompute_display_rating_stmt()`
  over the catalog (SQL-only, heals already-complete-but-stale rows — the adjacent
  need in design Q5; folded in, reported separately, flag-gated).

Any-worker (not master-gated: it is admin-triggered, not scheduler-driven). Same
process-local job-store caveat as embed rebuild (CR-A06) — documented, acceptable
for MVP. All writers go through `commit_with_retry`/`run_with_retry` (WAL lock safety).

## Thresholds I chose (D-IDENTITY)

OMDb-by-title, normalized titles via `normalize_for_sorting`,
`sim = max(fuzz.ratio, fuzz.token_set_ratio)/100`, `omdb_year` via `_parse_omdb_year`:

- **Discard** (treat as OMDb-nomatch, write nothing): `sim < 0.60` — a floor so a
  wildly-off `?t=` hit never pollutes metadata. (0.60 ≈ the `_omdb_contradicts`
  0.55 contradiction floor.)
- **STRONG** (identity write allowed): `omdb_year == item.year` (**year exact — hard
  gate**, requires `item.year`) **AND** `sim >= 0.90` **AND** OMDb `type` matches
  media_type (movie↔movie, series↔show).
- **Weak** (`0.60 <= sim < 0.90`, or year not exact, or `item.year` absent):
  metadata-only, fill-missing, no identity.

**Rationale — deliberate asymmetry vs `_omdb_contradicts`.** Keeping an existing
match only requires the *absence* of a strong contradiction (year gap >1 **and**
sim <0.55). *Asserting a new identity* from a bare title is the opposite risk, so
the bar is much higher: **year-exact** (0 tolerance, not ±1) is the hard gate
because OMDb frequently returns the English title (title alone is never
conclusive), and `sim >= 0.90` + type-match on top. This errs toward
metadata-only (safe: fill-missing, self-healing) and never toward a false
identity write (which would mis-group a title into another's Plex folder).

## Staged plan

Model-effort routing per wave; W2 and W3 both depend only on **W1** (the shared
primitives live there), so they run **in parallel** once W1 lands.

### Wave 1 — primitives (parallel-safe, additive, no behaviour change)
- Owner **sync-specialist** · model **sonnet**.
- `app/utils/rating_blend.py` (C1) + `OMDbData.imdb_id` + `omdb_service.search_by_title` (C2).
- Tests: `tests/test_rating_blend.py` (blend grid + SQL↔fn parity on `:memory:`),
  extend `tests/test_omdb_service.py` (search_by_title match/not-found/type-map/budget;
  old-payload deserialization back-compat for the new field).
- **DoD**: pytest + ruff green; parity proven; back-compat proven; zero wiring.

### Wave 2 — enrichment path rewrite (Risky core; depends W1)
- Owner **sync-specialist** · model **opus**.
- `enrichment_worker.py` per C3: unified single OMDb fetch (tie-break + ratings),
  systematic `imdb_rating`/`imdb_votes` fill-missing, OMDb-by-title fallback
  (thresholds above), `display_rating` blend, `omdb` reset + end-of-run recompute,
  extended batch-dedup.
- Tests (extend `tests/test_enrichment_scraping.py`, `tests/test_enrichment_guard.py`,
  fake-double style — no `AsyncMock`): (a) matched → OMDb fetched once, imdb ratings
  filled, NFO value not clobbered, `display_rating == blend`; (b) `confidence<1.0`
  contradiction still downgrades from the SAME single fetch (assert no 2nd OMDb call);
  (c) nomatch + strong title → identity written; weak → metadata-only, identity
  untouched; `sim<0.60` → nothing; (d) OMDb budget exhausted → TMDB still applied,
  OMDb skipped, no crash; (e) two items sharing an `imdb_id` → one cache put, no
  `UNIQUE` violation; (f) `from_cache` nomatch → OMDb-title NOT attempted.
- **DoD**: pytest + ruff green; boot OK; no double OMDb call; budget fail-open;
  double-insert dedup preserved; `display_rating` derivable from persisted columns.

### Wave 3 — backfill endpoint + recompute (depends W1; parallel with W2)
- Owner **backend-developer** · model **sonnet**.
- `app/api/enrichment.py` + `app/workers/enrichment_backfill_worker.py` (C4);
  mount in `main.py`.
- Tests: `tests/test_enrichment_backfill.py` (202 + jobId; 401 without master key;
  job status; incomplete-media selection; OMDb fill + `display_rating` recompute;
  Phase-B recompute-only is idempotent) + worker unit test.
- **DoD**: OpenAPI shows the endpoint (camelCase, no bare dict); master-key guard
  tested; in-memory job + single-run guard; recompute reuses C1 (no duplicated blend).

### Gate
`code-reviewer` per wave · `qa-engineer` on the W2 enrichment regressions ·
`integration-agent` final (OpenAPI coherent, `models/database.py` unchanged so
no ORM↔migration drift, contract to Android app `MediaResponse` unchanged —
`display_rating`/`isAdult`/etc. shapes identical, only values differ).

## Risks

- **`content_hash`-flip clobber of `display_rating`** (pre-existing,
  `sync_worker.py:695-733`): a provider content change reverts `display_rating`
  to the raw provider rating until re-enrichment. Not a regression — the blend is
  **self-healing** because it is recomputable from the durable `imdb_rating` +
  `tmdb_rating` (end-of-run recompute in W2, and the backfill Phase B). Documented,
  not "fixed" in sync (out of enrichment-path scope).
- **OMDb budget across processes**: `get_request_count()` is in-process (CR-F03
  residual). A non-master backfill running while the master pipeline enriches can
  exceed the daily OMDb quota in aggregate. Writes stay idempotent (fill-missing +
  upsert-by-PK), so only budget is wasted, never data corrupted. Mitigate by not
  triggering the backfill mid-pipeline; documented.
- **Weak OMDb-title metadata**: honoring the locked "metadata-only fill-missing"
  means a `0.60 ≤ sim < 0.90` hit can write a slightly-off summary/rating. Bounded:
  fill-missing only, and only for items TMDB matched to nothing (they had no rating
  anyway); the `sim < 0.60` discard floor is the safety valve.
- **OMDb-title STRONG identity → `unification_id` change → Plex folder gotcha**
  (§6 of the id-consistency doc): `LocalStorage` never rewrites an existing
  `movie.nfo`/`tvshow.nfo`/`poster.jpg`. For the nomatch→strong case the risk is
  low (the item moves from a title-based folder to a *new* `imdb://` folder with no
  pre-existing NFO; the old folder is orphan-pruned). Still flagged so operators
  know a re-labelled title may need a folder clear before regeneration.
- **Throughput**: ~102k catalog × ≤1 OMDb call, cached after → ~1–2 day first pass
  under the 95k/day default budget; cache persists, subsequent runs are cheap.

## ADR recommendation

The design doc is the primary artefact and is **sufficient to implement**. But
**one short ADR is warranted** at implementation time — recommend
`docs/architecture/adr/0003-dual-provider-enrichment-and-blended-display-rating.md`
(next number after 0001/0002) — because two decisions have lasting,
non-obvious impact a future maintainer will question:
1. `display_rating` no longer equals `vote_average`/`tmdb_rating` — it is now a
   **blend** derived from two persisted columns (why the numbers differ).
2. The **core enrichment path now depends on a second external provider (OMDb)**
   with a per-item budget and an identity-write policy.
Do **not** create the ADR now (Phase 1 is read-only + this one design doc); the
implementing wave (W2) writes it alongside the code. The doc `# Thresholds` and
`# Locked decisions` sections are the ADR's decision body when it is written.
