"""Trailer resolution — YouTube trailer key -> cached local mp4 via yt-dlp
(feature "trailers Home overlay pour les médias non-Plex", Lot A).

Chain:
  1. `resolve_by_rating_key`/`resolve_by_youtube_id` (called by the two
     `GET /api/media/trailer/*` routes, `app/api/media.py`) determine the
     YouTube video id to serve — either directly supplied ("generic" mode:
     Jellyfin `RemoteTrailers`/xtream-direct callers who already know the
     key), or read from `Media.youtube_trailer` ("rating_key" mode; captured
     at sync, `sync_worker.py`) with a live TMDB `/videos` repli + write-back
     when the column is still empty.
  2. `_resolve_cached_or_kickoff` checks the on-disk cache
     (`resolve_confined_cache_path`) — `ready` if the mp4 is already there,
     else it fires a background yt-dlp download (deduped per-process by
     `_in_flight`) and returns `pending`.
  3. `_download_trailer` runs yt-dlp in a thread (100% blocking library,
     house law §9.11 — never call it inline on the event loop) into a
     PRIVATE per-attempt work directory, then `os.replace`s the merged mp4
     into the public cache path atomically — nothing is ever visible at the
     served path until the whole download+merge succeeds (mirrors
     `download_service.download_to_disk`'s `.part` -> `os.replace`
     promotion, without literally reusing that HTTP-GET helper since yt-dlp
     does its own network/muxing).
  4. `purge_lru` evicts the least-recently-written cached mp4s once the
     cache exceeds `TRAILER_CACHE_MAX_MB` — called both right after a
     successful download and by the nightly cron (`app/main.py`, mirrors
     `epg_cleanup`/`subtitle_cache_cleanup`).

Feature-gated exactly like the physical-download feature (`config.py`):
`TRAILER_CACHE_DIR` empty = every entry point below returns "none"/is a
no-op — `yt_dlp` itself is only imported lazily, on an actual download
attempt (`_run_ytdlp`), so a disabled/unconfigured deployment never pays for
it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import Media
from app.services.media_service import media_service
from app.services.tmdb_service import tmdb_service
from app.utils.db_retry import write_with_retry
from app.utils.tasks import create_background_task
from app.utils.ttl_cache import TTLCache

logger = logging.getLogger("plexhub.trailer")

# Bare YouTube video id shape (11 base64url-ish chars: A-Z a-z 0-9 - _) —
# ALSO the exact `cache_key` shape accepted by
# `GET /api/media/trailer/file/{cache_key}`: no separator or traversal
# character (`.`, `/`) can ever match this pattern, so a validated id is
# structurally incapable of escaping `TRAILER_CACHE_DIR` even before
# `resolve_confined_cache_path`'s explicit realpath check runs.
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Live TMDB `/videos` repli result cache (rating_key mode, no stored
# `youtube_trailer` yet) — keyed (tmdb_id, media_kind). Caches BOTH positive
# and negative (`None`) results so a title genuinely without a trailer isn't
# re-queried on every popup focus. 6h TTL: long enough to spare repeat
# lookups within a session, short enough that a newly-uploaded trailer is
# picked up without a redeploy.
_TMDB_REPLI_TTL_SECONDS = 6 * 3600
_TMDB_REPLI_CACHE_SIZE = 2000
_tmdb_repli_cache: TTLCache[tuple[int, str], "str | None"] = TTLCache(
    max_size=_TMDB_REPLI_CACHE_SIZE, ttl_seconds=_TMDB_REPLI_TTL_SECONDS,
)
_MISSING = object()

# Per-process in-flight download dedup — several concurrent `resolve` calls
# for the same trailer (e.g. the same Home card re-focused before its first
# download finishes) must trigger exactly ONE yt-dlp download, not one per
# request. Process-unique by design (mirrors the plan's own scope — no
# cross-process locking; a rare cross-replica double-download is wasteful,
# never corrupting, since the final `os.replace` promotion is atomic either
# way).
_in_flight: set[str] = set()


class TrailerDisabledError(Exception):
    """`TRAILER_CACHE_DIR` is not configured — the trailer feature is disabled."""


class TrailerPathConfinementError(Exception):
    """A resolved cache path would escape `TRAILER_CACHE_DIR` (F-007-equivalent)."""


@dataclass
class TrailerResolution:
    status: Literal["ready", "pending", "none"]
    url: str | None = None


def is_valid_youtube_id(value: object) -> bool:
    """True iff `value` is a bare YouTube video id (11 chars,
    `[A-Za-z0-9_-]`) — also the exact `cache_key` shape the file endpoint
    accepts."""
    return isinstance(value, str) and bool(_YOUTUBE_ID_RE.match(value))


def file_url_for(youtube_id: str) -> str:
    """Public URL of the cached mp4 for `youtube_id` — relative, served by
    `GET /api/media/trailer/file/{cache_key}` under the caller's own
    host/auth (no absolute URL is ever handed out)."""
    return f"/api/media/trailer/file/{youtube_id}"


def resolve_confined_cache_path(youtube_id: str) -> Path:
    """Resolve the FINAL served path for `youtube_id`, PROVEN to sit under
    `TRAILER_CACHE_DIR` (F-007 invariant, mirrors
    `download_service.resolve_confined`). Raises `TrailerDisabledError` /
    `TrailerPathConfinementError` instead of ever returning an unconfined
    path — every caller (resolve, the file endpoint, purge) goes through
    this single choke point.
    """
    if not settings.TRAILER_CACHE_DIR:
        raise TrailerDisabledError("TRAILER_CACHE_DIR is not configured")
    base = Path(settings.TRAILER_CACHE_DIR).resolve(strict=False)
    resolved = Path(os.path.realpath(base / f"{youtube_id}.mp4"))
    # Mirrors `download_service.resolve_confined`'s invariant exactly: safe
    # iff `resolved` sits STRICTLY under `base` (the `resolved == base` arm
    # only matters for an empty-suffix request there; kept for parity). The
    # comparison MUST be against `base`, never against the pre-realpath
    # candidate — on Windows, `base / "/etc/passwd.mp4"` is ALREADY collapsed
    # to a drive-relative path (e.g. "C:\\etc\\passwd.mp4") at construction
    # time, before `os.path.realpath` even runs, so a candidate-vs-resolved
    # comparison would silently never fire for that escape shape.
    if resolved != base and base not in resolved.parents:
        raise TrailerPathConfinementError(
            f"cache_key escapes TRAILER_CACHE_DIR: {youtube_id!r}"
        )
    return resolved


# ─── Resolution entry points (called by the API routes) ────────────────────


async def resolve_by_youtube_id(youtube_id: str | None) -> TrailerResolution:
    """Generic mode — caller already knows the YouTube key (Jellyfin
    `RemoteTrailers`, xtream-direct `getVodInfo`). No DB, no TMDB repli."""
    if not is_valid_youtube_id(youtube_id):
        return TrailerResolution(status="none")
    return await _resolve_cached_or_kickoff(youtube_id)  # type: ignore[arg-type]


async def resolve_by_rating_key(
    db: AsyncSession, rating_key: str, server_id: str,
) -> TrailerResolution:
    """`rating_key` mode: `media.youtube_trailer` first, live TMDB `/videos`
    repli (write-back on success) when the column is still empty."""
    media = await media_service.get_media_by_key(db, rating_key, server_id)
    if media is None:
        return TrailerResolution(status="none")

    if is_valid_youtube_id(media.youtube_trailer):
        return await _resolve_cached_or_kickoff(media.youtube_trailer)  # type: ignore[arg-type]

    tmdb_id = _as_int(media.tmdb_id)
    if tmdb_id is None or not tmdb_service.is_configured:
        return TrailerResolution(status="none")
    media_kind = "movie" if media.type == "movie" else "tv"

    cache_key = (tmdb_id, media_kind)
    cached = _tmdb_repli_cache.get(cache_key, default=_MISSING)
    if cached is _MISSING:
        resolved_id = await tmdb_service.get_videos_trailer(tmdb_id, media_kind)
        _tmdb_repli_cache.set(cache_key, resolved_id)
        if resolved_id:
            await _write_back_youtube_trailer(rating_key, server_id, resolved_id)
    else:
        resolved_id = cached

    if not is_valid_youtube_id(resolved_id):
        return TrailerResolution(status="none")
    return await _resolve_cached_or_kickoff(resolved_id)


def _as_int(value: object) -> int | None:
    return int(value) if value is not None and str(value).isdigit() else None


async def _write_back_youtube_trailer(
    rating_key: str, server_id: str, youtube_id: str,
) -> None:
    """Persist a TMDB-resolved trailer key onto every (filter, sort_order)
    bucket of this (rating_key, server_id) row — same unconstrained
    `Media.rating_key ==, Media.server_id ==` UPDATE shape the enrichment
    worker's own rich-metadata write uses. Best-effort: a failure here never
    fails the resolve (the id resolved this call is still served/cached, it
    just won't be found by column on the NEXT resolve).

    `write_with_retry` opens a FRESH session for this write (never the
    request-scoped `db` the caller read from) — a same-session retry after a
    real SQLite lock raises `PendingRollbackError` instead of genuinely
    retrying (`app/utils/db_retry.py`'s own docstring, ADR 0004 Decision 4;
    piège: "same-session retry cannot survive a real lock")."""

    async def _work(session: AsyncSession) -> None:
        await session.execute(
            update(Media)
            .where(Media.rating_key == rating_key, Media.server_id == server_id)
            .values(youtube_trailer=youtube_id)
        )
        await session.commit()

    try:
        await write_with_retry(_work, op="trailer_service.write_back_youtube_trailer")
    except Exception as exc:
        logger.warning(
            "Failed to persist resolved trailer for %s/%s: %s", rating_key, server_id, exc,
        )


# ─── Cache lookup + background download kickoff ─────────────────────────────


async def _resolve_cached_or_kickoff(youtube_id: str) -> TrailerResolution:
    try:
        path = resolve_confined_cache_path(youtube_id)
    except (TrailerDisabledError, TrailerPathConfinementError):
        return TrailerResolution(status="none")

    if await asyncio.to_thread(_file_ready, path):
        return TrailerResolution(status="ready", url=file_url_for(youtube_id))

    if youtube_id not in _in_flight:
        _in_flight.add(youtube_id)
        create_background_task(
            _download_and_release(youtube_id), name=f"trailer_dl_{youtube_id}",
        )
    return TrailerResolution(status="pending")


def _file_ready(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


async def _download_and_release(youtube_id: str) -> None:
    try:
        downloaded = await _download_trailer(youtube_id)
        if downloaded:
            await purge_lru()
    except Exception as exc:
        logger.warning("Trailer download failed for %s: %s", youtube_id, exc)
    finally:
        _in_flight.discard(youtube_id)


async def _download_trailer(youtube_id: str) -> bool:
    """yt-dlp download -> merge -> atomic promotion into the public cache.

    Runs the 100% blocking `yt_dlp` library in a thread (house law §9.11).
    Downloads into a PRIVATE, per-attempt work directory so nothing is ever
    visible at the public `{id}.mp4` path until the whole download+merge
    succeeds — the promotion itself is one `os.replace` (atomic on the same
    filesystem; `.part`-style pattern, `download_service.download_to_disk`).
    Returns True iff a file was promoted.
    """
    final_path = resolve_confined_cache_path(youtube_id)
    work_dir = final_path.parent / f".ytdlp-{youtube_id}-{uuid.uuid4().hex[:8]}"

    def _run() -> bool:
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            _run_ytdlp(youtube_id, work_dir)
            produced = [p for p in work_dir.iterdir() if p.is_file()]
            if not produced:
                logger.warning("yt-dlp produced no output file for %s", youtube_id)
                return False
            # A single output file is expected (one trailer, one outtmpl
            # template) — the largest one wins defensively if yt-dlp ever
            # leaves a stray sidecar (e.g. a thumbnail) behind.
            produced.sort(key=lambda p: p.stat().st_size, reverse=True)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(produced[0], final_path)
            return True
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    return await asyncio.to_thread(_run)


def _run_ytdlp(youtube_id: str, work_dir: Path) -> None:
    """The actual (blocking) yt-dlp invocation — split out so tests can
    monkeypatch this single seam instead of stubbing the `yt_dlp` package.
    MUST be called off the event loop (see `_download_trailer`)."""
    import yt_dlp  # deferred: only needed on an actual download attempt

    ydl_opts = {
        "format": select_format(),
        "outtmpl": str(work_dir / "trailer.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "logger": _YtDlpLogger(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={youtube_id}"])


class _YtDlpLogger:
    """Routes yt-dlp's own logging through `plexhub.trailer` instead of the
    library's stdout/stderr default (yt-dlp prints warnings/errors even with
    `quiet=True`)."""

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.error("yt-dlp: %s", message)


def select_format() -> str:
    """Pure/testable: the primary tier needs ffmpeg to mux separate
    video+audio streams — without it (`ffmpeg` missing from the image) yt-dlp
    can only serve a pre-muxed progressive stream, so skip straight to the
    720p/any-mp4 progressive tiers (plan's documented repli)."""
    if shutil.which("ffmpeg"):
        return (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=720]/best[ext=mp4]"
        )
    return "best[ext=mp4][height<=720]/best[ext=mp4]"


