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


async def run():
    """Check a batch of stream URLs for availability."""
    batch_size = settings.HEALTH_CHECK_BATCH_SIZE
    cutoff = now_ms() - 7 * 24 * 3600 * 1000  # 7 days ago

    logger.info(f"Starting health check (batch size: {batch_size})")

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

        # Cache accounts
        accounts: dict[str, object] = {}
        checked = 0
        broken_count = 0

        async with httpx.AsyncClient(timeout=5.0) as client:
            for item in items:
                # Get account for this item
                account_id = item.server_id.replace("xtream_", "")
                if account_id not in accounts:
                    acc_result = await db.execute(
                        select(XtreamAccount).where(
                            XtreamAccount.id == account_id
                        )
                    )
                    accounts[account_id] = acc_result.scalars().first()

                account = accounts.get(account_id)
                if not account:
                    continue

                url = build_stream_url(account, item.rating_key)
                if not url:
                    continue

                is_broken = False
                try:
                    resp = await client.head(url, follow_redirects=True)
                    is_broken = resp.status_code >= 400
                except (httpx.TimeoutException, httpx.ConnectError):
                    is_broken = True
                except Exception as e:
                    logger.warning(f"Health check error for {item.rating_key}: {e}")
                    is_broken = True

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

                await asyncio.sleep(0.05)  # 50ms between probes

        await db.commit()

    logger.info(
        f"Health check complete: {checked} checked, {broken_count} broken"
    )
