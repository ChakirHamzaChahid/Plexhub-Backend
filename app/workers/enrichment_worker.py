import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
from rapidfuzz import fuzz

from sqlalchemy import func, select, update

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, EnrichmentQueue, XtreamAccount
from app.services import scrape_cache_service as scrape_cache
from app.services import omdb_scrape_cache_service as omdb_scrape_cache
from app.services.omdb_service import omdb_service
from app.services.tmdb_service import TMDBEnrichmentData, TMDBSearchOutcome, tmdb_service
from app.utils.string_normalizer import normalize_for_sorting
from app.utils.time import now_ms
from app.utils.db_retry import commit_with_retry

logger = logging.getLogger("plexhub.enrichment")

BATCH_SIZE = 200  # Commit every N items
CONCURRENCY = 8   # Parallel TMDB requests (free tier ~4 req/s, keep headroom)
MAX_ATTEMPTS = 3  # Max enrichment attempts before permanently skipping

# --- Anti-recurrence guard (Wave 3, S5 — id-consistency validator design doc §5) --
# `_IMDB_ID_RE`: cheap shape tripwire for `TMDBEnrichmentData.imdb_id`.
_IMDB_ID_RE = re.compile(r"^tt\d+$")
# Conservative OMDb tie-break thresholds (tech-lead deviation D1 — this is the
# *primary* defense, not the doc's "Optionnel"). Both conditions must hold to
# downgrade a match: a year gap of more than 1, AND a low title similarity —
# title alone is never conclusive because OMDb frequently returns the
# original/English title for localized content.
_OMDB_YEAR_TOLERANCE = 1
_OMDB_TITLE_CONTRADICTION_SIM = 0.55


@dataclass
class FetchResult:
    item: EnrichmentQueue
    data: TMDBEnrichmentData | None
    confidence: float | None
    result: str            # matched | nomatch | ambiguous | skipped
    # NOTE (CR-F03): this is a count of *logical* TMDB calls attempted for this
    # item (searches + details fetch) — informational only, kept for the
    # per-batch/per-item bookkeeping below. It is NOT used to enforce
    # `ENRICHMENT_DAILY_LIMIT` anymore: `run()` budgets against
    # `tmdb_service.get_request_count()`, the count of *real* outbound HTTP
    # attempts (including every retry inside `tmdb_service._request`), since a
    # single logical call can cost up to 4 real HTTP attempts under
    # 429/5xx retries.
    api_used: int
    cache_key: str | None  # set when the outcome should be written to scrape cache
    from_cache: bool = False


async def _search_with_fallback(
    media_type: str, title: str, year: int | None, summary: str | None,
) -> tuple[TMDBSearchOutcome, int]:
    """Try progressively looser searches; return (outcome, network_searches).

    Order (plan §3.3): default → without year → en-US → /search/multi. Stops at
    the first auto-match; otherwise returns the highest-scoring attempt so the
    best score is still recorded."""
    search = tmdb_service.search_movie if media_type == "movie" else tmdb_service.search_tv
    multi_kind = "movie" if media_type == "movie" else "tv"
    attempts: list[TMDBSearchOutcome] = []

    o = await search(title, year, summary=summary)
    attempts.append(o)
    if o.result != "matched" and year is not None:
        o = await search(title, None, summary=summary)
        attempts.append(o)
    if attempts[-1].result != "matched":
        o = await search(title, year, summary=summary, language="en-US")
        attempts.append(o)
    if attempts[-1].result != "matched":
        o = await tmdb_service.search_multi(title, multi_kind)
        attempts.append(o)

    for o in attempts:
        if o.result == "matched":
            return o, len(attempts)
    # No match — surface the best-scoring attempt (for recording / metrics).
    best = max(attempts, key=lambda a: a.confidence)
    return best, len(attempts)


