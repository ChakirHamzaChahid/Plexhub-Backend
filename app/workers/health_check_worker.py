import asyncio
import logging

import httpx
from sqlalchemy import select, update, func, or_

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, XtreamAccount
from app.services.stream_service import build_stream_url
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.health_check")

# Known video container magic bytes
_MPEG_TS_SYNC = 0x47
_MP4_FTYP = b"ftyp"
_MKV_EBML = b"\x1a\x45\xdf\xa3"
_AVI_RIFF = b"RIFF"


def _looks_like_video(data: bytes) -> bool:
    """Quick heuristic: does the payload look like a video stream?"""
    if len(data) < 8:
        return False
    if data[0] == _MPEG_TS_SYNC:
        return True
    if data[4:8] == _MP4_FTYP:
        return True
    if data[:4] == _MKV_EBML:
        return True
    if data[:4] == _AVI_RIFF:
        return True
    # Large enough response from a 200/206 — assume valid even if format unknown
    if len(data) >= 1024:
        return True
    return False


async def _check_one(client: httpx.AsyncClient, item, account, semaphore):
    """Check a single stream URL using Range request with HEAD fallback.

    Returns (item, is_broken).
    """
    url = build_stream_url(account, item.rating_key)
    if not url:
        return item, None  # Skip (no URL constructable)

    async with semaphore:
        try:
            # Use streaming GET with Range header to avoid downloading a full file
            # if the server ignores the Range header
            async with client.stream(
                "GET",
                url,
                headers={"Range": "bytes=0-8191"},
                follow_redirects=True,
            ) as resp:
                if resp.status_code in (200, 206):
                    # Read only the first chunk (up to 8 KB)
                    content = b""
                    async for chunk in resp.aiter_bytes(8192):
                        content = chunk
                        break  # Only read first chunk
                    if len(content) > 0:
                        return item, not _looks_like_video(content)
                    return item, True  # Empty response = broken
                elif resp.status_code == 416:
                    pass  # Fall through to HEAD fallback
                else:
                    return item, resp.status_code >= 400

            # HEAD fallback (for 416 Range Not Satisfiable)
            head_resp = await client.head(url, follow_redirects=True)
            return item, head_resp.status_code >= 400
        except (httpx.TimeoutException, httpx.ConnectError):
            return item, True
        except Exception as e:
            logger.warning(f"Health check error for {item.rating_key}: {e}")
            return item, True


async def run():
    """Check a batch of stream URLs for availability (cron job, random sample)."""
    batch_size = settings.HEALTH_CHECK_BATCH_SIZE
    timeout = float(settings.STREAM_VALIDATION_TIMEOUT)
    concurrency = settings.STREAM_VALIDATION_CONCURRENCY
    cutoff = now_ms() - 7 * 24 * 3600 * 1000  # 7 days ago

    logger.info(f"Starting health check (batch size: {batch_size}, concurrency: {concurrency})")

    async with async_session_factory() as db:
        # Get random batch of streams not checked in 7 days
        result = await db.execute(
            select(Media)
            .where(
                Media.server_id.like("xtream_%"),
                Media.type.in_(["movie", "episode"]),
                or_(
                    Media.last_stream_check.is_(None),
                    Media.last_stream_check < cutoff,
                ),
            )
            .order_by(func.random())
            .limit(batch_size)
        )
        items = list(result.scalars().all())

        if not items:
            logger.info("No streams to check")
            return

        # Pre-load all needed accounts in one query
        account_ids = {item.server_id.replace("xtream_", "") for item in items}
        acc_result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id.in_(list(account_ids)))
        )
        accounts = {acc.id: acc for acc in acc_result.scalars().all()}

        semaphore = asyncio.Semaphore(concurrency)
        checked = 0
        broken_count = 0

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Build tasks for all items
            tasks = []
            for item in items:
                account_id = item.server_id.replace("xtream_", "")
                account = accounts.get(account_id)
                if not account:
                    continue
                tasks.append(_check_one(client, item, account, semaphore))

            # Run all health checks concurrently
            results = await asyncio.gather(*tasks)

            # Apply results to DB
            for item, is_broken in results:
                if is_broken is None:
                    continue
                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                    )
                    .values(
                        is_broken=is_broken,
                        last_stream_check=now_ms(),
                        stream_error_count=(
                            Media.stream_error_count + 1 if is_broken else 0
                        ),
                    )
                )
                checked += 1
                if is_broken:
                    broken_count += 1

        await db.commit()

    logger.info(
        f"Health check complete: {checked} checked, {broken_count} broken"
    )


