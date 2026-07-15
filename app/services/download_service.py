"""PH-DL-03: physical media download service (docs/20-impl-media-download.md §5).

Pure service layer — no FastAPI/`HTTPException` here (routes map exceptions to
HTTP codes). Reads/writes go through an `AsyncSession` passed in by the
caller: request-path routes pass their own dependency-injected session,
`app.workers.download_worker` opens fresh sessions per attempt via
`async_session_factory` (same pattern as `unified_group_service`).

Security invariant (F-007): the destination path is NEVER client-supplied.
`compute_dest_path` derives + sanitizes a relative path from server-known
metadata; `resolve_confined` is the actual proof (realpath containment) run
again at write time by the worker. See the two functions' docstrings.

Secrets invariant: the upstream Xtream stream URL (contains `user`/`password`
in its query string) is never accepted, stored, or returned by this module —
`download_to_disk` takes an already-built `url` and never logs it or embeds
it in any exception message.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import DownloadBatch, DownloadJob, Media, XtreamAccount
from app.models.schemas import DownloadJobResponse, apply_adult_prefix
from app.services.aggregation_service import canonical_title_year
from app.services.stream_service import parse_rating_key
from app.utils.db_retry import run_with_retry
from app.utils.server_id import parse_server_id
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.download")

# DownloadJob.state values that are NOT terminal — a job in one of these
# states can still be claimed/canceled/progressed.
NON_TERMINAL_STATES = ("queued", "running")
# DownloadJob.state values that are terminal — eligible for clear_finished /
# retry (failed, canceled only).
TERMINAL_STATES = ("completed", "failed", "canceled")


# --- Exceptions (spec §5.1) --------------------------------------------------

class DownloadDisabledError(RuntimeError):
    """`DOWNLOAD_DIR` is not configured — the download feature is disabled."""


class PathConfinementError(ValueError):
    """A computed destination resolved outside `DOWNLOAD_DIR` (F-007)."""


class DownloadCanceled(Exception):
    """Cooperative cancel — caller must leave the `.part` file intact."""


class DownloadPermanentError(Exception):
    """Non-retryable upstream failure (404/403/bad content-type) -> failed."""


class DownloadTransientError(Exception):
    """Retryable upstream/disk failure (timeout/connect/5xx) -> auto-retry."""


class InsufficientDiskSpaceError(DownloadPermanentError):
    """`DOWNLOAD_DIR`'s filesystem has less free space than
    `DOWNLOAD_MIN_FREE_DISK_MB` (préflight, DL-02). A subclass of
    `DownloadPermanentError` (not `DownloadTransientError`): a full disk
    won't fix itself within the job's auto-retry budget, so this must not
    consume one of `DOWNLOAD_MAX_RETRIES` — it fails the job immediately,
    same as an upstream 404/403."""


# --- Path confinement (F-007, security-critical) -----------------------------

_PATH_SEP_RE = re.compile(r"[\\/]+")
_PARENT_DIR_RE = re.compile(r"\.\.+")
_MAX_SEGMENT_LEN = 180
_DEFAULT_EXT = "ts"
_RESERVED_WINDOWS_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _strip_unicode_control(text: str) -> str:
    """Drop ASCII + Unicode control/format/surrogate characters (Cc/Cf/Cs) —
    e.g. RTL override (U+202E), zero-width joiners, BOM — that could make a
    sanitized segment render misleadingly or embed non-printing bytes."""
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))


def _sanitize_segment(name: str, *, fallback: str) -> str:
    """Sanitize ONE path segment: NFC-normalize, strip separators/control
    chars/parent-dir markers, trim trailing '.'/' ', cap length.

    This is defense in depth — `resolve_confined` (realpath containment) is
    what actually PROVES the resulting path stays under `DOWNLOAD_DIR`
    (F-007); this function just keeps the segment sane and never lets a raw
    `..`/`/`/`\\` survive into it.
    """
    text = unicodedata.normalize("NFC", name or "")
    text = _strip_unicode_control(text)
    text = _PATH_SEP_RE.sub(" ", text)
    # Collapse any run of '..' (and longer) to a single '.' so no parent-dir
    # marker survives; the trailing strip below then drops a lone '.'.
    text = _PARENT_DIR_RE.sub(".", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(". ")
    text = text[:_MAX_SEGMENT_LEN]
    text = text.strip(". ")
    if not text:
        return fallback
    if text.lower() in _RESERVED_WINDOWS_NAMES:
        text = f"{text}_"
    return text


def compute_dest_path(
    *,
    media_type: str,
    title: str,
    year: Optional[int],
    season: Optional[int],
    episode: Optional[int],
    ext: str,
    is_adult: bool = False,
) -> str:
    """Relative destination path under `DOWNLOAD_DIR` (NEVER absolute, NEVER
    derived from client input).

    Movies:  ``Movies/<[XXX] ?><Title (Year)>/<[XXX] ?><Title (Year)>.<ext>``
    Episode: ``Series/<Show Title>/Season NN/<Show Title> - SxxEyy.<ext>``

    Every segment goes through `_sanitize_segment` (defense); `resolve_confined`
    proves confinement at write time (F-007) — the two are complementary, not
    redundant. `is_adult` is wired but deliberately UNUSED by callers at the
    MVP (spec §12 — "non appliqué au MVP"); kept available for the P2 prefix.
    """
    safe_ext = _sanitize_segment((ext or _DEFAULT_EXT).lstrip("."), fallback=_DEFAULT_EXT)
    display_title = apply_adult_prefix(title or "Unknown", is_adult)

    if media_type == "movie":
        display = f"{display_title} ({year})" if year else display_title
        folder = _sanitize_segment(display, fallback="Unknown")
        return f"Movies/{folder}/{folder}.{safe_ext}"

    if media_type == "episode":
        show_folder = _sanitize_segment(display_title, fallback="Unknown")
        season_n = season if season is not None else 0
        episode_n = episode if episode is not None else 0
        season_dir = f"Season {season_n:02d}"
        base_name = f"{show_folder} - S{season_n:02d}E{episode_n:02d}"
        file_name = _sanitize_segment(base_name, fallback=f"S{season_n:02d}E{episode_n:02d}")
        return f"Series/{show_folder}/{season_dir}/{file_name}.{safe_ext}"

    raise ValueError(f"unsupported media_type for download: {media_type!r}")


async def check_free_disk_space() -> None:
    """Préflight disk-space guard (DL-02, `DOWNLOAD_MIN_FREE_DISK_MB`).

    Raises `InsufficientDiskSpaceError` if `DOWNLOAD_DIR`'s filesystem has
    fewer free bytes than the configured threshold. No-op if the threshold
    is `<= 0` (opt-out) or `DOWNLOAD_DIR` is unset (an unrelated
    `DownloadDisabledError` is raised elsewhere on that path first).

    `shutil.disk_usage` is a blocking stat syscall — always offloaded via
    `asyncio.to_thread` (house law §9.11), never called inline on the loop.
    """
    threshold_mb = settings.DOWNLOAD_MIN_FREE_DISK_MB
    if threshold_mb <= 0 or not settings.DOWNLOAD_DIR:
        return

    def _stat_free_mb() -> float:
        return shutil.disk_usage(settings.DOWNLOAD_DIR).free / (1024 * 1024)

    free_mb = await asyncio.to_thread(_stat_free_mb)
    if free_mb < threshold_mb:
        raise InsufficientDiskSpaceError(
            f"insufficient free disk space ({free_mb:.0f} MiB free, {threshold_mb} MiB required)"
        )


def resolve_confined(rel_path: str) -> Path:
    """Resolve *rel_path* (as stored on `DownloadJob.dest_path`) to an
    ABSOLUTE path proven to sit under `DOWNLOAD_DIR`, or raise
    `PathConfinementError`. This is the actual security invariant (F-007) —
    `compute_dest_path` only sanitizes defensively; this proves it.
    """
    if not settings.DOWNLOAD_DIR:
        raise DownloadDisabledError("DOWNLOAD_DIR is not configured")
    base = Path(settings.DOWNLOAD_DIR).resolve(strict=False)
    resolved = Path(os.path.realpath(base / rel_path))
    if resolved != base and base not in resolved.parents:
        raise PathConfinementError(f"destination escapes DOWNLOAD_DIR: {rel_path!r}")
    return resolved


# --- Transfer primitive (spec §5.3) ------------------------------------------

@dataclass
class DownloadResult:
    bytes_downloaded: int
    bytes_total: Optional[int]
    already_present: bool   # dest final déjà là (skip-if-exists)
    resumed: bool            # repris via Range depuis un .part


_ERROR_CONTENT_TYPES = ("text/html", "application/json", "text/plain", "text/xml")


def _is_error_content_type(content_type: str) -> bool:
    if not content_type:
        return False
    return content_type.split(";")[0].strip().lower() in _ERROR_CONTENT_TYPES


def _parse_bytes_total(headers: httpx.Headers, resume_from: int) -> Optional[int]:
    content_range = headers.get("content-range")
    if content_range and "/" in content_range:
        total_str = content_range.rsplit("/", 1)[-1].strip()
        if total_str.isdigit():
            return int(total_str)
    content_length = headers.get("content-length")
    if content_length and content_length.isdigit():
        return resume_from + int(content_length)
    return None


def _parse_content_range_start(headers: httpx.Headers) -> Optional[int]:
    """Extract the starting offset from a `Content-Range: bytes <start>-<end>/<total>`
    response header, or `None` if absent/unparseable (CR-MIN-2 — used to prove a
    206 response actually resumes from the offset we asked for)."""
    content_range = (headers.get("content-range") or "").strip()
    if not content_range.lower().startswith("bytes"):
        return None
    spec = content_range[len("bytes"):].strip()
    start_str = spec.split("-", 1)[0].strip()
    return int(start_str) if start_str.isdigit() else None


async def _assert_public_redirect_host(host: Optional[str]) -> None:
    """DL-01 SSRF guard for a followed redirect target.

    Raise ``DownloadPermanentError`` unless *every* address ``host`` resolves to
    is a public, routable IP. Rejects loopback / RFC1918 / link-local
    (incl. 169.254.169.254 cloud metadata) / reserved / multicast, so following
    a provider's 302 can never make us fetch an internal address. The raised
    message never contains the host or the URL (they can embed Xtream creds).

    Residual caveat: this validates the hostname's *current* resolution; a
    determined attacker could DNS-rebind between this check and httpx's own
    connect. That is a far more involved attack than the plain "302 to
    127.0.0.1" this guards against, and out of scope for a self-hosted puller
    against an operator-chosen provider.
    """
    if not host:
        raise DownloadPermanentError("unsafe redirect")
    # A bare IP literal in the Location skips DNS but still must be public.
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, type=socket.SOCK_STREAM
        )
    except OSError:
        raise DownloadPermanentError("unsafe redirect") from None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise DownloadPermanentError("unsafe redirect") from None
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise DownloadPermanentError("unsafe redirect")


async def download_to_disk(
    url: str,
    dest: Path,
    *,
    on_progress: Optional[Callable[[int, Optional[int]], Awaitable[None]]] = None,
    cancel_check: Optional[Callable[[], Awaitable[bool]]] = None,
    chunk_bytes: int = settings.DOWNLOAD_CHUNK_BYTES,
) -> DownloadResult:
    """GET streaming httpx into `<dest>.part`, atomic promotion on success.

    - Skip-if-exists: `dest` present (size>0) and no `.part` -> `already_present`.
    - Resume: existing `.part` (size n>0) -> `Range: bytes=n-`; 206 with a
      `Content-Range` start matching the requested offset appends; a 206
      whose `Content-Range` start does NOT match (non-compliant provider) is
      treated like Range-ignored and truncates/restarts (CR-MIN-2 — avoids a
      gap/overlap in `.part`); 200 (Range ignored) truncates/restarts; 416
      promotes the `.part` as-is.
    - UA = `settings.XTREAM_USER_AGENT`. `follow_redirects=False` on the client
      so we vet redirects ourselves (DL-01, SSRF hardening): real Xtream
      providers 302 a stream URL to their CDN, so up to
      `settings.DOWNLOAD_MAX_REDIRECTS` hops ARE followed inline, but only after
      each hop's target is confirmed to resolve to a public IP — a 302 to an
      internal address (loopback/RFC1918/`169.254.169.254`) is still rejected,
      never fetched. Set `DOWNLOAD_MAX_REDIRECTS=0` to restore the old strict
      behaviour (any 3xx is a permanent failure).
    - `mkdir`/`stat`/`os.replace` are offloaded via `asyncio.to_thread`;
      buffered chunk writes stay inline (accepted I/O, same as elsewhere).
    - On cancel/failure the `.part` is left on disk — never promoted.
    - No message raised here ever contains `url` (it may carry Xtream creds).
    """
    part = dest.with_name(dest.name + ".part")

    def _prepare() -> tuple[int, int]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest_size = dest.stat().st_size if dest.exists() else -1
        part_size = part.stat().st_size if part.exists() else -1
        return dest_size, part_size

    dest_size, part_size = await asyncio.to_thread(_prepare)

    if dest_size > 0 and part_size < 0:
        return DownloadResult(
            bytes_downloaded=dest_size, bytes_total=dest_size,
            already_present=True, resumed=False,
        )

    resume_from = part_size if part_size > 0 else 0
    headers = {"User-Agent": settings.XTREAM_USER_AGENT}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    timeout = httpx.Timeout(
        connect=settings.DOWNLOAD_CONNECT_TIMEOUT,
        read=settings.DOWNLOAD_READ_TIMEOUT,
        write=settings.DOWNLOAD_READ_TIMEOUT,
        pool=settings.DOWNLOAD_CONNECT_TIMEOUT,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            # follow_redirects stays False so we vet each hop ourselves (DL-01).
            # Real Xtream providers 302 a stream URL to their CDN, so a 3xx is
            # followed — but only after its target is confirmed to resolve to a
            # public IP (loopback/RFC1918/link-local/etc. rejected, never
            # fetched). The redirect is handled inside the streaming request
            # loop, so a non-redirecting provider still makes exactly one
            # request. DOWNLOAD_MAX_REDIRECTS=0 restores the old strict "any
            # 3xx is a permanent failure" behaviour.
            redirects_left = settings.DOWNLOAD_MAX_REDIRECTS
            while True:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location or redirects_left <= 0:
                            raise DownloadPermanentError(f"upstream {resp.status_code}")
                        next_url = str(resp.url.join(location))
                        await _assert_public_redirect_host(httpx.URL(next_url).host)
                        url = next_url
                        redirects_left -= 1
                        continue

                    if resp.status_code == 416:
                        # `.part` is already complete per the server -> promote as-is.
                        await asyncio.to_thread(os.replace, part, dest)
                        return DownloadResult(
                            bytes_downloaded=resume_from, bytes_total=resume_from,
                            already_present=False, resumed=True,
                        )
                    if resp.status_code in (404, 403):
                        raise DownloadPermanentError(f"upstream {resp.status_code}")
                    if resp.status_code >= 500 or resp.status_code == 429:
                        raise DownloadTransientError(f"upstream {resp.status_code}")
                    if resp.status_code not in (200, 206):
                        raise DownloadPermanentError(f"upstream {resp.status_code}")

                    content_type = resp.headers.get("content-type", "")
                    if _is_error_content_type(content_type):
                        raise DownloadPermanentError(
                            f"invalid content-type {content_type.split(';')[0].strip()}"
                        )

                    if resp.status_code == 206:
                        range_start = _parse_content_range_start(resp.headers)
                        if range_start is not None and range_start != resume_from:
                            # CR-MIN-2: the server's resumed offset doesn't match
                            # what we asked for (`Range: bytes={resume_from}-`) —
                            # appending here would leave a gap or duplicate a
                            # byte range in `.part`. Treat exactly like a "200,
                            # Range ignored" response: truncate and restart.
                            logger.warning(
                                "Download: Content-Range start %d != requested resume offset %d"
                                " — restarting .part instead of appending",
                                range_start, resume_from,
                            )
                            mode = "wb"
                            resume_from = 0
                            resumed = False
                        else:
                            mode = "ab"
                            resumed = True
                    else:
                        mode = "wb"
                        resume_from = 0
                        resumed = False

                    bytes_total = _parse_bytes_total(resp.headers, resume_from)
                    bytes_done = resume_from

                    f = open(part, mode)
                    try:
                        async for chunk in resp.aiter_bytes(chunk_bytes):
                            if not chunk:
                                continue
                            f.write(chunk)
                            bytes_done += len(chunk)
                            if on_progress is not None:
                                await on_progress(bytes_done, bytes_total)
                            if cancel_check is not None and await cancel_check():
                                raise DownloadCanceled("canceled")
                    finally:
                        f.flush()
                        f.close()

                    await asyncio.to_thread(os.replace, part, dest)
                    return DownloadResult(
                        bytes_downloaded=bytes_done, bytes_total=bytes_total,
                        already_present=False, resumed=resumed,
                    )
    except DownloadCanceled:
        raise
    except DownloadPermanentError:
        raise
    except DownloadTransientError:
        raise
    except httpx.TimeoutException:
        # `from None`: never chain the original exception (its repr can
        # embed connection/host details) into anything a caller might log.
        raise DownloadTransientError("network timeout") from None
    except httpx.TransportError:
        raise DownloadTransientError("network error") from None
    except httpx.HTTPError:
        raise DownloadTransientError("network error") from None
    except OSError as exc:
        raise DownloadTransientError(
            f"disk error: {exc.strerror or exc.__class__.__name__}"
        ) from None


# --- Enqueue (spec §5.4) ------------------------------------------------------

@dataclass
class EnqueueResult:
    jobs: list[DownloadJob]
    batch_id: Optional[str]
    error: Optional[str]          # user-facing message; jobs=[] when set


def _ext_from_rating_key(rating_key: str) -> str:
    parsed = parse_rating_key(rating_key)
    return parsed.get("ext") or _DEFAULT_EXT


async def _find_non_terminal_job(
    db: AsyncSession, server_id: str, rating_key: str,
) -> Optional[DownloadJob]:
    result = await db.execute(
        select(DownloadJob)
        .where(
            DownloadJob.server_id == server_id,
            DownloadJob.rating_key == rating_key,
            DownloadJob.state.in_(NON_TERMINAL_STATES),
        )
        .order_by(DownloadJob.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


find_non_terminal_job = _find_non_terminal_job  # public alias for plex_download_service (C5)


async def _resolve_active_account(db: AsyncSession, server_id: str) -> Optional[XtreamAccount]:
    account_id = parse_server_id(server_id)
    if not account_id:
        return None
    result = await db.execute(
        select(XtreamAccount).where(
            XtreamAccount.id == account_id,
            XtreamAccount.is_active == True,  # noqa: E712
        )
    )
    return result.scalars().first()


async def list_series_seasons(
    db: AsyncSession, server_id: str, rating_key: str
) -> list[int]:
    """Distinct season numbers (``parent_index``) of a series' episodes for a
    given source, ascending. Powers the per-season download picker. A NULL
    season is normalized to 0 (mirrors ``compute_dest_path``)."""
    rows = (await db.execute(
        select(Media.parent_index)
        .where(
            Media.type == "episode",
            Media.server_id == server_id,
            Media.grandparent_rating_key == rating_key,
        )
        .distinct()
    )).scalars().all()
    return sorted({(s if s is not None else 0) for s in rows})


async def enqueue_selection(
    db: AsyncSession,
    *,
    media_type: str,           # 'movie' | 'show' (type of the selection)
    unification_id: str,
    server_id: str,
    rating_key: str,
    scope: str,                # 'movie' | 'series_all' | 'series_seasons'
    seasons: Optional[list[int]] = None,  # required (non-empty) for series_seasons
) -> EnqueueResult:
    """Resolve an operator selection into 1..N persisted `DownloadJob` rows.

    Movie -> exactly one job (`batch_id=None`). Series -> one `DownloadBatch` +
    one job per episode of the chosen source: `scope=series_all` takes every
    episode, `scope=series_seasons` takes only episodes whose season
    (`parent_index`) is in `seasons`. Never raises for a "normal" failure mode
    (missing config/account/media/episodes, empty season selection) — those come
    back as `EnqueueResult(jobs=[], error=...)`.
    """
    if not settings.DOWNLOAD_DIR:
        return EnqueueResult(jobs=[], batch_id=None, error="DOWNLOAD_DIR n'est pas défini")

    account = await _resolve_active_account(db, server_id)
    if account is None:
        return EnqueueResult(
            jobs=[], batch_id=None, error="Compte source introuvable ou inactif",
        )

    if scope == "movie":
        media_row = (await db.execute(
            select(Media)
            .where(Media.server_id == server_id, Media.rating_key == rating_key)
            .limit(1)
        )).scalars().first()
        if media_row is None:
            return EnqueueResult(jobs=[], batch_id=None, error="Media introuvable")

        existing = await _find_non_terminal_job(db, server_id, rating_key)
        if existing is not None:
            return EnqueueResult(jobs=[existing], batch_id=existing.batch_id, error=None)

        title, year = canonical_title_year(media_row)
        dest_path = compute_dest_path(
            media_type="movie", title=title, year=year,
            season=None, episode=None, ext=_ext_from_rating_key(rating_key),
        )
        job = DownloadJob(
            id=uuid.uuid4().hex,
            batch_id=None,
            server_id=server_id,
            rating_key=rating_key,
            media_type="movie",
            unification_id=unification_id or None,
            title=title,
            season=None,
            episode=None,
            dest_path=dest_path,
            state="queued",
            bytes_total=None,
            bytes_done=0,
            attempts=0,
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        db.add(job)

        async def _commit_movie() -> None:
            await db.commit()

        await run_with_retry(_commit_movie, op="enqueue_movie")
        logger.info("Download enqueued: movie job=%s title=%r", job.id, job.title)
        return EnqueueResult(jobs=[job], batch_id=None, error=None)

    if scope in ("series_all", "series_seasons"):
        # Normalize the season selection up front; series_seasons REQUIRES a
        # non-empty set, series_all ignores it (takes every season).
        season_set: Optional[set[int]] = None
        if scope == "series_seasons":
            season_set = {int(s) for s in seasons} if seasons else set()
            if not season_set:
                return EnqueueResult(
                    jobs=[], batch_id=None,
                    error="aucune saison sélectionnée",
                )

        show_row = (await db.execute(
            select(Media)
            .where(Media.server_id == server_id, Media.rating_key == rating_key)
            .limit(1)
        )).scalars().first()
        if show_row is None:
            return EnqueueResult(jobs=[], batch_id=None, error="Série introuvable")

        episode_filters = [
            Media.type == "episode",
            Media.server_id == server_id,
            Media.grandparent_rating_key == rating_key,
        ]
        if season_set is not None:
            episode_filters.append(Media.parent_index.in_(season_set))

        episodes = list((await db.execute(
            select(Media).where(*episode_filters)
        )).scalars().all())
        if not episodes:
            msg = (
                "aucun épisode pour les saisons sélectionnées"
                if season_set is not None else "aucun épisode disponible"
            )
            return EnqueueResult(jobs=[], batch_id=None, error=msg)

        show_title, show_year = canonical_title_year(show_row)
        batch = DownloadBatch(
            id=uuid.uuid4().hex,
            media_type=media_type or "show",
            unification_id=unification_id or None,
            title=show_title,
            server_id=server_id,
            rating_key=rating_key,
            scope=scope,
            total_jobs=0,
            created_at=now_ms(),
        )
        db.add(batch)

        jobs: list[DownloadJob] = []
        new_job_count = 0
        seen_pk: set[tuple[str, str]] = set()
        for ep in episodes:
            pk = (ep.server_id, ep.rating_key)
            if pk in seen_pk:
                continue
            seen_pk.add(pk)

            existing = await _find_non_terminal_job(db, ep.server_id, ep.rating_key)
            if existing is not None:
                jobs.append(existing)
                continue

            dest_path = compute_dest_path(
                media_type="episode", title=show_title, year=show_year,
                season=ep.parent_index, episode=ep.index,
                ext=_ext_from_rating_key(ep.rating_key),
            )
            job = DownloadJob(
                id=uuid.uuid4().hex,
                batch_id=batch.id,
                server_id=ep.server_id,
                rating_key=ep.rating_key,
                media_type="episode",
                unification_id=unification_id or None,
                title=ep.title or show_title,
                season=ep.parent_index,
                episode=ep.index,
                dest_path=dest_path,
                state="queued",
                bytes_total=None,
                bytes_done=0,
                attempts=0,
                created_at=now_ms(),
                updated_at=now_ms(),
            )
            db.add(job)
            jobs.append(job)
            new_job_count += 1

        # total_jobs counts only jobs actually linked to THIS batch — a
        # reused (deduped) job may still point at an earlier batch_id.
        batch.total_jobs = new_job_count

        async def _commit_series() -> None:
            await db.commit()

        await run_with_retry(_commit_series, op="enqueue_series")
        logger.info(
            "Download enqueued: series batch=%s title=%r jobs=%d (new=%d)",
            batch.id, batch.title, len(jobs), new_job_count,
        )
        return EnqueueResult(jobs=jobs, batch_id=batch.id, error=None)

    return EnqueueResult(jobs=[], batch_id=None, error=f"scope inconnu: {scope!r}")


# --- Read / mutate (request-path, spec §5.5) ---------------------------------

async def list_jobs(
    db: AsyncSession, *, states: Optional[list[str]] = None,
    limit: int = 200, offset: int = 0,
) -> tuple[list[DownloadJob], int]:
    base_filter = DownloadJob.state.in_(states) if states else None

    count_query = select(func.count()).select_from(DownloadJob)
    query = select(DownloadJob)
    if base_filter is not None:
        count_query = count_query.where(base_filter)
        query = query.where(base_filter)

    total = (await db.execute(count_query)).scalar() or 0
    query = query.order_by(DownloadJob.created_at.desc()).limit(limit).offset(offset)
    jobs = list((await db.execute(query)).scalars().all())
    return jobs, total


async def get_job(db: AsyncSession, job_id: str) -> Optional[DownloadJob]:
    return await db.get(DownloadJob, job_id)


async def cancel_job(db: AsyncSession, job_id: str) -> Optional[DownloadJob]:
    """`queued`/`running` -> `canceled` (immediate write; a running transfer
    discovers it cooperatively via `cancel_check` and leaves its `.part`).
    Terminal states -> no-op. Conditional `UPDATE ... WHERE id AND state IN
    (...)` — cross-process safe (see download_worker §6.2)."""
    job = await db.get(DownloadJob, job_id)
    if job is None:
        return None

    async def _do() -> int:
        result = await db.execute(
            update(DownloadJob)
            .where(DownloadJob.id == job_id, DownloadJob.state.in_(NON_TERMINAL_STATES))
            .values(state="canceled", updated_at=now_ms(), finished_at=now_ms())
        )
        await db.commit()
        return result.rowcount

    rowcount = await run_with_retry(_do, op="cancel_job")
    await db.refresh(job)
    if rowcount:
        logger.info("Download job canceled: id=%s title=%r", job.id, job.title)
    return job


async def retry_job(db: AsyncSession, job_id: str) -> Optional[DownloadJob]:
    """`failed`/`canceled` -> `queued`, resetting `error`/`attempts` (a manual
    retry asks for a full new cycle — attempts is NOT carried over). The
    `.part` file (if any) is left untouched -> the worker resumes via Range.
    `queued`/`running`/`completed` -> no-op."""
    job = await db.get(DownloadJob, job_id)
    if job is None:
        return None

    async def _do() -> int:
        result = await db.execute(
            update(DownloadJob)
            .where(DownloadJob.id == job_id, DownloadJob.state.in_(("failed", "canceled")))
            .values(state="queued", error=None, attempts=0, updated_at=now_ms(), finished_at=None)
        )
        await db.commit()
        return result.rowcount

    rowcount = await run_with_retry(_do, op="retry_job")
    await db.refresh(job)
    if rowcount:
        logger.info("Download job retried: id=%s title=%r", job.id, job.title)
    return job


async def clear_finished(db: AsyncSession) -> int:
    """DELETE every terminal (`completed`/`failed`/`canceled`) job. Returns
    the number deleted. Orphaned `download_batch` rows are left in place
    (harmless — no hard FK, no read path depends on them being pruned)."""
    async def _do() -> int:
        result = await db.execute(
            delete(DownloadJob).where(DownloadJob.state.in_(TERMINAL_STATES))
        )
        await db.commit()
        return result.rowcount

    deleted = await run_with_retry(_do, op="clear_finished")
    if deleted:
        logger.info("Download queue cleared: %d finished job(s) removed", deleted)
    return deleted


# --- Serialization (spec §4/§6.4) --------------------------------------------

def compute_percent(job: DownloadJob) -> Optional[float]:
    if not job.bytes_total:
        return None
    return round((job.bytes_done or 0) / job.bytes_total * 100, 1)


def compute_speed_bps(job: DownloadJob) -> Optional[float]:
    """Average bytes/sec since the job started running. No speed column is
    persisted — this is a point-in-time derivation from `bytes_done` /
    `(updated_at - started_at)`, recomputed on every read."""
    if job.state != "running" or not job.started_at:
        return None
    elapsed_s = max(1, (job.updated_at - job.started_at) / 1000)
    return (job.bytes_done or 0) / elapsed_s


def to_download_response(job: DownloadJob) -> DownloadJobResponse:
    """The single builder that maps a `DownloadJob` ORM row to the wire
    schema — deliberately NOT `from_attributes`/`model_validate` (see
    `DownloadJobResponse`'s docstring): `bytesDownloaded`/`retries` read
    differently-named columns and `percent`/`speedBps` are computed."""
    return DownloadJobResponse(
        job_id=job.id,
        batch_id=job.batch_id,
        type=job.media_type,
        unification_id=job.unification_id,
        title=job.title,
        season=job.season,
        episode=job.episode,
        server_id=job.server_id,
        rating_key=job.rating_key,
        state=job.state,
        bytes_downloaded=job.bytes_done or 0,
        bytes_total=job.bytes_total,
        percent=compute_percent(job),
        speed_bps=compute_speed_bps(job),
        dest_path=job.dest_path,
        error=job.error,
        retries=job.attempts or 0,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )
