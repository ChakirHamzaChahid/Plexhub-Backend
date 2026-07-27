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
import shutil
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
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
from app.utils.youtube import YOUTUBE_ID_RE, extract_youtube_id

logger = logging.getLogger("plexhub.trailer")

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

# Recently-failed downloads (video removed/age-restricted/geo-blocked/
# transient network error) — BM-2, code review. Without this, every popup
# focus on a permanently-broken id re-launches a full yt-dlp attempt
# (up to ~90s of thread time each). A short TTL negative cache absorbs the
# hammering while still eventually retrying (a geo-block or a transient
# provider hiccup can resolve itself) — `resolve` reports "pending", never
# "none", while an entry is live: the failure may be transient, so the
# caller must keep polling, not treat it as a hard miss.
_DOWNLOAD_FAILURE_TTL_SECONDS = 45 * 60
_DOWNLOAD_FAILURE_CACHE_SIZE = 500
_download_failures: TTLCache[str, bool] = TTLCache(
    max_size=_DOWNLOAD_FAILURE_CACHE_SIZE, ttl_seconds=_DOWNLOAD_FAILURE_TTL_SECONDS,
)

# Per-process in-flight download dedup — several concurrent `resolve` calls
# for the same trailer (e.g. the same Home card re-focused before its first
# download finishes) must trigger exactly ONE yt-dlp download, not one per
# request. Process-unique by design (mirrors the plan's own scope — no
# cross-process locking; a rare cross-replica double-download is wasteful,
# never corrupting, since the final `os.replace` promotion is atomic either
# way).
_in_flight: set[str] = set()

# Global concurrency cap (BM-1, code review) — `_in_flight` alone only
# dedupes by id; without a ceiling, scrolling the Home rail can trigger one
# yt-dlp *process* per distinct card focused (each a real subprocess doing
# network I/O + ffmpeg muxing). Once the cap is reached, a resolve for a
# NEW id still answers "pending" (never "none" — nothing failed, it's just
# queued) but does not start a download; it will be picked up on a later
# focus once a slot frees up.
_MAX_CONCURRENT_DOWNLOADS = 2

# Orphaned `.ytdlp-*` work directories older than this are removed by the
# nightly purge (crash/kill -9 mid-download leaves one behind — `_run()`'s
# `finally: shutil.rmtree` only fires on a normal return/exception, not a
# process kill). Generous window: a real download should finish in well
# under an hour even on a slow connection.
_ORPHAN_WORKDIR_MAX_AGE_SECONDS = 6 * 3600


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
    return isinstance(value, str) and bool(YOUTUBE_ID_RE.match(value))


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
    # iff `resolved` sits STRICTLY under `base`. The comparison MUST be
    # against `base`, never against the pre-realpath candidate — on Windows,
    # `base / "/etc/passwd.mp4"` is ALREADY collapsed to a drive-relative
    # path (e.g. "C:\\etc\\passwd.mp4") at construction time, before
    # `os.path.realpath` even runs, so a candidate-vs-resolved comparison
    # would silently never fire for that escape shape (this exact bug was
    # caught and fixed here during code review).
    # `resolved == base` (the left arm below) is structurally unreachable on
    # this call path: we always append a non-empty `f"{youtube_id}.mp4"`
    # suffix, so `resolved` can never literally equal `base` itself — kept
    # only for byte-for-byte parity with `download_service.resolve_confined`
    # (whose `rel_path` CAN legitimately be empty for a directory request).
    if resolved != base and base not in resolved.parents:
        raise TrailerPathConfinementError(
            f"cache_key escapes TRAILER_CACHE_DIR: {youtube_id!r}"
        )
    return resolved


# ─── Resolution entry points (called by the API routes) ────────────────────


async def resolve_by_youtube_id(youtube_id: str | None) -> TrailerResolution:
    """Generic mode — caller already knows the YouTube key (Jellyfin
    `RemoteTrailers`, xtream-direct `getVodInfo`). No DB, no TMDB repli.

    `youtube_id` is normalized via `extract_youtube_id` (BB-1, code review)
    — defensive: an Android caller mirroring this same provider data could
    just as easily be holding a raw URL instead of a bare id."""
    normalized = extract_youtube_id(youtube_id)
    if normalized is None:
        return TrailerResolution(status="none")
    return await _resolve_cached_or_kickoff(normalized)


async def resolve_by_rating_key(
    db: AsyncSession, rating_key: str, server_id: str,
) -> TrailerResolution:
    """`rating_key` mode: `media.youtube_trailer` first, live TMDB `/videos`
    repli (write-back on success) when the column is still empty."""
    media = await media_service.get_media_by_key(db, rating_key, server_id)
    if media is None:
        return TrailerResolution(status="none")

    # BB-1 (code review): the STORED column may itself be URL-shaped —
    # `sync_worker` normalizes at capture time going forward, but a row
    # synced before that fix (or an upstream provider quirk not covered by
    # `extract_youtube_id`'s patterns) could still hold something other than
    # a bare id. Normalize defensively here too, rather than trusting the
    # column's shape.
    stored = extract_youtube_id(media.youtube_trailer)
    if stored is not None:
        return await _resolve_cached_or_kickoff(stored)

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

    resolved_id = extract_youtube_id(resolved_id)
    if resolved_id is None:
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

    Unconditional `.values(youtube_trailer=youtube_id)`, NOT a
    `func.coalesce(...)` fill-missing write like `enrichment_worker`'s rich
    metadata — the asymmetry is intentional, not an oversight: this function
    is only ever reached from `resolve_by_rating_key`'s branch where
    `extract_youtube_id(media.youtube_trailer)` already returned `None`
    (column empty, or holding something unparseable), so "the column is
    empty" is enforced by the CALLER, not by a COALESCE in the UPDATE
    itself — there is nothing to fill-missing against here.

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

    # BM-2 (code review): a recently-failed id is neither re-launched nor
    # reported "none" — the failure (video removed/age-restricted/geo-
    # blocked/transient network error) may be transient, so we keep saying
    # "pending" and simply skip re-kicking a download until the TTL lapses.
    if _download_failures.get(youtube_id, default=False):
        return TrailerResolution(status="pending")

    if youtube_id not in _in_flight:
        # BM-1 (code review): global concurrency ceiling — `_in_flight`
        # alone only dedupes by id. Below the cap AND enough free disk
        # space (BM-3): launch. Otherwise: still "pending" (nothing failed,
        # just not started yet) — no download is queued for later, the
        # NEXT resolve call (next focus) re-evaluates from scratch.
        if len(_in_flight) < _MAX_CONCURRENT_DOWNLOADS and await _has_enough_free_disk_space():
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


