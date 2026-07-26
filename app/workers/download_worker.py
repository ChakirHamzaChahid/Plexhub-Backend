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

Metrics (S5.2, AUDIT-P8-002/F-103 — `docs/plans/2026-07-26-refacto-audit-v1-plan.md`
§VAGUE 5 S5.2). All three metrics are DECLARED and zero-initialised by S1.4
(`app/utils/metrics.py`); this module only imports and increments/sets them:

  - ``plexhub_download_jobs{state}`` (queue depth) is a Gauge refreshed from a
    live ``SELECT state, COUNT(*) FROM download_job GROUP BY state`` snapshot
    once per drain tick (`_refresh_queue_depth_gauge`), NOT maintained as a
    hand-incremented/decremented counter at every state transition. Two
    reasons: (1) a state machine this shape (queued->running->completed/
    failed/canceled, plus a requeue-on-transient-retry edge, plus
    cross-process cancel/retry/clear_finished writers in
    `download_service.py` that can run on ANY uvicorn worker) is exactly the
    kind of bookkeeping that silently drifts the moment one transition is
    missed or double-counted; a periodic COUNT(*) can never drift, it just
    re-derives truth every tick. (2) more fundamentally, `prometheus_client`
    keeps an IN-PROCESS registry — `cancel_job`/`retry_job`/`clear_finished`
    (request-path, `download_service.py`) can run on a DIFFERENT uvicorn
    worker process than the one holding the master election and draining the
    queue, so an inc/dec approach would update a gauge in a process whose
    `/metrics` may not even be the one an operator scrapes; a periodic read
    done ONLY by the process that already owns draining sidesteps that
    entirely. Refresh cadence = `DOWNLOAD_POLL_INTERVAL` (same cadence the
    loop already polls for new work at).
  - ``plexhub_download_bytes_total`` is incremented in
    `download_service.download_to_disk`'s chunk loop (real bytes actually
    written to `.part` this call — see that module for why).
  - ``plexhub_download_failures_total{reason}`` is incremented here, at the
    three points a job's outcome is known: the `PathConfinementError`/
    `DownloadDisabledError` except-branch (reason="confinement", by
    exception TYPE — unambiguous, no string match needed), the
    `DownloadPermanentError` except-branch, and `_handle_transient` (one
    increment per transient ATTEMPT, whether it goes on to retry or
    eventually gives up — this surfaces retry churn that self-heals via
    back-off, which a "final failed state only" counter would hide). The
    last two go through `download_service.classify_failure_reason`, which
    returns `None` (no increment) for any failure mode outside the 4 CLOSED
    label values (`http_403|disk_full|timeout|confinement`) — see that
    function's docstring for the known gap (404/5xx/network-error/missing-
    source failures are real but currently unlabelled by this metric).

Never labels/logs a URL, `rating_key`, filename, or full `server_id` (piège
§9-17c) — `reason` is a closed 4-value enum, `state` a closed 5-value enum,
`plexhub_download_bytes_total` carries no labels at all.

Worker-is-not-master caveat: this whole module's code only ever RUNS inside
the process holding the `fcntl` master election (`app/main.py`'s lifespan
gates `run_drain_loop` under `if is_master:` — piège §9-7). A slave
process's own `/metrics` therefore keeps these three series at their S1.4
zero-init value FOREVER — indistinguishable, on that instance alone, from a
genuinely empty/idle queue. This mirrors the exact same caveat already
documented for `plexhub_pipeline_last_success_timestamp_seconds`/
`plexhub_is_master` (S5.1): the fix is NOT per-metric, it's operational —
alert/dashboard by `instance` label filtered to (or joined with)
`plexhub_is_master == 1`, not by scraping every replica's queue-depth gauge
independently.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from sqlalchemy import func, select, update

from app.config import settings
from app.models.database import DownloadJob, Media, PlexMediaItem, XtreamAccount
from app.services import download_service, plex_download_service
from app.services.download_nfo import render_media_nfo, render_plex_media_nfo
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
from app.utils import metrics
from app.utils.db_retry import run_with_retry
from app.utils.server_id import is_plex_server_id, parse_server_id
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


