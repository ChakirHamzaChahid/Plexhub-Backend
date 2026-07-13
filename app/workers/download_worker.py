"""PH-DL-04: master-only drain loop for the physical media download queue
(docs/20-impl-media-download.md §6).

Started as a single long-lived background task ONLY on the elected master
(`fcntl.flock`, same pattern as the sync/enrich/validation/plex pipeline) —
wiring lives in `app/main.py` (PH-DL-06, out of this module's scope). Every
transition/progress write goes through `run_with_retry` on a FRESH session
per attempt (same pattern as `services/unified_group_service`), so a
`database is locked` retry never carries a half-open transaction across
attempts.

Concurrency / cancellation model (spec §6.2) is entirely DB-mediated — it has
to be, since the enqueue/cancel/retry ROUTES can run on any uvicorn worker
process while only the master drains:
  - Claim:    ``UPDATE ... WHERE id AND state='queued'`` — rowcount confirms
              a single winner.
  - Progress: ``UPDATE ... SET bytes_done, bytes_total, updated_at WHERE id``
              — never touches `state`, so it can't clobber a concurrent cancel.
  - Cancel:   (route, any process) ``UPDATE ... SET state='canceled' WHERE
              state IN ('queued','running')``; this worker discovers it via
              `cancel_check` re-reading `state`.
  - Terminal: ``UPDATE ... WHERE id AND state='running'`` — a prior cancel
              already flipped `state`, so the terminal write affects 0 rows
              and the cancel wins.

Secrets invariant: the upstream Xtream URL (user/password in the query
string) is re-derived here via `stream_service.build_stream_url` and is
NEVER logged, persisted, or included in any error message — only `job_id`/
`title`/`dest` appear in log lines.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import func, select, update

from app.config import settings
from app.models.database import DownloadJob, XtreamAccount
from app.services import download_service
from app.services.download_service import (
    DownloadCanceled,
    DownloadDisabledError,
    DownloadPermanentError,
    DownloadResult,
    DownloadTransientError,
    PathConfinementError,
    resolve_confined,
)
from app.services.stream_service import build_stream_url
from app.utils.db_retry import run_with_retry
from app.utils.server_id import parse_server_id
from app.utils.tasks import create_background_task
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.download.worker")

# Persisted-progress writes are throttled to at most once per this many
# seconds per job — a 1 MiB chunk_bytes default would otherwise write the DB
# on every ~1 MiB, which is excessive for a multi-GB file.
_PROGRESS_PERSIST_INTERVAL_S = 1.0


async def reap_orphans(session_factory) -> int:
    """Boot-time (master only): any job stuck `running` belonged to a
    previous process instance that is definitely dead — requeue it so it
    isn't a permanent phantom (F-005/F-006)."""
    async def _do() -> int:
        async with session_factory() as db:
            result = await db.execute(
                update(DownloadJob)
                .where(DownloadJob.state == "running")
                .values(state="queued", updated_at=now_ms())
            )
            await db.commit()
            return result.rowcount

    reaped = await run_with_retry(_do, op="reap_orphans")
    return reaped


async def _fetch_queued(session_factory, *, limit: int, exclude_ids: set) -> list[str]:
    async def _do() -> list[str]:
        async with session_factory() as db:
            query = select(DownloadJob.id).where(DownloadJob.state == "queued")
            if exclude_ids:
                query = query.where(DownloadJob.id.notin_(exclude_ids))
            query = query.order_by(DownloadJob.created_at.asc()).limit(limit)
            return list((await db.execute(query)).scalars().all())

    return await run_with_retry(_do, op="fetch_queued")


