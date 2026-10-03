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

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import EpgEntry, XtreamAccount
from app.services.xtream_service import xtream_service
from app.utils.server_id import parse_server_id

logger = logging.getLogger("plexhub.services.live")


class InvalidServerIdError(Exception):
    """Raised when server_id doesn't match the 'xtream_{account_id}' format."""


class AccountNotFoundError(Exception):
    """Raised when the parsed account_id has no matching XtreamAccount."""


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


async def ingest_short_epg(
    db: AsyncSession,
    server_id: str,
    stream_id: int,
    fetched_at: int,
) -> list[EpgEntry]:
    """Fetch the short EPG for a channel from Xtream and upsert its EpgEntry rows.

    ``fetched_at`` is passed in (not computed here) so it matches the exact
    timestamp the router used for its DB-cache freshness check — same value,
    same semantics as before the extraction.

    On a provider fetch failure, logs a warning and returns ``[]`` — mirrors
    the previous inline behavior of degrading to an empty EPG list instead of
    surfacing a 500.

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
    # One row per start_time: a payload that repeats a start_time keeps the
    # last occurrence (the upsert would converge on it anyway; collapsing here
    # keeps the multi-row INSERT free of self-conflicts).
    rows_by_start: dict[int, dict] = {}

    for listing in listings:
        if not isinstance(listing, dict):
            continue

        # Parse start/end times (Xtream returns epoch seconds or datetime strings)
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

        title = listing.get("title") or "Unknown"
        # Some providers base64 encode the title/description
        title = _try_base64_decode(title)

        description = listing.get("description") or ""
        description = _try_base64_decode(description)

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

    if not rows_by_start:
        return []

    stmt = sqlite_insert(EpgEntry).values(list(rows_by_start.values()))
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
    # populate_existing: if a conflicting row is already in this session's
    # identity map, RETURNING must refresh it rather than hand back stale values.
    result = await db.execute(stmt, execution_options={"populate_existing": True})
    return sorted(result.scalars().all(), key=lambda e: e.start_time)
