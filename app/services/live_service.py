"""Live TV EPG ingest: fetch the short EPG from Xtream and stage EpgEntry rows.

Extracted from app/api/live.py (CR-A01) — the router validates the request,
runs the DB-cache read itself (a plain parameterized SELECT, not business
logic), and delegates the provider fetch + parse + persist to this module
when the cache is empty.

The caller owns the transaction boundary: ``ingest_short_epg`` executes its
upsert on the caller's session but does not commit — the router still calls
``commit_with_retry`` itself (CR-C04), unchanged.

Lot 10 sub-lot D (FR-BE-554): the write is an UPSERT on the dedup key
``(server_id, stream_id, start_time)`` (``uix_epg_dedup``, guaranteed by
migration 028), no longer a bare ``db.add``. Two concurrent cache misses for the
same channel therefore converge on one row per programme instead of raising
``IntegrityError`` or duplicating. The same key is shared with the bulk sync in
``epg_sync_service``, so the two ingestion paths never duplicate each other.
"""
from __future__ import annotations

import base64
import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import EpgEntry, LiveChannel, XtreamAccount
from app.services.xtream_service import xtream_service
from app.utils.server_id import parse_server_id

logger = logging.getLogger("plexhub.services.live")


class InvalidServerIdError(Exception):
    """Raised when server_id doesn't match the 'xtream_{account_id}' format."""


class AccountNotFoundError(Exception):
    """Raised when the parsed account_id has no matching XtreamAccount."""


class ChannelNotFoundError(Exception):
    """Raised when no LiveChannel matches `(server_id, stream_id)`."""


def _try_base64_decode(value: str) -> str:
    """Decode base64 only if the result is valid readable UTF-8 text."""
    if not value:
        return value
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8", errors="replace")
        # Reject if decoded text contains control chars (likely not real text)
        if any(ord(c) < 32 and c not in "\n\r\t" for c in decoded):
            return value
        # Reject if decoding produced the Unicode replacement char: the bytes
        # were not valid UTF-8, so this wasn't real base64-encoded text either.
        if "�" in decoded:
            return value
        return decoded
    except Exception:
        return value  # not base64, use as-is


async def fetch_short_epg_rows(
    db: AsyncSession,
    server_id: str,
    stream_id: int,
    fetched_at: int,
) -> list[dict]:
    """Fetch and parse short EPG rows from Xtream without writing to the DB.

    Raises:
        InvalidServerIdError: server_id isn't 'xtream_{account_id}'.
        AccountNotFoundError: no XtreamAccount for the parsed account_id.
    """
    account_id = parse_server_id(server_id)
    if account_id is None:
        raise InvalidServerIdError(server_id)

    acc_result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = acc_result.scalars().first()
    if not account:
        raise AccountNotFoundError(account_id)

    try:
        epg_data = await xtream_service.get_short_epg(account, stream_id=stream_id)
    except Exception as e:
        # Never interpolate `e`: an httpx HTTPStatusError message embeds the full
        # request URL, i.e. the Xtream username and password.
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning(
            "Failed to fetch EPG for stream %s: %s%s", stream_id, type(e).__name__,
            f" (HTTP {status})" if status else "",
        )
        return []

    listings = epg_data.get("epg_listings") or []
    rows_by_start: dict[int, dict] = {}

    for listing in listings:
        if not isinstance(listing, dict):
            continue

        start = listing.get("start_timestamp") or listing.get("start")
        end = listing.get("stop_timestamp") or listing.get("end")

        if start and str(start).isdigit():
            start_ms = int(start) * 1000
        else:
            start_ms = 0
        if end and str(end).isdigit():
            end_ms = int(end) * 1000
        else:
            end_ms = 0

        if not start_ms:
            continue

        title = _try_base64_decode(listing.get("title") or "Unknown")
        description = _try_base64_decode(listing.get("description") or "")

        rows_by_start[start_ms] = {
            "server_id": server_id,
            "epg_channel_id": listing.get("epg_id") or "",
            "stream_id": stream_id,
            "title": title,
            "description": description or None,
            "start_time": start_ms,
            "end_time": end_ms,
            "lang": listing.get("lang"),
            "fetched_at": fetched_at,
        }

    return list(rows_by_start.values())