async def run_pipeline_validation():
    """Validate unchecked/stale streams as part of the sync pipeline.

    Unlike the cron-based run() which samples randomly, this targets
    streams that have never been checked or whose check is stale.
    Ensures new content is validated before Plex file generation.
    """
    if not settings.STREAM_VALIDATION_ENABLED:
        logger.info("Stream validation disabled (STREAM_VALIDATION_ENABLED=false)")
        return

    concurrency = settings.STREAM_VALIDATION_CONCURRENCY
    timeout = float(settings.STREAM_VALIDATION_TIMEOUT)
    recheck_cutoff = now_ms() - settings.STREAM_VALIDATION_RECHECK_HOURS * 3600 * 1000

    logger.info(
        f"Starting pipeline stream validation "
        f"(concurrency: {concurrency}, timeout: {timeout}s, "
        f"recheck window: {settings.STREAM_VALIDATION_RECHECK_HOURS}h)"
    )

    total_checked = 0
    total_broken = 0
    total_recovered = 0

    async with async_session_factory() as db:
        # Find all unchecked or stale streams (movies + episodes only)
        result = await db.execute(
            select(Media)
            .where(
                Media.server_id.like("xtream_%"),
                Media.type.in_(["movie", "episode"]),
                Media.is_in_allowed_categories == True,
                or_(
                    Media.last_stream_check.is_(None),
                    Media.last_stream_check < recheck_cutoff,
                ),
            )
            .order_by(Media.last_stream_check.asc().nullsfirst())
        )
        items = list(result.scalars().all())

        if not items:
            logger.info("No streams need validation")
            return

        logger.info(f"Found {len(items)} streams to validate")

        # Pre-load all needed accounts
        account_ids = {item.server_id.replace("xtream_", "") for item in items}
        acc_result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id.in_(list(account_ids)))
        )
        accounts = {acc.id: acc for acc in acc_result.scalars().all()}

        semaphore = asyncio.Semaphore(concurrency)
        commit_interval = 200  # Commit and log every N results
        log_interval = 50  # Log progress every N results

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Build all tasks
            tasks = []
            for item in items:
                account_id = item.server_id.replace("xtream_", "")
                account = accounts.get(account_id)
                if not account:
                    continue
                tasks.append(asyncio.ensure_future(
                    _check_one(client, item, account, semaphore)
                ))

            # Process results as they complete (real-time feedback)
            pending_updates = 0
            for coro in asyncio.as_completed(tasks):
                item, is_broken = await coro
                if is_broken is None:
                    continue

                was_broken = item.is_broken
                new_error_count = (
                    (item.stream_error_count or 0) + 1 if is_broken else 0
                )

                if was_broken and not is_broken:
                    total_recovered += 1

                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                    )
                    .values(
                        is_broken=is_broken,
                        last_stream_check=now_ms(),
                        stream_error_count=new_error_count,
                    )
                )
                total_checked += 1
                if is_broken:
                    total_broken += 1
                pending_updates += 1

                # Log progress frequently
                if total_checked % log_interval == 0:
                    logger.info(
                        f"Validation progress: {total_checked}/{len(items)} "
                        f"({total_broken} broken so far)"
                    )

                # Commit periodically to avoid huge transactions
                if pending_updates >= commit_interval:
                    await db.commit()
                    pending_updates = 0

            # Final commit for remaining updates
            if pending_updates > 0:
                await db.commit()

    logger.info(
        f"Pipeline validation complete: {total_checked} checked, "
        f"{total_broken} broken, {total_recovered} recovered"
    )