async def _has_enough_free_disk_space() -> bool:
    """BM-3 (code review) préflight — mirrors
    `download_service.check_free_disk_space`'s shape (blocking
    `shutil.disk_usage` offloaded via `asyncio.to_thread`, house law §9.11).
    `TRAILER_MIN_FREE_DISK_MB<=0` opts out (same convention as
    `DOWNLOAD_MIN_FREE_DISK_MB`)."""
    threshold_mb = settings.TRAILER_MIN_FREE_DISK_MB
    if threshold_mb <= 0 or not settings.TRAILER_CACHE_DIR:
        return True

    def _stat_free_mb() -> float:
        return shutil.disk_usage(settings.TRAILER_CACHE_DIR).free / (1024 * 1024)

    try:
        free_mb = await asyncio.to_thread(_stat_free_mb)
    except OSError as exc:
        logger.warning("Trailer disk-space check failed, allowing download: %s", exc)
        return True
    if free_mb < threshold_mb:
        logger.warning(
            "Trailer download skipped — only %.0f MiB free (< %s MiB floor)",
            free_mb, threshold_mb,
        )
        return False
    return True


async def _download_and_release(youtube_id: str) -> None:
    try:
        downloaded = await _download_trailer(youtube_id)
        if downloaded:
            await purge_lru()
        else:
            _download_failures.set(youtube_id, True)
    except Exception as exc:
        logger.warning("Trailer download failed for %s: %s", youtube_id, exc)
        _download_failures.set(youtube_id, True)
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
    `quiet=True`).

    `debug()` is routed to `logger.debug` rather than swallowed (code
    review) — depending on the yt-dlp version, some ERROR-level diagnostics
    (e.g. certain extractor failures) are routed through `.debug()` with a
    leading `"[debug] "`/... prefix instead of `.error()`; dropping it
    silently would hide the actual reason a download failed."""

    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.error("yt-dlp: %s", message)


@lru_cache(maxsize=1)
def _ffmpeg_available() -> bool:
    """Memoized (code review) — `shutil.which` re-scans `PATH` on every
    call; ffmpeg presence is fixed for the lifetime of the process (it's
    baked into the Docker image), so re-stat'ing it on every single
    download is pure waste. `select_format` is a thin wrapper so tests can
    still flip availability freely by calling `_ffmpeg_available.cache_clear()`
    between cases (see `tests/test_trailer_service.py`)."""
    return shutil.which("ffmpeg") is not None


def select_format() -> str:
    """Pure/testable: the primary tier needs ffmpeg to mux separate
    video+audio streams — without it (`ffmpeg` missing from the image) yt-dlp
    can only serve a pre-muxed progressive stream, so skip straight to the
    720p/any-mp4 progressive tiers (plan's documented repli)."""
    if _ffmpeg_available():
        return (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=720]/best[ext=mp4]"
        )
    return "best[ext=mp4][height<=720]/best[ext=mp4]"


# ─── LRU purge (after each download + nightly cron, app/main.py) ────────────


async def purge_lru() -> int:
    """Evict the least-recently-written cached trailers until the cache is
    back under `TRAILER_CACHE_MAX_MB`, AND remove any orphaned `.ytdlp-*`
    work directory older than `_ORPHAN_WORKDIR_MAX_AGE_SECONDS` (a crash/
    kill -9 mid-download leaves one behind — its own `finally: rmtree`
    never gets a chance to run). Returns the total number of filesystem
    entries removed (mp4s + orphan dirs combined). No-op if the feature is
    disabled. Blocking filesystem walk/stat/unlink — offloaded via
    `asyncio.to_thread` (house law §9.11)."""
    if not settings.TRAILER_CACHE_DIR:
        return 0
    return await asyncio.to_thread(_purge_lru_sync)


def _purge_lru_sync() -> int:
    base = Path(settings.TRAILER_CACHE_DIR)
    if not base.is_dir():
        return 0

    removed = _purge_orphan_workdirs(base)
    removed += _purge_lru_mp4s(base)
    return removed


def _purge_orphan_workdirs(base: Path) -> int:
    """Remove `.ytdlp-*` work directories older than
    `_ORPHAN_WORKDIR_MAX_AGE_SECONDS` — leftovers from a process kill/crash
    mid-download (code review, minor). Independent of `TRAILER_CACHE_MAX_MB`
    (opt-out `<=0` does not apply here — an orphan directory is pure litter,
    never a legitimately-cached trailer)."""
    now = time.time()
    removed = 0
    for p in base.iterdir():
        if not p.is_dir() or not p.name.startswith(".ytdlp-"):
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age <= _ORPHAN_WORKDIR_MAX_AGE_SECONDS:
            continue
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            removed += 1
    if removed:
        logger.info("Trailer LRU purge: removed %s orphaned work director(y/ies)", removed)
    return removed


def _purge_lru_mp4s(base: Path) -> int:
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