async def upsert_short_epg_rows(
    db: AsyncSession,
    rows: list[dict],
) -> list[EpgEntry]:
    """Upsert parsed short EPG rows on ``db`` without committing."""
    if not rows:
        return []

    stmt = sqlite_insert(EpgEntry).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["server_id", "stream_id", "start_time"],
        set_={
            col: stmt.excluded[col]
            for col in (
                "epg_channel_id", "title", "description", "end_time", "lang",
                "fetched_at",
            )
        },
    ).returning(EpgEntry)
    result = await db.execute(stmt, execution_options={"populate_existing": True})
    return sorted(result.scalars().all(), key=lambda e: e.start_time)


async def ingest_short_epg(
    db: AsyncSession,
    server_id: str,
    stream_id: int,
    fetched_at: int,
) -> list[EpgEntry]:
    """Fetch the short EPG for a channel from Xtream and upsert its EpgEntry rows.

    Raises:
        InvalidServerIdError: server_id isn't 'xtream_{account_id}'.
        AccountNotFoundError: no XtreamAccount for the parsed account_id.
    """
    rows = await fetch_short_epg_rows(db, server_id, stream_id, fetched_at)
    return await upsert_short_epg_rows(db, rows)


async def list_channels(
    db: AsyncSession,
    *,
    limit: int,
    offset: int,
    sort: str,
    server_id: str | None = None,
    category_id: str | None = None,
    search: str | None = None,
) -> tuple[list[LiveChannel], int]:
    """List live TV channels with pagination, filtering, and search."""
    query = select(LiveChannel).where(
        LiveChannel.is_in_allowed_categories == True,
    )
    if server_id:
        query = query.where(LiveChannel.server_id == server_id)
    if category_id:
        query = query.where(LiveChannel.category_id == category_id)
    if search:
        safe_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(LiveChannel.name.ilike(f"%{safe_search}%", escape="\\"))

    count_query = select(func.count()).select_from(LiveChannel)
    if query.whereclause is not None:
        count_query = count_query.where(query.whereclause)
    total = (await db.execute(count_query)).scalar() or 0

    if sort == "name_desc":
        query = query.order_by(LiveChannel.name_sortable.desc())
    elif sort == "added_desc":
        query = query.order_by(LiveChannel.added_at.desc())
    elif sort == "added_asc":
        query = query.order_by(LiveChannel.added_at.asc())
    else:
        query = query.order_by(LiveChannel.name_sortable.asc())

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all()), total


async def get_channel(
    db: AsyncSession, stream_id: int, server_id: str,
) -> LiveChannel:
    """Return a single live channel or raise ChannelNotFoundError."""
    result = await db.execute(
        select(LiveChannel).where(
            LiveChannel.stream_id == stream_id,
            LiveChannel.server_id == server_id,
        )
    )
    channel = result.scalars().first()
    if not channel:
        raise ChannelNotFoundError(stream_id)
    return channel


async def resolve_live_stream_url(
    db: AsyncSession, stream_id: int, server_id: str,
) -> str:
    """Build the direct live stream URL for `(server_id, stream_id)`."""
    account_id = parse_server_id(server_id)
    if account_id is None:
        raise InvalidServerIdError(server_id)

    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise AccountNotFoundError(account_id)

    channel = await get_channel(db, stream_id, server_id)
    ext = channel.container_extension or "ts"
    return xtream_service.build_live_url(
        account.base_url, account.port,
        account.username, account.password,
        stream_id, ext,
    )


async def list_channel_epg_cached(
    db: AsyncSession, stream_id: int, server_id: str, now_ms: int,
) -> list[EpgEntry]:
    """Return future/active EPG entries cached in DB for a channel."""
    query = select(EpgEntry).where(
        EpgEntry.stream_id == stream_id,
        EpgEntry.server_id == server_id,
        EpgEntry.end_time >= now_ms,
    ).order_by(EpgEntry.start_time.asc())
    result = await db.execute(query)
    return list(result.scalars().all())


async def list_epg_now(
    db: AsyncSession, server_id: str, now_ms: int, *, limit: int, offset: int,
) -> tuple[list[EpgEntry], int]:
    """Return currently airing EPG entries across all channels for a server."""
    where = (
        EpgEntry.server_id == server_id,
        EpgEntry.start_time <= now_ms,
        EpgEntry.end_time >= now_ms,
    )
    total = (
        await db.execute(select(func.count()).select_from(EpgEntry).where(*where))
    ).scalar() or 0
    query = (
        select(EpgEntry)
        .where(*where)
        .order_by(EpgEntry.start_time.asc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(query)
    return list(result.scalars().all()), total