async def run_drain_loop(session_factory) -> None:
    """Long-lived master-only coroutine: reap orphans once, then poll for
    `queued` jobs every `DOWNLOAD_POLL_INTERVAL`s and dispatch up to
    `DOWNLOAD_CONCURRENCY` concurrent transfers. Stops cleanly on
    cancellation (lifespan shutdown)."""
    if not settings.DOWNLOAD_DIR:
        logger.info("Download worker disabled: DOWNLOAD_DIR is not configured")
        return

    reaped = await reap_orphans(session_factory)
    if reaped:
        logger.info("Download worker: reaped %d orphaned running job(s) at boot", reaped)

    concurrency = max(1, settings.DOWNLOAD_CONCURRENCY)
    sem = asyncio.Semaphore(concurrency)
    in_flight: dict[str, asyncio.Task] = {}

    logger.info("Download worker: drain loop started (concurrency=%d)", concurrency)
    try:
        while True:
            try:
                for job_id in [jid for jid, task in in_flight.items() if task.done()]:
                    in_flight.pop(job_id, None)

                free_slots = concurrency - len(in_flight)
                if free_slots > 0:
                    candidates = await _fetch_queued(
                        session_factory, limit=free_slots, exclude_ids=set(in_flight),
                    )
                    for job_id in candidates:
                        task = create_background_task(
                            _run_job(session_factory, job_id, sem),
                            name=f"download_job_{job_id}",
                        )
                        in_flight[job_id] = task
            except Exception:
                # BLOQUANT fix (review): `run_with_retry` only retries
                # "database is locked" — any OTHER transient `OperationalError`
                # (WAL checkpoint contention, the nightly `sqlite3.backup`,
                # "disk image malformed"...) previously propagated straight
                # out of this loop and killed the coroutine PERMANENTLY, with
                # every queued job stuck `queued` and no recovery short of a
                # process restart. `asyncio.CancelledError` is a `BaseException`
                # in this codebase's supported Python versions (3.12/3.13), so
                # it is never caught here — shutdown still propagates first,
                # via the outer `except asyncio.CancelledError` below. Never
                # logs a URL (this tick never touches one).
                logger.error(
                    "Download worker: drain tick failed unexpectedly — will retry"
                    " next poll",
                    exc_info=True,
                )

            await asyncio.sleep(max(1, settings.DOWNLOAD_POLL_INTERVAL))
    except asyncio.CancelledError:
        logger.info("Download worker: drain loop stopping (shutdown)")
        raise


async def _claim(session_factory, job_id: str) -> bool:
    async def _do() -> int:
        async with session_factory() as db:
            now = now_ms()
            result = await db.execute(
                update(DownloadJob)
                .where(DownloadJob.id == job_id, DownloadJob.state == "queued")
                .values(
                    state="running",
                    started_at=func.coalesce(DownloadJob.started_at, now),
                    updated_at=now,
                )
            )
            await db.commit()
            return result.rowcount

    rowcount = await run_with_retry(_do, op="claim_job")
    return bool(rowcount)


async def _load_job(session_factory, job_id: str) -> DownloadJob | None:
    async def _do():
        async with session_factory() as db:
            return await db.get(DownloadJob, job_id)

    return await run_with_retry(_do, op="load_job")


async def _load_account(session_factory, server_id: str) -> XtreamAccount | None:
    account_id = parse_server_id(server_id)
    if not account_id:
        return None

    async def _do():
        async with session_factory() as db:
            result = await db.execute(
                select(XtreamAccount).where(
                    XtreamAccount.id == account_id,
                    XtreamAccount.is_active == True,  # noqa: E712
                )
            )
            return result.scalars().first()

    return await run_with_retry(_do, op="load_account")


async def _persist_progress(
    session_factory, job_id: str, bytes_done: int, bytes_total: int | None,
) -> None:
    async def _do() -> None:
        async with session_factory() as db:
            values: dict = {"bytes_done": bytes_done, "updated_at": now_ms()}
            if bytes_total is not None:
                values["bytes_total"] = bytes_total
            # Deliberately does NOT touch `state` (spec §6.2) — a concurrent
            # cancel writing `state='canceled'` must never be clobbered here.
            await db.execute(update(DownloadJob).where(DownloadJob.id == job_id).values(**values))
            await db.commit()

    try:
        await run_with_retry(_do, op="persist_progress")
    except Exception:
        # Best-effort — a missed progress tick must never abort the transfer.
        logger.debug("Download job %s: progress persist skipped", job_id, exc_info=True)


async def _is_canceled(session_factory, job_id: str) -> bool:
    async def _do() -> bool:
        async with session_factory() as db:
            state = (await db.execute(
                select(DownloadJob.state).where(DownloadJob.id == job_id)
            )).scalar()
            return state is not None and state != "running"

    try:
        return await run_with_retry(_do, op="check_canceled")
    except Exception:
        logger.debug("Download job %s: cancel-check skipped", job_id, exc_info=True)
        return False