async def _resolve(item, media_type: str, semaphore) -> FetchResult:
    """Resolve one queue item to TMDB data, using the persistent scrape cache
    first and the fallback search chain on a miss."""
    get_details = (
        tmdb_service.get_movie_details if media_type == "movie"
        else tmdb_service.get_tv_details
    )
    async with semaphore:
        try:
            existing_tmdb = item.existing_tmdb_id
            existing_imdb = item.existing_imdb_id

            # Scenario 2: TMDB id known, IMDB missing — fetch details only.
            if existing_tmdb and not existing_imdb:
                tmdb_id = int(existing_tmdb) if str(existing_tmdb).isdigit() else None
                if tmdb_id:
                    details = await get_details(tmdb_id)
                    return FetchResult(item, details, 1.0, "matched", 1, None)
                return FetchResult(item, None, None, "skipped", 0, None)

            # Scenario 3: IMDB known, TMDB missing — keep IMDB, nothing to do.
            if existing_imdb and not existing_tmdb:
                return FetchResult(item, None, None, "skipped", 0, None)

            if not tmdb_service.is_configured:
                return FetchResult(item, None, None, "skipped", 0, None)

            # Scenario 4: both absent — persistent cache first, then search.
            cache_key = scrape_cache.make_key(media_type, item.title, item.year)
            async with async_session_factory() as cdb:
                hit = await scrape_cache.get(cdb, cache_key, now_ms())
            if hit is not None:
                return FetchResult(item, hit.data, hit.confidence, hit.result, 0, None, from_cache=True)

            outcome, n_search = await _search_with_fallback(
                media_type, item.title, item.year, item.existing_summary,
            )
            if outcome.result == "matched" and outcome.match:
                details = await get_details(outcome.match.tmdb_id)
                return FetchResult(
                    item, details, outcome.match.confidence, "matched",
                    n_search + 1, cache_key,
                )
            return FetchResult(item, None, outcome.confidence, outcome.result, n_search, cache_key)
        except httpx.HTTPStatusError as e:
            # A 404 means the tmdb_id supplied by the provider no longer exists
            # on TMDB — an expected, recoverable miss, not a code fault. Log it
            # quietly without a traceback; let real HTTP errors surface loudly.
            if e.response.status_code == 404:
                logger.info(f"Enrichment skipped for {item.rating_key}: TMDB 404 (stale id)")
                return FetchResult(item, None, None, "not_found", 0, None)
            logger.warning(f"Enrichment fetch failed for {item.rating_key}: {e}", exc_info=True)
            return FetchResult(item, None, None, "skipped", 0, None)
        except Exception as e:
            logger.warning(f"Enrichment fetch failed for {item.rating_key}: {e}", exc_info=True)
            return FetchResult(item, None, None, "skipped", 0, None)


async def _fetch_movie_data(item, semaphore):
    return await _resolve(item, "movie", semaphore)


async def _fetch_series_data(item, semaphore):
    return await _resolve(item, "show", semaphore)


def _shape_invalid(data: TMDBEnrichmentData) -> str | None:
    """Cheap intra-record tripwire on a single `get_details()` result.

    `tmdb_id`/`imdb_id` both come from the SAME TMDB response
    (`external_ids`, see `tmdb_service._parse_details`) so they are
    consistent by construction — this only bounds the root-cause search if
    object-level corruption is ever observed in prod. Returns a short reason
    string on failure, else None."""
    if data.tmdb_id is None or data.tmdb_id <= 0:
        return f"non-positive tmdb_id ({data.tmdb_id!r})"
    if data.imdb_id is not None and not _IMDB_ID_RE.match(data.imdb_id):
        return f"malformed imdb_id shape ({data.imdb_id!r})"
    return None


def _parse_omdb_year(raw: str | None) -> int | None:
    """OMDb `Year`: "1984" (movie), "2015–2019" / "2015-" (series, en-dash or
    open-ended). Take the leading 4-digit year; unparseable -> None."""
    if not raw:
        return None
    m = re.match(r"^(\d{4})", raw)
    return int(m.group(1)) if m else None