# S5.2 — single source of truth for the 5 `DownloadJob.state` values, shared
# with `plexhub_download_jobs{state}`'s zero-init in `app/utils/metrics.py`
# (which hand-maintains its own identical tuple — the two are not
# structurally linked, but both mirror the same `NON_TERMINAL_STATES` +
# `TERMINAL_STATES` from `download_service.py`, so drifting the state
# machine itself would already need touching those two, closer, tuples).
_QUEUE_STATES: tuple[str, ...] = (
    download_service.NON_TERMINAL_STATES + download_service.TERMINAL_STATES
)


async def _refresh_queue_depth_gauge(session_factory) -> None:
    """Set ``plexhub_download_jobs{state}`` from a live ``SELECT state,
    COUNT(*) FROM download_job GROUP BY state`` snapshot (see module
    docstring for why this is a periodic re-derivation rather than a
    hand-incremented/decremented counter). Best-effort: a missed refresh
    tick must never abort the drain loop — the next tick (≤
    `DOWNLOAD_POLL_INTERVAL` later) re-derives truth from scratch anyway, so
    there is nothing to compensate for on the next successful read.
    """
    async def _do() -> dict[str, int]:
        async with session_factory() as db:
            rows = (
                await db.execute(
                    select(DownloadJob.state, func.count())
                    .group_by(DownloadJob.state)
                )
            ).all()
            return {state: count for state, count in rows}

    try:
        counts = await run_with_retry(_do, op="queue_depth_gauge")
    except Exception:
        logger.debug("Download worker: queue depth gauge refresh skipped", exc_info=True)
        return

    for state in _QUEUE_STATES:
        metrics.download_jobs.labels(state=state).set(counts.get(state, 0))


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

    # S5.2: establish the queue-depth gauge baseline as soon as the drain
    # loop is live, instead of waiting up to one full `DOWNLOAD_POLL_INTERVAL`
    # for the first tick — orphans just got reaped above, so this reflects
    # their requeue immediately.
    await _refresh_queue_depth_gauge(session_factory)

    concurrency = max(1, settings.DOWNLOAD_CONCURRENCY)
    sem = asyncio.Semaphore(concurrency)
    in_flight: dict[str, asyncio.Task] = {}

    logger.info("Download worker: drain loop started (concurrency=%d)", concurrency)
    try:
        while True:
            try:
                # S5.2: refreshed once per tick (≈ every DOWNLOAD_POLL_INTERVAL)
                # from a live COUNT(*) GROUP BY state — see module docstring +
                # `_refresh_queue_depth_gauge` for why this is a periodic read
                # rather than a hand-maintained counter.
                await _refresh_queue_depth_gauge(session_factory)

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


async def _load_media(session_factory, server_id: str, rating_key: str) -> Media | None:
    async def _do():
        async with session_factory() as db:
            result = await db.execute(
                select(Media).where(
                    Media.server_id == server_id,
                    Media.rating_key == rating_key,
                ).limit(1)
            )
            return result.scalars().first()

    return await run_with_retry(_do, op="load_media")


async def _load_plex_media_item(
    session_factory, server_id: str, rating_key: str,
) -> PlexMediaItem | None:
    async def _do():
        async with session_factory() as db:
            return await db.get(PlexMediaItem, (server_id, rating_key))

    return await run_with_retry(_do, op="load_plex_media_item")