def _safe_error(exc: Exception) -> str:
    """Map an exception to a short, bounded message for `download_job.error`.

    `download_to_disk` only ever raises typed exceptions whose `str()` is a
    message THIS codebase constructed (e.g. "upstream 404", "network
    timeout") — never the raw upstream exception repr, which could embed the
    Xtream URL. Capped defensively regardless.
    """
    message = str(exc).strip() or exc.__class__.__name__
    return message[:200]


async def _mark_completed(session_factory, job_id: str, result: DownloadResult) -> None:
    async def _do() -> None:
        async with session_factory() as db:
            now = now_ms()
            values: dict = {
                "state": "completed",
                "bytes_done": result.bytes_downloaded,
                "error": None,
                "updated_at": now,
                "finished_at": now,
            }
            if result.bytes_total is not None:
                values["bytes_total"] = result.bytes_total
            await db.execute(
                update(DownloadJob)
                .where(DownloadJob.id == job_id, DownloadJob.state == "running")
                .values(**values)
            )
            await db.commit()

    await run_with_retry(_do, op="mark_completed")
    logger.info("Download job %s: completed (%d bytes)", job_id, result.bytes_downloaded)


async def _mark_failed(session_factory, job_id: str, message: str) -> None:
    async def _do() -> None:
        async with session_factory() as db:
            now = now_ms()
            await db.execute(
                update(DownloadJob)
                .where(DownloadJob.id == job_id, DownloadJob.state == "running")
                .values(state="failed", error=message[:200], updated_at=now, finished_at=now)
            )
            await db.commit()

    await run_with_retry(_do, op="mark_failed")
    logger.warning("Download job %s: failed (%s)", job_id, message)


async def _handle_transient(session_factory, job_id: str, message: str) -> None:
    """Bump `attempts`; if still within `DOWNLOAD_MAX_RETRIES`, requeue
    IMMEDIATELY (state='queued') and only THEN back off, else mark `failed`.
    Every terminal/requeue write stays conditional on `state='running'` so a
    concurrent cancel always wins (spec §6.2).

    Majeur fix (review — HOL blocking): the caller (`_run_job`) invokes this
    AFTER releasing its concurrency-semaphore slot, and — unlike the prior
    design — the requeue write now happens BEFORE the exponential back-off
    sleep, not after. Two consequences:
      1. the job shows `queued` (not `running`) for the ENTIRE back-off
         window, so the admin UI/API never lies about a job actively
         transferring while it's really just waiting to retry;
      2. because this coroutine keeps running (sleeping) until the delay
         elapses, the drain loop's `in_flight` bookkeeping (keyed by task,
         not by DB state) still correctly excludes this `job_id` from
         `_fetch_queued` until the back-off has genuinely elapsed — true
         exponential back-off, not just "however long until the next poll
         tick". Meanwhile, since the semaphore is already released, OTHER
         queued jobs are free to claim the freed concurrency slot right away
         (DOWNLOAD_CONCURRENCY=1 no longer head-of-line-blocks the whole
         queue behind one flaky job).
    """
    async def _peek_attempts():
        async with session_factory() as db:
            job = await db.get(DownloadJob, job_id)
            if job is None or job.state != "running":
                return None
            return (job.attempts or 0) + 1

    attempts = await run_with_retry(_peek_attempts, op="handle_transient_peek")
    if attempts is None:
        return  # already canceled/gone — nothing to do

    if attempts <= settings.DOWNLOAD_MAX_RETRIES:
        delay = min(2 ** attempts, 30)
        logger.warning(
            "Download job %s: transient error (%s), retry %d/%d in %ds",
            job_id, message, attempts, settings.DOWNLOAD_MAX_RETRIES, delay,
        )

        async def _requeue() -> None:
            async with session_factory() as db:
                await db.execute(
                    update(DownloadJob)
                    .where(DownloadJob.id == job_id, DownloadJob.state == "running")
                    .values(
                        state="queued", attempts=attempts,
                        error=message[:200], updated_at=now_ms(),
                    )
                )
                await db.commit()

        await run_with_retry(_requeue, op="handle_transient_requeue")
        await asyncio.sleep(delay)
    else:
        async def _fail() -> None:
            async with session_factory() as db:
                now = now_ms()
                await db.execute(
                    update(DownloadJob)
                    .where(DownloadJob.id == job_id, DownloadJob.state == "running")
                    .values(
                        state="failed", attempts=attempts,
                        error=message[:200], updated_at=now, finished_at=now,
                    )
                )
                await db.commit()

        await run_with_retry(_fail, op="handle_transient_fail")
        logger.warning(
            "Download job %s: giving up after %d attempt(s) (%s)", job_id, attempts, message,
        )


