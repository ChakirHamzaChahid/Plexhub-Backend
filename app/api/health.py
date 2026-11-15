import asyncio
import logging
import time

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from app.db import database as db_module
from app.models.database import XtreamAccount
from app.models.schemas import HealthResponse
from app.services.media_service import media_service
from app.utils import job_health

logger = logging.getLogger("plexhub.api.health")
router = APIRouter(tags=["health"])

# CR-O01: Cache DB stats for 5 minutes so Docker's 30s HEALTHCHECK never
# pounds the API pool or runs 5 COUNT(*)/MAX scans on Media every 30s.
HEALTH_STATS_TTL_SECONDS = 300.0
_HEALTH_ERROR_BACKOFF_SECONDS = 15.0
_HEALTH_DB_TIMEOUT_SECONDS = 2.0

_EMPTY_STATS: dict[str, int | None] = {
    "accounts": 0,
    "total_media": 0,
    "enriched_media": 0,
    "broken_streams": 0,
    "last_sync_at": None,
}
# Keyed by id(async_session_factory) so per-test DB fixtures stay isolated.
# Value: (expires_at_monotonic, stats_dict)
_stats_cache: dict[int, tuple[float, dict[str, int | None]]] = {}
_refresh_locks: dict[int, asyncio.Lock] = {}


async def _fetch_health_stats() -> dict[str, int | None]:
    factory = db_module.async_session_factory
    async with factory() as db:
        stats = await media_service.get_stats(db)
        acc_result = await db.execute(
            select(func.count()).select_from(XtreamAccount)
        )
        account_count = acc_result.scalar() or 0
        last_sync_result = await db.execute(
            select(func.max(XtreamAccount.last_synced_at))
        )
        last_sync = last_sync_result.scalar()
        return {
            "accounts": account_count,
            "total_media": stats["total_media"],
            "enriched_media": stats["enriched_media"],
            "broken_streams": stats["broken_streams"],
            "last_sync_at": last_sync,
        }


async def _get_cached_health_stats() -> dict[str, int | None]:
    factory_key = id(db_module.async_session_factory)
    now = time.monotonic()
    cached = _stats_cache.get(factory_key)
    if cached is not None and now < cached[0]:
        return cached[1]

    lock = _refresh_locks.setdefault(factory_key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _stats_cache.get(factory_key)
        if cached is not None and now < cached[0]:
            return cached[1]

        try:
            fresh = await asyncio.wait_for(
                _fetch_health_stats(),
                timeout=_HEALTH_DB_TIMEOUT_SECONDS,
            )
            _stats_cache[factory_key] = (now + HEALTH_STATS_TTL_SECONDS, fresh)
            return fresh
        except Exception as exc:
            logger.warning("health stats refresh degraded (%s); serving fallback", type(exc).__name__)
            fallback = cached[1] if cached is not None else dict(_EMPTY_STATS)
            _stats_cache[factory_key] = (now + _HEALTH_ERROR_BACKOFF_SECONDS, fallback)
            return fallback


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request):
    stats = await _get_cached_health_stats()

    return HealthResponse(
        status="ok",
        # Report the live app version (= APP_VERSION, passed to FastAPI(version=...)
        # in app/main.py) instead of a hardcoded literal that silently drifts.
        version=request.app.version,
        accounts=stats["accounts"] or 0,
        total_media=stats["total_media"] or 0,
        enriched_media=stats["enriched_media"] or 0,
        broken_streams=stats["broken_streams"] or 0,
        last_sync_at=stats["last_sync_at"],
        # S5.1 (AUDIT-P8-005): additive, public-endpoint-safe (no path, no
        # host, no config — just a bool and a timestamp, house-law piège 10).
        is_master=job_health.is_master(),
        last_pipeline_success_at=job_health.last_success_ms("pipeline"),
    )