def _write_nfo_text(path: Path, text: str) -> None:
    """Atomically write the sidecar .nfo (tmp in same dir + os.replace). Sync —
    call via ``asyncio.to_thread``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


async def _resolve_sidecar_nfo_xml(session_factory, job: DownloadJob) -> str | None:
    """Build the sidecar ``.nfo`` XML for *job*, from whichever catalogue owns
    it: Plex jobs (``plex_*`` server_id) read ``plex_media_item`` (board
    DL-PLEX-03 — previously skipped because ``Media`` never holds a Plex row),
    Xtream jobs read ``Media``. Returns ``None`` when the row is missing or the
    type has no per-file NFO."""
    if is_plex_server_id(job.server_id):
        item = await _load_plex_media_item(session_factory, job.server_id, job.rating_key)
        return render_plex_media_nfo(item) if item is not None else None
    media = await _load_media(session_factory, job.server_id, job.rating_key)
    return render_media_nfo(media) if media is not None else None


async def _write_sidecar_nfo(session_factory, job: DownloadJob, dest: Path) -> None:
    """Best-effort: write a ``.nfo`` next to a just-completed download.

    Never raises — a missing/garbled NFO must not fail the download. The NFO
    sits in the SAME confined directory as ``dest`` (only the suffix changes),
    so it inherits ``dest``'s path-confinement with no extra check. Covers both
    Xtream and Plex jobs (see ``_resolve_sidecar_nfo_xml``).
    """
    try:
        xml = await _resolve_sidecar_nfo_xml(session_factory, job)
        if not xml:
            return
        nfo_path = dest.with_suffix(".nfo")
        await asyncio.to_thread(_write_nfo_text, nfo_path, xml)
        logger.info("Download job %s: wrote sidecar NFO %s", job.id, nfo_path.name)
    except Exception:
        logger.warning(
            "Download job %s: sidecar NFO generation failed (non-fatal)",
            job.id, exc_info=True,
        )


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
    # S5.2: single hook for every call site of `_mark_failed` (permanent
    # transfer errors AND the pre-transfer "not found" messages below) —
    # `classify_failure_reason` returns `None` for anything outside the 4
    # CLOSED label values, so this is a no-op (not a miscount) for the
    # pre-transfer messages ("compte source introuvable...", "URL de flux
    # introuvable", "source Plex introuvable...") and for permanent errors
    # outside the mapped set (404, 5xx, unsafe redirect, bad content-type).
    # `confinement` is NOT classified from `message` here (it never matches
    # any pattern) — the confinement except-branch in `_run_job` increments
    # it directly from the exception TYPE instead.
    reason = download_service.classify_failure_reason(message)
    if reason is not None:
        metrics.download_failures_total.labels(reason=reason).inc()


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
    # S5.2: one increment per transient ATTEMPT (not per eventual outcome) —
    # a job that times out twice then succeeds on the third try still
    # surfaces 2 real failure events, which a "count only the final `failed`
    # state" approach would hide entirely. Counted even if the job turns out
    # to already be canceled below (`attempts is None`): the transfer attempt
    # genuinely failed with this error regardless of what happened to the
    # job concurrently.
    reason = download_service.classify_failure_reason(message)
    if reason is not None:
        metrics.download_failures_total.labels(reason=reason).inc()

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

        # Direct-stream fallbacks (Plex download-disabled recovery); Xtream
        # jobs never populate this, so their transfer is byte-for-byte
        # unchanged (download_to_disk treats [] like the old single-URL call).
        fallback_urls: list[str] = []
        if is_plex_server_id(job.server_id):
            plex_urls = await plex_download_service.resolve_job_urls(session_factory, job)
            if not plex_urls:
                await _mark_failed(session_factory, job_id, "source Plex introuvable ou non synchronisée")
                return
            # [0] = original-file `?download=1` URL (share allows downloads);
            # the rest are direct-stream fallbacks download_to_disk switches to
            # on a 403 — see plex_download_service.resolve_job_urls.
            url = plex_urls[0]
            fallback_urls = plex_urls[1:]
        else:
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
            # S5.2: classified by exception TYPE here (unambiguous), not by
            # `_mark_failed`'s generic message-based `classify_failure_reason`
            # — "chemin de destination invalide" never matches any of its
            # patterns, so this increment does not double up with the one
            # `_mark_failed` would otherwise skip for this message.
            metrics.download_failures_total.labels(reason="confinement").inc()
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
                fallback_urls=fallback_urls,
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
            # Sidecar .nfo next to the finished file — best-effort, after the
            # job is already marked completed so it can never turn a good
            # download into a failure.
            await _write_sidecar_nfo(session_factory, job, dest)
    # `sem` is released here — `_handle_transient` (peek + immediate requeue
    # + exponential back-off sleep) must NEVER run while still holding the
    # concurrency slot, or a single flaky job freezes the whole queue at
    # DOWNLOAD_CONCURRENCY=1 for up to 30s (Majeur #5).
    if transient_message is not None:
        await _handle_transient(session_factory, job_id, transient_message)
