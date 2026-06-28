import asyncio
import logging
from dataclasses import dataclass

import httpx

from sqlalchemy import func, select, update

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, EnrichmentQueue, XtreamAccount
from app.services import scrape_cache_service as scrape_cache
from app.services.tmdb_service import TMDBEnrichmentData, TMDBSearchOutcome, tmdb_service
from app.utils.time import now_ms
from app.utils.db_retry import commit_with_retry

logger = logging.getLogger("plexhub.enrichment")

BATCH_SIZE = 200  # Commit every N items
CONCURRENCY = 8   # Parallel TMDB requests (free tier ~4 req/s, keep headroom)
MAX_ATTEMPTS = 3  # Max enrichment attempts before permanently skipping


@dataclass
class FetchResult:
    item: EnrichmentQueue
    data: TMDBEnrichmentData | None
    confidence: float | None
    result: str            # matched | nomatch | ambiguous | skipped
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
        for fr in results:
            item = fr.item
            enrichment_data = fr.data

            # Outcome metric (only the genuine matching attempts — skip scenario 2/3).
            if fr.result in ("matched", "nomatch", "ambiguous"):
                metric_type = "movie" if item.media_type == "movie" else "tv"
                tmdb_match_total.labels(media_type=metric_type, result=fr.result).inc()

            # Persist the resolution so the same title is never re-queried.
            if fr.cache_key and not fr.from_cache:
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
    used = 0
    semaphore = asyncio.Semaphore(CONCURRENCY)

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
            if used >= daily_limit:
                break
            batch_end = min(batch_start + BATCH_SIZE, len(pending_vod))
            batch = pending_vod[batch_start:batch_end]

            tasks = [
                _fetch_movie_data(item, semaphore)
                for item in batch
            ]
            results = await asyncio.gather(*tasks)

            batch_used = await _apply_enrichment_results(db, results)
            used += batch_used
            await commit_with_retry(db)

            logger.info(f"Enrichment VOD {batch_end}/{len(pending_vod)} "
                       f"({batch_used} API calls, {used} total)")

        # Phase 2: Series
        remaining = daily_limit - used
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
                if used >= daily_limit:
                    break
                batch_end = min(batch_start + BATCH_SIZE, len(pending_series))
                batch = pending_series[batch_start:batch_end]

                tasks = [_fetch_series_data(item, semaphore) for item in batch]
                results = await asyncio.gather(*tasks)

                batch_used = await _apply_enrichment_results(db, results)
                used += batch_used
                await commit_with_retry(db)

                logger.info(f"Enrichment Series {batch_end}/{len(pending_series)} "
                           f"({batch_used} API calls, {used} total)")

    logger.info(f"Enrichment batch complete: {used} TMDB API calls used")

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
