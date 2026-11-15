"""Background startup and scheduled maintenance coroutines extracted from app/main.py (CR-A05)."""
from __future__ import annotations

import hashlib
import logging
import time

from sqlalchemy import delete, select

from app.config import settings
from app.db import database
from app.db.maintenance import run_sqlite_maintenance
from app.models.database import EpgEntry, XtreamAccount
from app.services import plex_generation_service, unified_group_service
from app.services.xtream_credentials import XtreamCredentials
from app.services.xtream_service import safe_xtream_error, xtream_service
from app.utils.db_retry import run_with_retry

logger = logging.getLogger("plexhub")


async def auto_generate_plex_library() -> None:
    """Auto-generate Plex library for all active accounts if PLEX_LIBRARY_DIR is set."""
    await plex_generation_service.generate_plex_library_auto()


async def rebuild_unified_groups() -> None:
    """Rebuild the precomputed media_group snapshot and refresh sqlite_stat1."""
    try:
        counts = await unified_group_service.rebuild_all(database.async_session_factory)
        logger.info("Unified-group snapshot rebuilt: %s", counts)
    except Exception as exc:
        logger.error("Unified-group snapshot rebuild failed: %s", exc, exc_info=True)

    await run_sqlite_maintenance(database.engine)


async def auto_provision_xtream_account() -> None:
    """Create an Xtream account from env vars if it doesn't already exist."""
    account_id = hashlib.md5(
        f"{settings.XTREAM_BASE_URL}{settings.XTREAM_USERNAME}".encode()
    ).hexdigest()[:8]

    async with database.async_session_factory() as db:
        result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id == account_id)
        )
        if result.scalars().first():
            logger.info(f"Xtream account {account_id} already exists (from env)")
            return

        credentials = XtreamCredentials(
            base_url=settings.XTREAM_BASE_URL,
            port=settings.XTREAM_PORT,
            username=settings.XTREAM_USERNAME,
            password=settings.XTREAM_PASSWORD,
        )

        try:
            auth_data = await xtream_service.authenticate(credentials)
            user_info = auth_data.get("user_info", {})
            server_info = auth_data.get("server_info", {})
        except Exception as exc:
            logger.error(f"Xtream auto-provision auth failed: {safe_xtream_error(exc)}")
            return

        account = XtreamAccount(
            id=account_id,
            label="Auto (env)",
            base_url=settings.XTREAM_BASE_URL,
            port=settings.XTREAM_PORT,
            username=settings.XTREAM_USERNAME,
            password=settings.XTREAM_PASSWORD,
            status=user_info.get("status", "Unknown"),
            expiration_date=int(user_info["exp_date"]) * 1000
            if user_info.get("exp_date")
            else None,
            max_connections=int(user_info.get("max_connections", 1)),
            allowed_formats=",".join(user_info.get("allowed_output_formats", [])),
            server_url=server_info.get("url"),
            https_port=int(server_info["https_port"])
            if server_info.get("https_port")
            else None,
            is_active=True,
            created_at=int(time.time() * 1000),
        )
        db.add(account)
        await db.commit()
        logger.info(f"Xtream account auto-provisioned from env: {account_id}")


async def cleanup_stale_epg() -> None:
    """Delete EPG entries whose end_time is in the past (stale programs)."""
    cutoff = int(time.time() * 1000)

    async def _prune() -> int:
        async with database.async_session_factory() as db:
            result = await db.execute(
                delete(EpgEntry).where(EpgEntry.end_time < cutoff)
            )
            await db.commit()
            return result.rowcount

    rowcount = await run_with_retry(_prune, op="epg_cleanup")
    logger.info(f"EPG cleanup: removed {rowcount} stale entries")