async def _omdb_contradicts(
    db, item: EnrichmentQueue, data: TMDBEnrichmentData, ts: int,
) -> bool:
    """Cross-check a low-confidence TMDB match against OMDb (by imdb_id).

    Fail-open on every non-conclusive path: unconfigured, budget exhausted,
    not found, missing year on either side, or any transport/parsing
    exception -> False (keep the match; absence of signal must never
    degrade a normal match). Downgrades ONLY when BOTH a year gap > 1 year
    AND a low title similarity hold — OMDb frequently returns the
    original/English title for localized content, so title alone is never
    conclusive (language-safety)."""
    imdb_id = data.imdb_id
    if not imdb_id:
        return False
    try:
        # Budget guard first — once the daily OMDb spend is exhausted, the
        # tie-break is disabled for the rest of this run (no downgrade),
        # even for ids that happen to already be cached.
        if omdb_service.get_request_count() >= settings.OMDB_DAILY_LIMIT:
            return False

        cached = await omdb_scrape_cache.get(db, imdb_id, ts)
        if cached is not None:
            omdb_data = cached
        else:
            omdb_data = await omdb_service.get_by_imdb_id(imdb_id)
            result = "found" if omdb_data is not None else "not_found"
            await omdb_scrape_cache.put(db, imdb_id, result, omdb_data, ts)

        if omdb_data is None or not item.year:
            return False
        omdb_year = _parse_omdb_year(omdb_data.year)
        if omdb_year is None:
            return False
        if abs(omdb_year - item.year) <= _OMDB_YEAR_TOLERANCE:
            return False

        query_norm = normalize_for_sorting(item.title)
        cand_norm = normalize_for_sorting(omdb_data.title)
        if not query_norm or not cand_norm:
            return False
        sim = max(
            fuzz.ratio(query_norm, cand_norm),
            fuzz.token_set_ratio(query_norm, cand_norm),
        ) / 100.0
        if sim >= _OMDB_TITLE_CONTRADICTION_SIM:
            return False

        logger.warning(
            "Enrichment guard: OMDb contradicts match for rating_key=%s "
            "(item year=%s, omdb year=%s, title similarity=%.2f) — downgrading to ambiguous",
            item.rating_key, item.year, omdb_year, sim,
        )
        return True
    except Exception as e:
        logger.warning(
            "Enrichment guard: OMDb tie-break failed for rating_key=%s (%s) — keeping match",
            item.rating_key, type(e).__name__,
        )
        return False


