"""Sidecar ``.nfo`` for a physically downloaded media file.

Feature: when a download completes, write a Jellyfin/Kodi-compatible ``.nfo``
next to the media file so an offline copy carries its metadata. This reuses the
Plex-generator NFO XML builders (``app.plex_generator.nfo_builder``) — the only
new work here is mapping the single ``Media`` row a ``DownloadJob`` points at
onto the ``PlexMovie`` / ``PlexEpisode`` the builders expect (the generator's
own ``source.py`` maps aggregated groups, not a single row, so it can't be
reused directly).

Best-effort by contract: ``render_media_nfo`` returns ``None`` for anything it
can't describe, and the caller (the download worker) swallows failures so a
missing/garbled ``.nfo`` never fails the download itself.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.models.database import Media
from app.plex_generator.models import PlexEpisode, PlexMovie
from app.plex_generator.nfo_builder import build_episode_nfo, build_movie_nfo

logger = logging.getLogger("plexhub.download.nfo")


def _tmdb_int(value) -> Optional[int]:
    """Coerce a stored tmdb_id (str/int/None) to int, mirroring
    ``plex_generator.source._tmdb_int`` — the NFO ``<uniqueid type='tmdb'>``
    wants an int, and a non-numeric value is simply dropped."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _as_float(value) -> Optional[float]:
    """Coerce a rating to float or None — PlexMovie.rating is typed float, and a
    stray non-numeric string must not break NFO rendering."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def render_media_nfo(media: Media) -> Optional[str]:
    """Return the ``.nfo`` XML for a downloaded ``Media`` row.

    Movies -> ``movie.nfo`` shape; episodes -> ``episodedetails`` shape. Returns
    ``None`` for a type we don't emit a per-file NFO for (e.g. ``show``), so the
    caller can skip writing.
    """
    if media.type == "movie":
        movie = PlexMovie(
            source_id=media.rating_key,
            title=media.title or "Unknown",
            is_adult=bool(getattr(media, "is_adult", False)),
            year=media.year,
            poster_url=media.resolved_thumb_url or media.thumb_url,
            fanart_url=media.resolved_art_url or media.art_url,
            genres=media.genres,
            summary=media.summary,
            imdb_id=media.imdb_id,
            tmdb_id=_tmdb_int(media.tmdb_id),
            content_rating=media.content_rating,
            rating=_as_float(media.display_rating or media.scraped_rating),
            duration_ms=media.duration,
            cast=media.cast,
        )
        return build_movie_nfo(movie)

    if media.type == "episode":
        episode = PlexEpisode(
            source_id=media.rating_key,
            series_title=media.grandparent_title or media.parent_title or "Unknown",
            season_num=media.parent_index if media.parent_index is not None else 0,
            episode_num=media.index if media.index is not None else 0,
            title=media.title,
            summary=media.summary,
            duration_ms=media.duration,
            thumb_url=media.resolved_thumb_url or media.thumb_url,
        )
        return build_episode_nfo(episode)

    return None
