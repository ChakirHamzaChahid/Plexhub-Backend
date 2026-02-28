import re
import logging
from typing import Optional

from app.services.xtream_service import xtream_service

logger = logging.getLogger("plexhub.stream")


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
    else:
        logger.warning(f"Cannot build stream URL for type: {parsed['type']}")
        return None
