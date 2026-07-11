import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.database import get_db
from app.models.database import LiveChannel, EpgEntry, XtreamAccount
from app.models.schemas import (
    LiveChannelResponse,
    LiveChannelListResponse,
    EpgEntryResponse,
    EpgListResponse,
    StreamResponse,
)
from app.services import live_service
from app.services.xtream_service import xtream_service
from app.utils.db_retry import commit_with_retry
from app.utils.server_id import parse_server_id

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
    query = select(LiveChannel).where(
        LiveChannel.is_in_allowed_categories == True,
    )

    if server_id:
        query = query.where(LiveChannel.server_id == server_id)
    if category_id:
        query = query.where(LiveChannel.category_id == category_id)
    if search:
        # Escape LIKE wildcards to prevent pattern injection
        safe_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(LiveChannel.name.ilike(f"%{safe_search}%", escape="\\"))

    # Count.
    # CR-P03: narrow func.count() over the base table with the SAME filters,
    # instead of wrapping a `SELECT *` subquery — avoids materializing every
    # matched channel's columns just to count them (still a full scan because
    # of the leading-wildcard ILIKE search — see CR-P03 residual note, an
    # FTS5/trigram index would be the real fix).
    count_query = select(func.count()).select_from(LiveChannel)
    if query.whereclause is not None:
        count_query = count_query.where(query.whereclause)
    total = (await db.execute(count_query)).scalar() or 0

    # Sort
    if sort == "name_asc":
        query = query.order_by(LiveChannel.name_sortable.asc())
    elif sort == "name_desc":
        query = query.order_by(LiveChannel.name_sortable.desc())
    elif sort == "added_desc":
        query = query.order_by(LiveChannel.added_at.desc())
    elif sort == "added_asc":
        query = query.order_by(LiveChannel.added_at.asc())
    else:
        query = query.order_by(LiveChannel.name_sortable.asc())

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    items = list(result.scalars().all())

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
    result = await db.execute(
        select(LiveChannel).where(
            LiveChannel.stream_id == stream_id,
            LiveChannel.server_id == server_id,
        )
    )
    channel = result.scalars().first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    return LiveChannelResponse.model_validate(channel)


@router.get("/channels/{stream_id}/stream", response_model=StreamResponse)
async def get_channel_stream(
    stream_id: int,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Get the live stream URL for a channel."""
    account_id = parse_server_id(server_id)
    if account_id is None:
        raise HTTPException(400, "Invalid server_id format")

    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    # Verify channel exists
    ch_result = await db.execute(
        select(LiveChannel).where(
            LiveChannel.stream_id == stream_id,
            LiveChannel.server_id == server_id,
        )
    )
    channel = ch_result.scalars().first()
    if not channel:
        raise HTTPException(404, "Channel not found")

    ext = channel.container_extension or "ts"
    url = xtream_service.build_live_url(
        account.base_url, account.port,
        account.username, account.password,
        stream_id, ext,
    )
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
    # Try from DB first
    now_ms = int(time.time() * 1000)
    query = select(EpgEntry).where(
        EpgEntry.stream_id == stream_id,
        EpgEntry.server_id == server_id,
        EpgEntry.end_time >= now_ms,
    ).order_by(EpgEntry.start_time.asc())

    result = await db.execute(query)
    entries = list(result.scalars().all())

    if entries:
        return EpgListResponse(
            items=[EpgEntryResponse.model_validate(e) for e in entries],
            total=len(entries),
        )

    # Fetch from Xtream API + stage new EpgEntry rows (live_service.ingest_short_epg)
    try:
        new_entries = await live_service.ingest_short_epg(
            db, server_id, stream_id, fetched_at=now_ms,
        )
    except live_service.InvalidServerIdError:
        raise HTTPException(400, "Invalid server_id format")
    except live_service.AccountNotFoundError:
        raise HTTPException(404, "Account not found")

    if new_entries:
        # CR-C04: this write can race a long-running sync/validation holding
        # the single WAL writer — retry on "database is locked" instead of
        # surfacing a raw 500 to the client (no per-entry refresh needed).
        await commit_with_retry(db)  # Persist EPG entries

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

    query = select(EpgEntry).where(
        EpgEntry.server_id == server_id,
        EpgEntry.start_time <= now_ms,
        EpgEntry.end_time >= now_ms,
    ).order_by(EpgEntry.start_time.asc())

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    entries = list(result.scalars().all())

    return EpgListResponse(
        items=[EpgEntryResponse.model_validate(e) for e in entries],
        total=total,
    )