async def _apply_enrichment_results(db, results: list[FetchResult]):
    """Apply enrichment results to DB — IDs + rich metadata + scrape cache + metric."""
    from app.utils.metrics import tmdb_match_total

    batch_used = 0
    ts = now_ms()
    # Defer ORM flushes of the dirty EnrichmentQueue items to the single commit
    # in run(). Without this, every in-loop query autoflushes them, and if that
    # write hits the WAL writer lock (held for hours by stream validation) it
    # raises mid-loop and crashes the whole batch instead of being retried at
    # commit time. Paired with the 60s busy_timeout on the engine connection.
    with db.no_autoflush:
        # Dedupe cache writes within the batch: several items can share one
        # cache_key (same normalized title+year across versions/accounts). Under
        # `no_autoflush` a second `scrape_cache.put` for the same key can't see
        # the first's still-pending INSERT, so it would add a second row with the
        # same primary key → `UNIQUE constraint failed: tmdb_scrape_cache.cache_key`
        # crashes the whole batch at commit. One put per key is enough — the id is
        # still applied to every media row below regardless.
        put_keys: set[str] = set()
        for fr in results:
            item = fr.item
            enrichment_data = fr.data

            # --- Anti-recurrence guard (Wave 3, S5) -----------------------------
            # Runs BEFORE the outcome metric / scrape-cache write below so a
            # downgrade to "ambiguous" is reflected everywhere fr.result feeds
            # into (the plexhub_tmdb_match_total metric, the persisted scrape
            # cache resolution, and the id-write gate further down).
            if enrichment_data:
                shape_issue = _shape_invalid(enrichment_data)
                if shape_issue:
                    logger.warning(
                        "Enrichment guard: %s for rating_key=%s — downgrading to ambiguous",
                        shape_issue, item.rating_key,
                    )
                    fr.result = "ambiguous"
                    enrichment_data = None
                elif (
                    fr.confidence is not None and fr.confidence < 1.0
                    and enrichment_data.imdb_id and omdb_service.is_configured
                    and await _omdb_contradicts(db, item, enrichment_data, ts)
                ):
                    fr.result = "ambiguous"
                    enrichment_data = None
            # ---------------------------------------------------------------------

            # Outcome metric (only the genuine matching attempts — skip scenario 2/3).
            if fr.result in ("matched", "nomatch", "ambiguous"):
                metric_type = "movie" if item.media_type == "movie" else "tv"
                tmdb_match_total.labels(media_type=metric_type, result=fr.result).inc()

            # Persist the resolution so the same title is never re-queried.
            if fr.cache_key and not fr.from_cache and fr.cache_key not in put_keys:
                put_keys.add(fr.cache_key)
                await scrape_cache.put(
                    db, fr.cache_key, item.media_type, fr.result,
                    fr.confidence, enrichment_data, ts,
                )

            if enrichment_data:
                tmdb_id = enrichment_data.tmdb_id
                imdb_id = enrichment_data.imdb_id
                new_unif = f"imdb://{imdb_id}" if imdb_id else f"tmdb://{tmdb_id}"

                update_values = {
                    "tmdb_id": str(tmdb_id),
                    "imdb_id": imdb_id,
                    "unification_id": new_unif,
                    "history_group_key": new_unif,
                    "tmdb_match_confidence": fr.confidence,
                }
                if enrichment_data.overview:
                    update_values["summary"] = enrichment_data.overview
                if enrichment_data.genres:
                    update_values["genres"] = enrichment_data.genres
                if enrichment_data.poster_url:
                    update_values["resolved_thumb_url"] = enrichment_data.poster_url
                if enrichment_data.backdrop_url:
                    update_values["resolved_art_url"] = enrichment_data.backdrop_url
                if enrichment_data.vote_average:
                    update_values["scraped_rating"] = enrichment_data.vote_average
                    update_values["display_rating"] = enrichment_data.vote_average
                if enrichment_data.year:
                    update_values["year"] = enrichment_data.year
                if enrichment_data.cast:
                    update_values["cast"] = enrichment_data.cast

                # Rich metadata mirroring the NFO columns. Fill-missing-only via
                # COALESCE so we never clobber richer data already imported from a
                # tvshow.nfo / movie.nfo, nor the adult tagging's content_rating
                # ("XXX"). `imdb_rating`/`imdb_votes` stay untouched — TMDB has no
                # IMDb scores. (col, value) pairs; skipped when TMDB gave nothing.
                rich = (
                    ("content_rating", enrichment_data.content_rating),
                    ("original_title", enrichment_data.original_title),
                    ("tagline", enrichment_data.tagline),
                    ("premiered", enrichment_data.premiered),
                    ("status", enrichment_data.status),
                    ("studio", enrichment_data.studio),
                    ("country", enrichment_data.country),
                    ("tvdb_id", enrichment_data.tvdb_id),
                    ("wikidata_id", enrichment_data.wikidata_id),
                    ("tmdb_rating", enrichment_data.tmdb_rating),
                    ("tmdb_votes", enrichment_data.tmdb_votes),
                    ("cast_json", enrichment_data.cast_json),
                )
                for col, value in rich:
                    if value is not None:
                        update_values[col] = func.coalesce(getattr(Media, col), value)

                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                    )
                    .values(**update_values)
                )
                item.status = "done"
            else:
                # Record the best score even without a match (future manual review).
                if fr.confidence is not None:
                    await db.execute(
                        update(Media)
                        .where(
                            Media.rating_key == item.rating_key,
                            Media.server_id == item.server_id,
                        )
                        .values(tmdb_match_confidence=fr.confidence)
                    )
                item.status = "skipped"
            item.attempts += 1
            item.processed_at = ts
            batch_used += fr.api_used
    return batch_used


