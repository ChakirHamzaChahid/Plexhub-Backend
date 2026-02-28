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

    Returns (item, enrichment_data, confidence, api_used) where
    enrichment_data is a TMDBEnrichmentData or None.

    Handles 4 scenarios:
    1. Both IDs present: should be filtered out by enqueue (won't happen)
    2. TMDB present, IMDB absent: fetch details+external_ids (1 API call)
    3. IMDB present, TMDB absent: keep IMDB, skip for now
    4. Both absent: search + details+external_ids (2 API calls)
    """
    async with semaphore:
        try:
            existing_tmdb = item.existing_tmdb_id
            existing_imdb = item.existing_imdb_id

            # Scenario 2: TMDB present, IMDB absent — get full details in 1 call
            if existing_tmdb and not existing_imdb:
                tmdb_id = int(existing_tmdb) if str(existing_tmdb).isdigit() else None
                if tmdb_id:
                    details = await tmdb_service.get_movie_details(tmdb_id)
                    return item, details, 1.0, 1
                return item, None, None, 0

            # Scenario 3: IMDB present, TMDB absent — keep IMDB, skip search
            if existing_imdb and not existing_tmdb:
                return item, None, None, 0

            # Scenario 4: Both absent — full search + details
            if tmdb_service.is_configured:
                match = await tmdb_service.search_movie(item.title, item.year)
                if match and match.confidence >= 0.85:
                    details = await tmdb_service.get_movie_details(match.tmdb_id)
                    return item, details, match.confidence, 2
                return item, None, None, 1  # search call used, no match

            return item, None, None, 0
        except Exception as e:
            logger.debug(f"Enrichment fetch failed for {item.rating_key}: {e}")
            return item, None, None, 0


async def _fetch_series_data(item, semaphore):
    """Fetch TMDB data for a series with optimized API usage.

    Same pattern as movies but uses TV endpoints.
    """
    async with semaphore:
        try:
            existing_tmdb = item.existing_tmdb_id
            existing_imdb = item.existing_imdb_id

            # Scenario 2: TMDB present, IMDB absent — get full details in 1 call
            if existing_tmdb and not existing_imdb:
                tmdb_id = int(existing_tmdb) if str(existing_tmdb).isdigit() else None
                if tmdb_id:
                    details = await tmdb_service.get_tv_details(tmdb_id)
                    return item, details, 1.0, 1
                return item, None, None, 0

            # Scenario 3: IMDB present, TMDB absent — keep IMDB
            if existing_imdb and not existing_tmdb:
                return item, None, None, 0

            # Scenario 4: Both absent — full search + details
            if tmdb_service.is_configured:
                match = await tmdb_service.search_tv(item.title, item.year)
                if match and match.confidence >= 0.85:
                    details = await tmdb_service.get_tv_details(match.tmdb_id)
                    return item, details, match.confidence, 2
                return item, None, None, 1

            return item, None, None, 0
        except Exception as e:
            logger.debug(f"Enrichment fetch failed for series '{item.title}': {e}")
            return item, None, None, 0


async def _apply_enrichment_results(db, results):
    """Apply enrichment results to DB — IDs + rich metadata from TMDB."""
    batch_used = 0
    for item, enrichment_data, confidence, api_used in results:
        if enrichment_data:
            tmdb_id = enrichment_data.tmdb_id
            imdb_id = enrichment_data.imdb_id
            new_unif = f"imdb://{imdb_id}" if imdb_id else f"tmdb://{tmdb_id}"

            # Build update dict: always set IDs + unification
            update_values = {
                "tmdb_id": str(tmdb_id),
                "imdb_id": imdb_id,
                "unification_id": new_unif,
                "history_group_key": new_unif,
                "tmdb_match_confidence": confidence,
            }

            # Rich metadata: only fill in blanks (don't overwrite Xtream data)
            # We'll use a conditional update in a moment

            await db.execute(
                update(Media)
                .where(
                    Media.rating_key == item.rating_key,
                    Media.server_id == item.server_id,
                )
                .values(**update_values)
            )

            # Conditional updates: only set fields that are currently empty
            conditional_fields = {}
            if enrichment_data.overview:
                conditional_fields["summary"] = enrichment_data.overview
            if enrichment_data.genres:
                conditional_fields["genres"] = enrichment_data.genres
            if enrichment_data.poster_url:
                conditional_fields["resolved_thumb_url"] = enrichment_data.poster_url
            if enrichment_data.backdrop_url:
                conditional_fields["resolved_art_url"] = enrichment_data.backdrop_url
            if enrichment_data.vote_average:
                conditional_fields["scraped_rating"] = enrichment_data.vote_average
            if enrichment_data.year:
                conditional_fields["year"] = enrichment_data.year

            # Update summary/genres only if currently null
            if conditional_fields:
                for field, value in conditional_fields.items():
                    col = getattr(Media, field)
                    # Only update if current value is NULL or empty
                    await db.execute(
                        update(Media)
                        .where(
                            Media.rating_key == item.rating_key,
                            Media.server_id == item.server_id,
                            (col.is_(None)) | (col == ""),
                        )
                        .values(**{field: value})
                    )

            # Always update display_rating if currently 0 and TMDB has a rating
            if enrichment_data.vote_average:
                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                        Media.display_rating == 0.0,
                    )
                    .values(display_rating=enrichment_data.vote_average)
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
