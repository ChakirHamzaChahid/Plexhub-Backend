import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.schemas import (
    EpgEntryResponse,
    EpgListResponse,
    LiveChannelListResponse,
    LiveChannelResponse,
    StreamResponse,
)
from app.services import live_service
from app.utils.db_retry import session_factory_for, write_with_retry

logger = logging.getLogger("plexhub.live")

router = APIRouter(prefix="/live", tags=["live"])


@router.get("/channels", response_model=LiveChannelListResponse)
async def list_channels(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("name_asc"),
    server_id: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List live TV channels with pagination, filtering, and search."""
    items, total = await live_service.list_channels(
        db,
        limit=limit,
        offset=offset,
        sort=sort,
        server_id=server_id,
        category_id=category_id,
        search=search,
    )
    return LiveChannelListResponse(
        items=[LiveChannelResponse.model_validate(ch) for ch in items],
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/channels/{stream_id}", response_model=LiveChannelResponse)
async def get_channel(
    stream_id: int,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Get a single live channel by stream_id."""
    try:
        channel = await live_service.get_channel(db, stream_id, server_id)
    except live_service.ChannelNotFoundError:
        raise HTTPException(404, "Channel not found")
    return LiveChannelResponse.model_validate(channel)


@router.get("/channels/{stream_id}/stream", response_model=StreamResponse)
async def get_channel_stream(
    stream_id: int,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Get the live stream URL for a channel."""
    try:
        url = await live_service.resolve_live_stream_url(db, stream_id, server_id)
    except live_service.InvalidServerIdError:
        raise HTTPException(400, "Invalid server_id format")
    except live_service.AccountNotFoundError:
        raise HTTPException(404, "Account not found")
    except live_service.ChannelNotFoundError:
        raise HTTPException(404, "Channel not found")
    return StreamResponse(url=url)


@router.get("/channels/{stream_id}/epg", response_model=EpgListResponse)
async def get_channel_epg(
    stream_id: int,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Get EPG entries for a specific channel.

    First checks local DB cache. If empty, fetches from Xtream short EPG
    and caches the results.
    """
    now_ms = int(time.time() * 1000)
    entries = await live_service.list_channel_epg_cached(db, stream_id, server_id, now_ms)
    if entries:
        return EpgListResponse(
            items=[EpgEntryResponse.model_validate(e) for e in entries],
            total=len(entries),
        )

    # Fetch from Xtream API first (outside write_with_retry so lock retries
    # never replay the upstream Xtream HTTP request).
    try:
        rows = await live_service.fetch_short_epg_rows(
            db, server_id, stream_id, fetched_at=now_ms,
        )
    except live_service.InvalidServerIdError:
        raise HTTPException(400, "Invalid server_id format")
    except live_service.AccountNotFoundError:
        raise HTTPException(404, "Account not found")

    new_entries = []
    if rows:
        async def _work(session: AsyncSession):
            entries = await live_service.upsert_short_epg_rows(session, rows)
            await session.commit()
            return entries

        new_entries = await write_with_retry(
            _work,
            session_factory=session_factory_for(db),
            op="live.ingest_short_epg",
        )

    return EpgListResponse(
        items=[EpgEntryResponse.model_validate(e) for e in new_entries],
        total=len(new_entries),
    )


@router.get("/epg", response_model=EpgListResponse)
async def get_epg_now(
    server_id: str = Query(...),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Get currently airing EPG entries across all channels for a server."""
    now_ms = int(time.time() * 1000)
    entries, total = await live_service.list_epg_now(
        db, server_id, now_ms, limit=limit, offset=offset,
    )
    return EpgListResponse(
        items=[EpgEntryResponse.model_validate(e) for e in entries],
        total=total,
    )