# ─── LRU purge (after each download + nightly cron, app/main.py) ────────────


async def purge_lru() -> int:
    """Evict the least-recently-written cached trailers until the cache is
    back under `TRAILER_CACHE_MAX_MB`. Returns the number of files removed.
    No-op if the feature is disabled or the cap is `<=0` (opt-out, mirrors
    `DOWNLOAD_MIN_FREE_DISK_MB`'s convention). Blocking filesystem walk/stat/
    unlink — offloaded via `asyncio.to_thread` (house law §9.11)."""
    if not settings.TRAILER_CACHE_DIR:
        return 0
    return await asyncio.to_thread(_purge_lru_sync)


def _purge_lru_sync() -> int:
    base = Path(settings.TRAILER_CACHE_DIR)
    if not base.is_dir():
        return 0
    max_bytes = settings.TRAILER_CACHE_MAX_MB * 1024 * 1024
    if max_bytes <= 0:
        return 0

    entries: list[tuple[Path, float, int]] = []
    total = 0
    for p in base.iterdir():
        if not p.is_file() or not p.name.endswith(".mp4"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((p, st.st_mtime, st.st_size))
        total += st.st_size

    if total <= max_bytes:
        return 0

    # Oldest mtime first -> evicted first. mtime is a valid LRU proxy here
    # because a re-download always rewrites the file (refreshing its mtime)
    # and reads never mutate it — no separate access-time bookkeeping needed.
    entries.sort(key=lambda e: e[1])
    removed = 0
    for path, _mtime, size in entries:
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Trailer LRU purge: failed to remove %s: %s", path, exc)
            continue
        total -= size
        removed += 1
    if removed:
        logger.info(
            "Trailer LRU purge: removed %s file(s), cache now ~%.1f MB",
            removed, total / (1024 * 1024),
        )
    return removed
