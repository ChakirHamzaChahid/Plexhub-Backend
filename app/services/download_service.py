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
import logging
import os
import re
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
    - Resume: existing `.part` (size n>0) -> `Range: bytes=n-`; 206 appends,
      200 (Range ignored) truncates/restarts, 416 promotes the `.part` as-is.
    - UA = `settings.XTREAM_USER_AGENT`; redirects followed (server-derived
      URL, no new SSRF surface vs. existing stream validation).
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
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as resp:
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


async def enqueue_selection(
    db: AsyncSession,
    *,
    media_type: str,           # 'movie' | 'show' (type of the selection)
    unification_id: str,
    server_id: str,
    rating_key: str,
    scope: str,                # 'movie' | 'series_all'
) -> EnqueueResult:
    """Resolve an operator selection into 1..N persisted `DownloadJob` rows.

    Movie -> exactly one job (`batch_id=None`). Series (`scope=series_all`)
    -> one `DownloadBatch` + one job per episode of the chosen source. Never
    raises for a "normal" failure mode (missing config/account/media/episodes)
    — those come back as `EnqueueResult(jobs=[], error=...)`.
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

    if scope == "series_all":
        show_row = (await db.execute(
            select(Media)
            .where(Media.server_id == server_id, Media.rating_key == rating_key)
            .limit(1)
        )).scalars().first()
        if show_row is None:
            return EnqueueResult(jobs=[], batch_id=None, error="Série introuvable")

        episodes = list((await db.execute(
            select(Media).where(
                Media.type == "episode",
                Media.server_id == server_id,
                Media.grandparent_rating_key == rating_key,
            )
        )).scalars().all())
        if not episodes:
            return EnqueueResult(jobs=[], batch_id=None, error="aucun épisode disponible")

        show_title, show_year = canonical_title_year(show_row)
        batch = DownloadBatch(
            id=uuid.uuid4().hex,
            media_type=media_type or "show",
            unification_id=unification_id or None,
            title=show_title,
            server_id=server_id,
            rating_key=rating_key,
            scope="series_all",
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
