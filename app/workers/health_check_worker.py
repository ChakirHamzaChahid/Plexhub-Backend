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

CONCURRENCY = 20  # Parallel HEAD requests


async def _check_one(client: httpx.AsyncClient, item, account, semaphore):
    """Check a single stream URL. Returns (item, is_broken)."""
    url = build_stream_url(account, item.rating_key)
    if not url:
        return item, None  # Skip

    async with semaphore:
        try:
            resp = await client.head(url, follow_redirects=True)
            return item, resp.status_code >= 400
        except (httpx.TimeoutException, httpx.ConnectError):
            return item, True
        except Exception as e:
            logger.warning(f"Health check error for {item.rating_key}: {e}")
            return item, True


async def run():
    """Check a batch of stream URLs for availability (concurrent)."""
    batch_size = settings.HEALTH_CHECK_BATCH_SIZE
    cutoff = now_ms() - 7 * 24 * 3600 * 1000  # 7 days ago

    logger.info(f"Starting health check (batch size: {batch_size}, concurrency: {CONCURRENCY})")

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

        semaphore = asyncio.Semaphore(CONCURRENCY)
        checked = 0
        broken_count = 0

        async with httpx.AsyncClient(timeout=5.0) as client:
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
