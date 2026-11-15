import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import XtreamAccount
from app.services.xtream_service import xtream_service
from app.utils.server_id import parse_server_id

logger = logging.getLogger("plexhub.stream")


class InvalidStreamServerIdError(ValueError):
    """Raised when server_id does not match 'xtream_{account_id}'."""


class StreamAccountNotFoundError(LookupError):
    """Raised when the XtreamAccount for server_id does not exist."""


class UnbuildableStreamUrlError(ValueError):
    """Raised when a stream URL cannot be constructed for rating_key."""


def parse_rating_key(rating_key: str) -> dict:
    """
    Parse a rating_key into its components.

    vod_435071.mp4 -> {"type": "movie", "id": "435071", "ext": "mp4"}
    ep_7890.mkv    -> {"type": "episode", "id": "7890", "ext": "mkv"}
    vod_435071     -> {"type": "movie", "id": "435071", "ext": None}
    series_6581    -> {"type": "series", "id": "6581", "ext": None}
    """
    if rating_key.startswith("vod_"):
        remainder = rating_key[4:]
        parts = remainder.rsplit(".", 1)
        stream_id = parts[0]
        ext = parts[1] if len(parts) > 1 else None
        return {"type": "movie", "id": stream_id, "ext": ext}
    elif rating_key.startswith("ep_"):
        remainder = rating_key[3:]
        parts = remainder.rsplit(".", 1)
        ep_id = parts[0]
        ext = parts[1] if len(parts) > 1 else None
        return {"type": "episode", "id": ep_id, "ext": ext}
    elif rating_key.startswith("live_"):
        remainder = rating_key[5:]
        parts = remainder.rsplit(".", 1)
        stream_id = parts[0]
        ext = parts[1] if len(parts) > 1 else None
        return {"type": "live", "id": stream_id, "ext": ext}
    elif rating_key.startswith("series_"):
        return {"type": "series", "id": rating_key[7:], "ext": None}
    elif rating_key.startswith("season_"):
        return {"type": "season", "id": rating_key[7:], "ext": None}
    else:
        return {"type": "unknown", "id": rating_key, "ext": None}


def build_stream_url(account, rating_key: str) -> Optional[str]:
    """Build the direct stream URL for a given media item."""
    parsed = parse_rating_key(rating_key)

    if parsed["type"] == "movie":
        ext = parsed["ext"] or "ts"
        return xtream_service.build_movie_url(
            account.base_url, account.port,
            account.username, account.password,
            int(parsed["id"]), ext,
        )
    elif parsed["type"] == "episode":
        ext = parsed["ext"] or "ts"
        return xtream_service.build_episode_url(
            account.base_url, account.port,
            account.username, account.password,
            parsed["id"], ext,
        )
    elif parsed["type"] == "live":
        ext = parsed["ext"] or "ts"
        return xtream_service.build_live_url(
            account.base_url, account.port,
            account.username, account.password,
            int(parsed["id"]), ext,
        )
    else:
        logger.warning(f"Cannot build stream URL for type: {parsed['type']}")
        return None


async def resolve_stream_url(db: AsyncSession, rating_key: str, server_id: str) -> str:
    """Resolve the direct Xtream stream URL for `(server_id, rating_key)`."""
    account_id = parse_server_id(server_id)
    if account_id is None:
        raise InvalidStreamServerIdError("Invalid server_id format")

    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise StreamAccountNotFoundError("Account not found")

    url = build_stream_url(account, rating_key)
    if not url:
        raise UnbuildableStreamUrlError(f"Cannot build stream URL for: {rating_key}")
    return url