async def run():
    """Run enrichment batch with parallel TMDB fetching."""
    daily_limit = settings.ENRICHMENT_DAILY_LIMIT
    semaphore = asyncio.Semaphore(CONCURRENCY)

    # Budget against REAL TMDB HTTP calls (search + details + every retry
    # inside tmdb_service._request), not logical queue items — a single item
    # can cost up to 4 real HTTP attempts (1 initial + up to 3 retries) when
    # TMDB rate-limits (429) or 5xx's, so counting logical items/searches
    # under-counted real spend by 2-4x (CR-F03). `tmdb_service` exposes a
    # live counter incremented on every real outbound attempt; reset it here
    # so `used()` reflects only this run's spend (in-process only — see
    # `reset_request_count` docstring for the persisted-daily-quota residual).
    tmdb_service.reset_request_count()

    def used() -> int:
        return tmdb_service.get_request_count()

    logger.info(f"Starting enrichment batch (daily limit: {daily_limit}, concurrency: {CONCURRENCY})")

    async with async_session_factory() as db:
        # Phase 1: VOD movies (pending + retryable skipped items)
        from sqlalchemy import or_
        result = await db.execute(
            select(EnrichmentQueue)
            .where(
                or_(
                    EnrichmentQueue.status == "pending",
                    # Retry previously skipped items if under max attempts
                    (EnrichmentQueue.status == "skipped") & (EnrichmentQueue.attempts < MAX_ATTEMPTS),
                ),
                EnrichmentQueue.media_type == "movie",
            )
            .order_by(EnrichmentQueue.created_at)
            .limit(daily_limit)
        )
        pending_vod = list(result.scalars().all())
        logger.info(f"Enrichment Phase 1: {len(pending_vod)} pending movies")

        # Process in batches with parallel HTTP, sequential DB writes
        for batch_start in range(0, len(pending_vod), BATCH_SIZE):
            if used() >= daily_limit:
                logger.info(f"Enrichment: real TMDB call budget ({daily_limit}) reached, stopping VOD phase")
                break
            batch_end = min(batch_start + BATCH_SIZE, len(pending_vod))
            batch = pending_vod[batch_start:batch_end]

            tasks = [
                _fetch_movie_data(item, semaphore)
                for item in batch
            ]
            results = await asyncio.gather(*tasks)

            before = used()
            await _apply_enrichment_results(db, results)
            await commit_with_retry(db)

            logger.info(f"Enrichment VOD {batch_end}/{len(pending_vod)} "
                       f"({used() - before} real TMDB calls this batch, {used()} total)")

        # Phase 2: Series
        remaining = daily_limit - used()
        if remaining > 0:
            result = await db.execute(
                select(EnrichmentQueue)
                .where(
                    or_(
                        EnrichmentQueue.status == "pending",
                        (EnrichmentQueue.status == "skipped") & (EnrichmentQueue.attempts < MAX_ATTEMPTS),
                    ),
                    EnrichmentQueue.media_type == "show",
                )
                .order_by(EnrichmentQueue.created_at)
                .limit(remaining)
            )
            pending_series = list(result.scalars().all())
            logger.info(f"Enrichment Phase 2: {len(pending_series)} pending series")

            for batch_start in range(0, len(pending_series), BATCH_SIZE):
                if used() >= daily_limit:
                    logger.info(f"Enrichment: real TMDB call budget ({daily_limit}) reached, stopping series phase")
                    break
                batch_end = min(batch_start + BATCH_SIZE, len(pending_series))
                batch = pending_series[batch_start:batch_end]

                tasks = [_fetch_series_data(item, semaphore) for item in batch]
                results = await asyncio.gather(*tasks)

                before = used()
                await _apply_enrichment_results(db, results)
                await commit_with_retry(db)

                logger.info(f"Enrichment Series {batch_end}/{len(pending_series)} "
                           f"({used() - before} real TMDB calls this batch, {used()} total)")

    logger.info(f"Enrichment batch complete: {used()} real TMDB HTTP calls used")

    # Update queue-size gauges so Prometheus reflects post-batch state.
    from app.utils.metrics import enrichment_queue_size
    from sqlalchemy import func as _sql_func
    async with async_session_factory() as db:
        rows = await db.execute(
            select(EnrichmentQueue.status, _sql_func.count())
            .group_by(EnrichmentQueue.status)
        )
        for status, count in rows.all():
            enrichment_queue_size.labels(status=status).set(count)