async def _run_job(session_factory, job_id: str, sem: asyncio.Semaphore) -> None:
    # Majeur fix (review — HOL blocking, #5): set when the transfer ends in a
    # transient failure, and only acted on AFTER the `async with sem:` block
    # below has been exited — see `_handle_transient`'s docstring. Left
    # `None` on every other exit path (claim miss / not-found / permanent
    # failure / cancel / success), where no post-semaphore work is needed.
    transient_message: str | None = None

    async with sem:
        if not await _claim(session_factory, job_id):
            return  # already claimed/canceled by another dispatch

        job = await _load_job(session_factory, job_id)
        if job is None:
            return

        account = await _load_account(session_factory, job.server_id)
        if account is None:
            await _mark_failed(session_factory, job_id, "compte source introuvable ou inactif")
            return

        url = build_stream_url(account, job.rating_key)
        if not url:
            await _mark_failed(session_factory, job_id, "URL de flux introuvable")
            return

        try:
            dest = resolve_confined(job.dest_path)
        except (PathConfinementError, DownloadDisabledError) as exc:
            logger.error("Download job %s: destination rejected: %s", job_id, exc)
            await _mark_failed(session_factory, job_id, "chemin de destination invalide")
            return

        last_persist = {"t": 0.0}

        async def _on_progress(bytes_done: int, bytes_total: int | None) -> None:
            now = time.monotonic()
            if now - last_persist["t"] < _PROGRESS_PERSIST_INTERVAL_S:
                return
            last_persist["t"] = now
            await _persist_progress(session_factory, job_id, bytes_done, bytes_total)

        # Majeur fix (review — cancel-check throttle, #4): previously called
        # `_is_canceled` (fresh session + SELECT) on EVERY chunk. Throttled to
        # the same ~1 SELECT/s gate as `_on_progress` above — cross-process
        # cancellation is still observed, just not once per (potentially
        # tiny) chunk.
        last_cancel_check = {"t": 0.0, "canceled": False}

        async def _cancel_check() -> bool:
            now = time.monotonic()
            if now - last_cancel_check["t"] < _PROGRESS_PERSIST_INTERVAL_S:
                return last_cancel_check["canceled"]
            last_cancel_check["t"] = now
            last_cancel_check["canceled"] = await _is_canceled(session_factory, job_id)
            return last_cancel_check["canceled"]

        try:
            # Sécu Moyen fix (review — disk preflight, #3): checked right
            # before the transfer starts, inside the same try/except as
            # `download_to_disk` so `InsufficientDiskSpaceError` (a
            # `DownloadPermanentError` subclass) is handled identically —
            # `failed` immediately, no retry budget consumed.
            await download_service.check_free_disk_space()
            result = await download_service.download_to_disk(
                url, dest, on_progress=_on_progress, cancel_check=_cancel_check,
            )
        except DownloadCanceled:
            logger.info("Download job %s: canceled (title=%r)", job_id, job.title)
            return
        except DownloadPermanentError as exc:
            await _mark_failed(session_factory, job_id, _safe_error(exc))
            return
        except DownloadTransientError as exc:
            transient_message = _safe_error(exc)
        except Exception:
            # Defensive: a bug in the transfer primitive must never crash the
            # drain loop or leave the job stuck `running` forever.
            logger.error("Download job %s: unexpected error", job_id, exc_info=True)
            transient_message = "erreur inattendue"
        else:
            await _mark_completed(session_factory, job_id, result)
    # `sem` is released here — `_handle_transient` (peek + immediate requeue
    # + exponential back-off sleep) must NEVER run while still holding the
    # concurrency slot, or a single flaky job freezes the whole queue at
    # DOWNLOAD_CONCURRENCY=1 for up to 30s (Majeur #5).
    if transient_message is not None:
        await _handle_transient(session_factory, job_id, transient_message)
