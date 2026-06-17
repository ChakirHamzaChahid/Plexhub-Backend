from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.database import XtreamAccount
from app.models.schemas import HealthResponse
from app.services.media_service import media_service

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request, db: AsyncSession = Depends(get_db)):
    stats = await media_service.get_stats(db)

    # Count accounts
    acc_result = await db.execute(
        select(func.count()).select_from(XtreamAccount)
    )
    account_count = acc_result.scalar() or 0

    # Last sync time
    last_sync_result = await db.execute(
        select(func.max(XtreamAccount.last_synced_at))
    )
    last_sync = last_sync_result.scalar()

    return HealthResponse(
        status="ok",
        # Report the live app version (= APP_VERSION, passed to FastAPI(version=...)
        # in app/main.py) instead of a hardcoded literal that silently drifts.
        version=request.app.version,
        accounts=account_count,
        total_media=stats["total_media"],
        enriched_media=stats["enriched_media"],
        broken_streams=stats["broken_streams"],
        last_sync_at=last_sync,
    )
