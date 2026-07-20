"""Manual OMDb ratings backfill — Wave 3 of the dual-provider enrichment
refacto (`docs/plans/2026-07-20-omdb-rating-enrichment-design.md` §C4).

Mirrors `app.workers.embedding_worker`'s in-memory 202-job precedent
(``OrderedDict`` job store, ``JOBS_CAP``, ``create_background_task``) PLUS an
in-memory **single-run guard** (module-level flag): only ONE backfill run may
be "running" at a time, per process. Triggered exclusively by
``POST /api/admin/enrichment/omdb-backfill`` (Pattern C, master-key only,
``app/api/enrichment.py``) — never auto-runs at boot/scheduler (same
"never at boot" discipline as the embed rebuild, CLAUDE.md §9 piège 5).

- **Phase A** (`_run_phase_a`) keyset-paginates ``media`` rows that already
  have an ``imdb_id`` but no ``imdb_rating`` yet. For each, it resolves OMDb
  data cache-first / budget-gated (mirrors
  ``enrichment_worker._fetch_omdb_by_id`` line for line — fail-open on
  unconfigured/over-budget/not-found/exception, never crashes the job), then
  COALESCE fill-missing writes ``imdb_rating``/``imdb_votes`` + blends
  ``display_rating`` via the Wave-1 helper
  (``app.utils.rating_blend.blend_display_rating_case``). The UPDATE targets
  every row sharing ``(server_id, rating_key)`` (multiple ``filter``/
  ``sort_order`` pagination-cache duplicates of the same physical item),
  mirroring the exact precedent in ``nfo_import_service``/
  ``enrichment_worker._apply_enrichment_results``.
- **Phase B** (optional, ``recompute_display_rating=True``) runs
  ``recompute_display_rating_stmt()`` — SQL-only, no network — healing rows
  whose ``display_rating`` drifted from the persisted ``imdb_rating``/
  ``tmdb_rating`` columns (sync ``content_hash``-flip clobber, see design doc
  "Risks"). Idempotent: running it twice is a no-op the second time.

Process-local job store (same CR-A06 caveat as ``embedding_worker._ai_jobs``
— not shared across master/worker processes). All writers go through
``commit_with_retry`` (WAL lock safety, CLAUDE.md §9 piège 8).
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select, update

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media
from app.services import omdb_scrape_cache_service as omdb_scrape_cache
from app.services.omdb_service import omdb_service
from app.utils.db_retry import commit_with_retry
from app.utils.rating_blend import blend_display_rating_case, recompute_display_rating_stmt
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.enrichment_backfill")

PAGE_SIZE = 200
JOBS_CAP = 100
COMMIT_EVERY = 200  # matches enrichment_worker.BATCH_SIZE order of magnitude

MediaTypeFilter = Literal["movie", "show", "all"]

# Process-local job store — see module docstring (mirrors
# embedding_worker._ai_jobs / CR-A06 caveat: invisible across processes).
_jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

# In-memory single-run guard: True while a backfill run is in flight in THIS
# process. `enqueue_backfill` sets it synchronously (no `await` between the
# router's `is_running()` check and this call, so no race within the single
# asyncio event loop); `run_backfill`'s `finally` always releases it.
_running: bool = False


def is_running() -> bool:
    return _running


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_job_id() -> str:
    return f"omdb_backfill_{_now_ms()}"


def register_job(job_id: str, payload: dict[str, Any]) -> None:
    """Insert a job entry with FIFO eviction when len exceeds JOBS_CAP."""
    if job_id in _jobs:
        _jobs[job_id].update(payload)
        return
    while len(_jobs) >= JOBS_CAP:
        _jobs.popitem(last=False)  # evict oldest
    _jobs[job_id] = payload


def get_job(job_id: str) -> dict[str, Any] | None:
    return _jobs.get(job_id)


def _media_types(media_type: MediaTypeFilter) -> tuple[str, ...]:
    if media_type == "movie":
        return ("movie",)
    if media_type == "show":
        return ("show",)
    return ("movie", "show")


async def _fetch_omdb_by_id(
    imdb_id: str, session_factory,
) -> tuple[Any, tuple[str, str] | None]:
    """Cache-first, budget-gated OMDb lookup by imdb_id.

    Mirrors ``enrichment_worker._fetch_omdb_by_id`` line for line: budget
    guard first (a spent daily budget disables OMDb for the rest of the
    run), then the persistent cache, then the network. Fail-open:
    unconfigured / over budget -> ``(None, None)``; ``get_by_imdb_id`` itself
    is graceful-None on error (never raises). Returns
    ``(omdb_data, omdb_put)`` where ``omdb_put`` is ``(imdb_id, result)`` to
    persist on a FRESH HTTP call, or ``None`` on a cache-hit / budget-skip
    (nothing new to write).
    """
    if not imdb_id or not omdb_service.is_configured:
        return None, None
    if omdb_service.get_request_count() >= settings.OMDB_DAILY_LIMIT:
        return None, None
    ts = now_ms()
    async with session_factory() as cdb:
        cached = await omdb_scrape_cache.get(cdb, imdb_id, ts)
    if cached is not None:
        return cached, None
    omdb_data = await omdb_service.get_by_imdb_id(imdb_id)
    return omdb_data, (imdb_id, "found" if omdb_data is not None else "not_found")


def _bump(job_id: str, **deltas: int) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    for key, delta in deltas.items():
        job[key] = job.get(key, 0) + delta


async def _run_phase_a(
    job_id: str,
    session_factory,
    *,
    media_types: tuple[str, ...],
    limit: int | None,
) -> None:
    """Keyset-paginate incomplete media (imdb_id set, imdb_rating NULL),
    fill-missing imdb_rating/imdb_votes via OMDb, blend display_rating.

    Keyset cursor on the composite-PK columns ``(server_id, rating_key)``
    (stable, indexed ordering — no OFFSET). ``.distinct()`` collapses the
    ``filter``/``sort_order`` pagination-cache duplicate rows that share the
    same physical item (same convention as
    ``nfo_import_service``'s "de-duped on rating_key" comment) so each
    physical item is scanned/fetched once per page.

    Each page is processed in two strictly separated steps — a **fetch**
    step (every OMDb/cache lookup, each via its own short-lived session from
    ``_fetch_omdb_by_id``) followed by a **write** step (one session, all
    the writes for the page, batched commits). This mirrors
    ``enrichment_worker``'s fetch-then-apply split (`_resolve` phase fully
    resolved via ``asyncio.gather`` before ``_apply_enrichment_results`` ever
    opens its own session). Interleaving a fresh nested session INSIDE an
    open, uncommitted write session is unsafe: on a low-pool-size /
    ``StaticPool`` deployment (notably ``sqlite:///:memory:`` in tests) the
    nested session can share the write session's underlying DBAPI
    connection, and its implicit close silently rolls back the write
    session's pending, uncommitted work — a real regression caught by
    ``tests/test_enrichment_backfill.py::TestPhaseASelection`` during
    review.
    """
    cursor_server: str | None = None
    cursor_rk: str | None = None
    scanned = 0
    put_keys: set[str] = set()

    while True:
        if limit is not None:
            remaining = limit - scanned
            if remaining <= 0:
                break
            page_limit = min(PAGE_SIZE, remaining)
        else:
            page_limit = PAGE_SIZE

        async with session_factory() as db:
            stmt = (
                select(Media.server_id, Media.rating_key, Media.imdb_id)
                .distinct()
                .where(
                    Media.imdb_id.isnot(None),
                    Media.imdb_id != "",
                    Media.imdb_rating.is_(None),
                    Media.type.in_(media_types),
                )
            )
            if cursor_server is not None:
                stmt = stmt.where(
                    or_(
                        Media.server_id > cursor_server,
                        and_(Media.server_id == cursor_server, Media.rating_key > cursor_rk),
                    )
                )
            stmt = stmt.order_by(Media.server_id, Media.rating_key).limit(page_limit)
            rows = (await db.execute(stmt)).all()

        if not rows:
            break

        # --- Fetch step: resolve every row's OMDb data first. No write
        # session is open during this loop (see docstring above).
        fetched: list[tuple[str, str, Any, tuple[str, str] | None]] = []
        for server_id, rating_key, imdb_id in rows:
            cursor_server, cursor_rk = server_id, rating_key
            scanned += 1
            job = _jobs.get(job_id)
            if job is not None:
                job["scanned"] = scanned
            try:
                omdb_data, omdb_put = await _fetch_omdb_by_id(imdb_id, session_factory)
            except Exception as exc:
                logger.warning(
                    "omdb-backfill: OMDb fetch failed for %s/%s (%s)",
                    server_id, rating_key, type(exc).__name__,
                )
                _bump(job_id, errors=1)
                if job is not None:
                    job["lastError"] = f"{type(exc).__name__}: {exc}"
                continue
            fetched.append((server_id, rating_key, omdb_data, omdb_put))

        # --- Write step: one session, all writes for this page.
        pending_writes = 0
        async with session_factory() as db:
            # See enrichment_worker._apply_enrichment_results: defer autoflush
            # so an in-loop query never unexpectedly flushes a pending write
            # outside a commit_with_retry-wrapped commit.
            with db.no_autoflush:
                for server_id, rating_key, omdb_data, omdb_put in fetched:
                    job = _jobs.get(job_id)
                    try:
                        if omdb_put is not None:
                            put_imdb_id, put_result = omdb_put
                            if put_imdb_id not in put_keys:
                                put_keys.add(put_imdb_id)
                                await omdb_scrape_cache.put(
                                    db, put_imdb_id, put_result, omdb_data, now_ms(),
                                )
                                pending_writes += 1

                        if omdb_data is None:
                            continue  # not_found / unconfigured / over budget — fail-open, skip

                        _bump(job_id, omdbFetched=1)

                        new_imdb = omdb_data.imdb_rating
                        if new_imdb is None:
                            continue  # OMDb had no usable rating for this id

                        new_votes = omdb_data.imdb_votes
                        update_values: dict = {
                            "imdb_rating": func.coalesce(Media.imdb_rating, new_imdb),
                        }
                        if new_votes is not None:
                            update_values["imdb_votes"] = func.coalesce(Media.imdb_votes, new_votes)
                        # tmdb operand: the backfill never introduces a fresh
                        # tmdb_rating (that is enrichment_worker's job), so the
                        # persisted column is used as-is — mathematically the
                        # same as COALESCE(tmdb_rating, NULL).
                        update_values["display_rating"] = blend_display_rating_case(
                            func.coalesce(Media.imdb_rating, new_imdb),
                            Media.tmdb_rating,
                            Media.display_rating,
                        )
                        await db.execute(
                            update(Media)
                            .where(Media.server_id == server_id, Media.rating_key == rating_key)
                            .values(**update_values)
                        )
                        pending_writes += 1
                        _bump(job_id, imdbFilled=1)
                    except Exception as exc:
                        logger.warning(
                            "omdb-backfill: write failed for %s/%s (%s)",
                            server_id, rating_key, type(exc).__name__,
                        )
                        _bump(job_id, errors=1)
                        if job is not None:
                            job["lastError"] = f"{type(exc).__name__}: {exc}"
                        continue

                    if pending_writes >= COMMIT_EVERY:
                        await commit_with_retry(db)
                        pending_writes = 0

            if pending_writes:
                await commit_with_retry(db)

        if len(rows) < page_limit:
            break


async def run_backfill(
    job_id: str,
    session_factory,
    *,
    media_type: MediaTypeFilter = "all",
    recompute_display_rating: bool = True,
    limit: int | None = None,
) -> None:
    """Background coroutine: Phase A (OMDb fill-missing) + optional Phase B
    (SQL-only display_rating recompute).

    Wraps everything in try/except so status -> 'failed' on unexpected crash.
    Always sets ``finishedAt`` and releases the single-run guard, even on
    failure (``finally``).
    """
    global _running
    register_job(job_id, {
        "status": "running",
        "scanned": 0,
        "omdbFetched": 0,
        "imdbFilled": 0,
        "displayRecomputed": 0,
        "errors": 0,
        "lastError": None,
        "startedAt": _now_ms(),
        "finishedAt": None,
    })
    omdb_service.reset_request_count()
    try:
        await _run_phase_a(
            job_id, session_factory,
            media_types=_media_types(media_type), limit=limit,
        )
        if recompute_display_rating:
            async with session_factory() as db:
                result = await db.execute(recompute_display_rating_stmt())
                await commit_with_retry(db)
                job = _jobs.get(job_id)
                if job is not None:
                    rowcount = result.rowcount
                    job["displayRecomputed"] = rowcount if rowcount and rowcount > 0 else 0
        job = _jobs.get(job_id)
        if job is not None:
            job["status"] = "completed"
            job["finishedAt"] = _now_ms()
    except Exception as exc:
        logger.exception("omdb-backfill job %s crashed", job_id)
        job = _jobs.get(job_id)
        if job is not None:
            job["status"] = "failed"
            job["lastError"] = f"{type(exc).__name__}: {exc}"
            job["finishedAt"] = _now_ms()
    finally:
        _running = False


async def enqueue_backfill(
    *,
    media_type: MediaTypeFilter = "all",
    recompute_display_rating: bool = True,
    limit: int | None = None,
) -> str:
    """Register a new job + fire-and-forget via create_background_task.

    Caller (``api/enrichment.py``) must check ``is_running()`` first and
    return 409 on a conflict — this function itself flips the guard to
    True; there is no ``await`` between a router's ``is_running()`` check
    and this call, so no race can slip through within the single-threaded
    asyncio event loop.
    """
    from app.utils.tasks import create_background_task

    global _running
    _running = True
    job_id = _make_job_id()
    register_job(job_id, {
        "status": "queued",
        "scanned": 0,
        "omdbFetched": 0,
        "imdbFilled": 0,
        "displayRecomputed": 0,
        "errors": 0,
        "lastError": None,
        "startedAt": _now_ms(),
        "finishedAt": None,
    })
    create_background_task(
        run_backfill(
            job_id, async_session_factory,
            media_type=media_type,
            recompute_display_rating=recompute_display_rating,
            limit=limit,
        ),
        name=f"omdb-backfill-{job_id}",
    )
    return job_id
