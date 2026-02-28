import asyncio
import logging
import time

from sqlalchemy import select, update

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, EnrichmentQueue, XtreamAccount
from app.services.tmdb_service import tmdb_service

logger = logging.getLogger("plexhub.enrichment")

BATCH_SIZE = 50  # Commit every N items
CONCURRENCY = 5  # Parallel TMDB requests


def now_ms() -> int:
    return int(time.time() * 1000)


async def _fetch_movie_data(item, semaphore):
    """Fetch TMDB data for a movie with optimized API usage.
    
    Handles 4 scenarios:
    1. Both IDs present: should be filtered out by enqueue (won't happen)
    2. TMDB present, IMDB absent: fetch external_ids only (1 API call)
    3. IMDB present, TMDB absent: could search but skip for now (complex)
    4. Both absent: full search + external_ids (2 API calls)
    """
    async with semaphore:
        try:
            existing_tmdb = item.existing_tmdb_id
            existing_imdb = item.existing_imdb_id
            tmdb_id = None
            imdb_id = None
            confidence = None
            api_used = 0

            # Scenario 2: TMDB present, IMDB absent - fetch external_ids only
            if existing_tmdb and not existing_imdb:
                tmdb_id = int(existing_tmdb) if str(existing_tmdb).isdigit() else None
                if tmdb_id:
                    ext_ids = await tmdb_service.get_movie_external_ids(tmdb_id)
                    imdb_id = ext_ids.get("imdb_id")
                    api_used = 1
                    confidence = 1.0
                    return item, tmdb_id, imdb_id, confidence, api_used

            # Scenario 3: IMDB present, TMDB absent - keep IMDB, skip TMDB search for now
            if existing_imdb and not existing_tmdb:
                imdb_id = existing_imdb
                # Could implement TMDB find by IMDB in future, but skip for now
                return item, None, imdb_id, None, 0

            # Scenario 4: Both absent - full search
            if tmdb_service.is_configured:
                match = await tmdb_service.search_movie(item.title, item.year)
                if match and match.confidence >= 0.85:
                    tmdb_id = match.tmdb_id
                    ext_ids = await tmdb_service.get_movie_external_ids(tmdb_id)
                    imdb_id = ext_ids.get("imdb_id")
                    confidence = match.confidence
                    api_used = 2  # search + external_ids

            return item, tmdb_id, imdb_id, confidence, api_used
        except Exception as e:
            logger.debug(f"Enrichment fetch failed for {item.rating_key}: {e}")
            return item, None, None, None, 0


async def _fetch_series_data(item, semaphore):
    """Fetch TMDB data for a series with optimized API usage.
    
    Similar to movies, handles partial ID scenarios.
    """
    async with semaphore:
        try:
            existing_tmdb = item.existing_tmdb_id
            existing_imdb = item.existing_imdb_id
            tmdb_id = None
            imdb_id = None
            confidence = None
            api_used = 0

            # Scenario 2: TMDB present, IMDB absent - fetch external_ids only
            if existing_tmdb and not existing_imdb:
                tmdb_id = int(existing_tmdb) if str(existing_tmdb).isdigit() else None
                if tmdb_id:
                    ext_ids = await tmdb_service.get_tv_external_ids(tmdb_id)
                    imdb_id = ext_ids.get("imdb_id")
                    api_used = 1
                    confidence = 1.0
                    return item, tmdb_id, imdb_id, confidence, api_used

            # Scenario 3: IMDB present, TMDB absent - keep IMDB
            if existing_imdb and not existing_tmdb:
                imdb_id = existing_imdb
                return item, None, imdb_id, None, 0

            # Scenario 4: Both absent - full search
            if tmdb_service.is_configured:
                match = await tmdb_service.search_tv(item.title, item.year)
                if match and match.confidence >= 0.85:
                    tmdb_id = match.tmdb_id
                    ext_ids = await tmdb_service.get_tv_external_ids(tmdb_id)
                    imdb_id = ext_ids.get("imdb_id")
                    confidence = match.confidence
                    api_used = 2

            return item, tmdb_id, imdb_id, confidence, api_used
        except Exception as e:
            logger.debug(f"Enrichment fetch failed for series '{item.title}': {e}")
            return item, None, None, None, 0
async def _apply_enrichment_results(db, results):
    """Apply enrichment results to DB (sequential writes)."""
    batch_used = 0
    for item, tmdb_id, imdb_id, confidence, api_used in results:
        if tmdb_id:
            new_unif = f"imdb://{imdb_id}" if imdb_id else f"tmdb://{tmdb_id}"
            await db.execute(
                update(Media)
                .where(
                    Media.rating_key == item.rating_key,
                    Media.server_id == item.server_id,
                )
                .values(
                    tmdb_id=str(tmdb_id),
                    imdb_id=imdb_id,
                    unification_id=new_unif,
                    history_group_key=new_unif,
                    tmdb_match_confidence=confidence,
                )
            )
            item.status = "done"
        else:
            item.status = "skipped"
        item.attempts += 1
        item.processed_at = now_ms()
        batch_used += api_used
    return batch_used


async def run():
    """Run enrichment batch with parallel TMDB fetching."""
    daily_limit = settings.ENRICHMENT_DAILY_LIMIT
    used = 0
    semaphore = asyncio.Semaphore(CONCURRENCY)

    logger.info(f"Starting enrichment batch (daily limit: {daily_limit}, concurrency: {CONCURRENCY})")

    async with async_session_factory() as db:
        # Phase 1: VOD movies
        result = await db.execute(
            select(EnrichmentQueue)
            .where(
                EnrichmentQueue.status == "pending",
                EnrichmentQueue.media_type == "movie",
            )
            .order_by(EnrichmentQueue.created_at)
            .limit(daily_limit)
        )
        pending_vod = list(result.scalars().all())
        logger.info(f"Enrichment Phase 1: {len(pending_vod)} pending movies")

        # Pre-load existing tmdb_ids from Media (set during sync)

        # Process in batches with parallel HTTP, sequential DB writes
        for batch_start in range(0, len(pending_vod), BATCH_SIZE):
            if used >= daily_limit:
                break
            batch_end = min(batch_start + BATCH_SIZE, len(pending_vod))
            batch = pending_vod[batch_start:batch_end]

            tasks = [
                _fetch_movie_data(
                    item,
                    tmdb_map.get((item.rating_key, item.server_id)),
                    semaphore,
                )
                for item in batch
            ]
            results = await asyncio.gather(*tasks)

            batch_used = await _apply_enrichment_results(db, results)
            used += batch_used
            await db.commit()

            logger.info(f"Enrichment VOD {batch_end}/{len(pending_vod)} "
                       f"({batch_used} API calls, {used} total)")

        # Phase 2: Series
        remaining = daily_limit - used
        if remaining > 0:
            result = await db.execute(
                select(EnrichmentQueue)
                .where(
                    EnrichmentQueue.status == "pending",
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
                await db.commit()

                logger.info(f"Enrichment Series {batch_end}/{len(pending_series)} "
                           f"({batch_used} API calls, {used} total)")

    logger.info(f"Enrichment batch complete: {used} TMDB API calls used")
