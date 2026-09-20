"""Manual-scraper BATCH job (ADR 0005 D8, wave W5).

Walks the catalogue's un-identified items and tries to settle each one
automatically, in two passes:

- **Pass 1 — pairing.** An item that already carries exactly ONE id is the
  cheapest win in the whole system: an imdb-only row never gets a tmdb id
  from the enrichment worker (ADR 0005 F6, `enrichment_worker.py:158-161`),
  and a tmdb-only row never gets its imdb back. One TMDB call resolves the
  missing side; no text matching, no poster comparison, no ambiguity.
- **Pass 2 — scraping.** An item with NO id goes through the same
  `manual_scrape_service.search_candidates` the interactive UI uses (fewer
  poster comparisons per item, `SCRAPE_BATCH_POSTER_CANDIDATES`), and the
  PURE `decide()` rule engine says apply (rule A / rule B) or review.

Everything that cannot be settled lands in `scrape_review` with the
candidates it was scored against frozen as JSON, so the admin "À vérifier"
list renders with zero network calls.

Design constraints worth keeping:

- **Process-local job store** (`OrderedDict` + `JOBS_CAP` + a synchronous
  `_running` guard), same precedent as `embedding_worker`/
  `enrichment_backfill_worker` — invisible across processes (CR-A06), which
  is acceptable because the trigger is a single operator clicking a button
  in the admin UI.
- **Budget via ContextVar tallies** (`tmdb_service.count_requests()` /
  `omdb_service.count_requests()`), NEVER the global `real_request_count`:
  `enrichment_worker.run()` resets that counter from under us (ADR 0005 F5).
  The tally object is shared with tasks spawned from this context, so the
  concurrent item workers all count into the same budget.
- **Dry run writes NOTHING** — not a media row, not a review row, not a
  scrape cache entry. It exists to produce the pHash distance histogram that
  calibrates `SCRAPE_BATCH_POSTER_ONLY_AUTO` (rule B) before that flag is
  ever turned on.
- **One rebuild per touched media_type at the very end**
  (`schedule_rebuild(delay=0)` + `flush_scheduled()`), not one per item:
  every `apply_candidate` call passes `schedule_rebuild=False`.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any, Callable, Literal

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import Media, ScrapeReview
from app.services import manual_scrape_service as mss
from app.services import unified_group_service
from app.services.omdb_service import count_requests as omdb_count_requests
from app.services.tmdb_service import count_requests as tmdb_count_requests
from app.utils.tasks import create_background_task
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.manual_scrape_batch")

JOBS_CAP = 20
PAGE_SIZE = 100

MediaKind = Literal["movie", "show"]

# Buckets of the best (smallest) pHash distance seen per item — the dry-run
# output an operator reads before enabling rule B. "unknown" covers items
# where no poster comparison could be made at all.
DISTANCE_BUCKETS = ("0-3", "4-6", "7-12", "13-20", "21+", "unknown")


class BatchAlreadyRunningError(Exception):
    """A batch is already in flight in this process."""


_jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
# Synchronous single-run guard: `start()` sets it with no `await` between the
# check and the set, so two concurrent POSTs on the single event loop cannot
# both win.
_running: bool = False


def is_running() -> bool:
    return _running


def get(job_id: str) -> dict[str, Any] | None:
    return _jobs.get(job_id)


def get_latest() -> dict[str, Any] | None:
    if not _jobs:
        return None
    return next(reversed(_jobs.values()))


def cancel(job_id: str) -> bool:
    """Cooperative cancel: the flag is checked between items, so an in-flight
    item always finishes (its write is already committed or not at all)."""
    job = _jobs.get(job_id)
    if job is None or job["status"] not in ("queued", "running"):
        return False
    job["cancelRequested"] = True
    return True


def _register(job_id: str, payload: dict[str, Any]) -> None:
    while len(_jobs) >= JOBS_CAP:
        _jobs.popitem(last=False)  # FIFO eviction, oldest first
    _jobs[job_id] = payload


def _new_job(job_id: str, *, media_type: str, dry_run: bool) -> dict[str, Any]:
    return {
        "jobId": job_id,
        "status": "queued",
        "dryRun": dry_run,
        "mediaType": media_type,
        "phase": "pairing",
        "scanned": 0,
        "paired": 0,
        "autoAppliedA": 0,
        "autoAppliedB": 0,
        "queuedForReview": 0,
        "skippedLocked": 0,
        "errors": 0,
        "lastError": None,
        "tmdbCalls": 0,
        "omdbCalls": 0,
        "tmdbBudget": settings.SCRAPE_BATCH_TMDB_LIMIT,
        "budgetExhausted": False,
        "distanceHistogram": {b: 0 for b in DISTANCE_BUCKETS},
        "cancelRequested": False,
        "startedAt": now_ms(),
        "finishedAt": None,
    }


def start(
    *,
    media_type: MediaKind | Literal["all"],
    dry_run: bool,
    max_items: int | None = None,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> str:
    """Register and launch a batch run; returns its jobId.

    Raises `BatchAlreadyRunningError` if one is already in flight (the admin
    route maps that to 409). `session_factory` defaults to the app factory,
    resolved HERE rather than at import time so tests can inject theirs."""
    global _running
    if _running:
        raise BatchAlreadyRunningError("a manual-scrape batch is already running")

    if session_factory is None:
        from app.db.database import async_session_factory

        session_factory = async_session_factory

    job_id = f"scrape_batch_{now_ms()}"
    _register(job_id, _new_job(job_id, media_type=media_type, dry_run=dry_run))
    _running = True  # set synchronously, before the first await (see `_running`)

    create_background_task(
        run(
            job_id,
            session_factory=session_factory,
            media_type=media_type,
            dry_run=dry_run,
            max_items=max_items,
        ),
        name=job_id,
    )
    return job_id


def _media_types(media_type: str) -> tuple[str, ...]:
    if media_type == "movie":
        return ("movie",)
    if media_type == "show":
        return ("show",)
    return ("movie", "show")


_IMDB_MISSING = or_(Media.imdb_id.is_(None), Media.imdb_id == "")
_TMDB_MISSING = or_(Media.tmdb_id.is_(None), Media.tmdb_id == "")
_IMDB_PRESENT = and_(Media.imdb_id.isnot(None), Media.imdb_id != "")
_TMDB_PRESENT = and_(Media.tmdb_id.isnot(None), Media.tmdb_id != "")


def _no_open_review():
    """Skip items already in the queue (`pending`) or explicitly set aside by
    the operator (`dismissed`) — re-scraping either would either churn the
    same review row or override a deliberate "leave it alone"."""
    return ~exists(
        select(ScrapeReview.rating_key)
        .where(
            ScrapeReview.rating_key == Media.rating_key,
            ScrapeReview.server_id == Media.server_id,
            ScrapeReview.status.in_(("pending", "dismissed")),
        )
        .correlate(Media)
    )


def _selection_where(media_types: tuple[str, ...], *, pass_one: bool):
    id_clause = (
        or_(
            and_(_IMDB_PRESENT, _TMDB_MISSING),
            and_(_TMDB_PRESENT, _IMDB_MISSING),
        )
        if pass_one
        else and_(_IMDB_MISSING, _TMDB_MISSING)
    )
    return [
        Media.type.in_(media_types),
        Media.is_in_allowed_categories == True,  # noqa: E712
        # Never touch a row an operator fixed by hand (ADR 0005 D7).
        Media.match_locked == False,  # noqa: E712
        id_clause,
        _no_open_review(),
    ]


async def _fetch_page(
    session_factory, media_types: tuple[str, ...], *, pass_one: bool,
    cursor: tuple[str, str] | None, limit: int,
) -> list[Any]:
    """One keyset page of DISTINCT items (ADR 0005 F1: `GROUP BY
    server_id, rating_key`, since `media`'s PK has four columns)."""
    stmt = (
        select(
            Media.server_id, Media.rating_key, Media.type, Media.title,
            Media.year, Media.imdb_id, Media.tmdb_id, Media.thumb_url,
            Media.summary,
        )
        .where(*_selection_where(media_types, pass_one=pass_one))
        .group_by(Media.server_id, Media.rating_key)
    )
    if cursor is not None:
        stmt = stmt.where(
            or_(
                Media.server_id > cursor[0],
                and_(Media.server_id == cursor[0], Media.rating_key > cursor[1]),
            )
        )
    stmt = stmt.order_by(Media.server_id, Media.rating_key).limit(limit)
    async with session_factory() as db:
        return list((await db.execute(stmt)).all())


def _bucket(distance: int | None) -> str:
    if distance is None:
        return "unknown"
    if distance <= 3:
        return "0-3"
    if distance <= 6:
        return "4-6"
    if distance <= 12:
        return "7-12"
    if distance <= 20:
        return "13-20"
    return "21+"


def _kind(media_type: str) -> Literal["movie", "tv"]:
    return "movie" if media_type == "movie" else "tv"


async def _pair_one(job: dict[str, Any], row, *, session_factory, dry_run: bool) -> None:
    """Pass 1 — resolve the missing id of an item that has exactly one."""
    media_type = "movie" if (row.type or "") == "movie" else "show"
    imdb_id = (row.imdb_id or "").strip() or None
    tmdb_id = (row.tmdb_id or "").strip() or None
    tmdb = mss.tmdb_service

    resolved_tmdb: int | None = None
    resolved_imdb: str | None = None
    reason: str | None = None

    if imdb_id and not tmdb_id:
        resolved_tmdb = await tmdb.find_by_imdb_id(imdb_id, _kind(media_type))
        resolved_imdb = imdb_id
        if resolved_tmdb is None:
            reason = "no_tmdb_for_imdb"
    else:
        try:
            resolved_tmdb = int(tmdb_id) if tmdb_id else None
        except ValueError:
            resolved_tmdb = None
        if resolved_tmdb is None:
            reason = "no_imdb_for_tmdb"
        else:
            extras = await tmdb.get_match_extras(resolved_tmdb, _kind(media_type))
            resolved_imdb = extras.imdb_id if extras is not None else None
            if not resolved_imdb:
                reason = "no_imdb_for_tmdb"

    if reason is not None:
        await _queue_review(
            job, row, media_type=media_type, reason=reason, candidates=[],
            session_factory=session_factory, dry_run=dry_run,
        )
        return

    job["paired"] += 1
    if dry_run:
        return

    outcome = await mss.apply_candidate(
        server_id=row.server_id, rating_key=row.rating_key, media_type=media_type,
        tmdb_id=resolved_tmdb, imdb_id=resolved_imdb,
        source="batch_pair", force=False, propagate=False,
        schedule_rebuild=False, session_factory=session_factory,
    )
    _account_outcome(job, outcome)


async def _scrape_one(job: dict[str, Any], row, *, session_factory, dry_run: bool) -> None:
    """Pass 2 — full candidate search + `decide()` on an item with no id."""
    media_type = "movie" if (row.type or "") == "movie" else "show"
    query = mss.ScrapeQuery(
        media_type=media_type, title=(row.title or "").strip(), year=row.year,
    )
    result = await mss.search_candidates(
        query,
        xtream_poster_url=row.thumb_url,
        summary=row.summary,
        poster_top_n=settings.SCRAPE_BATCH_POSTER_CANDIDATES,
    )

    distances = [
        c.poster.phash_distance for c in result.candidates
        if c.poster is not None and c.poster.phash_distance is not None
    ]
    job["distanceHistogram"][_bucket(min(distances) if distances else None)] += 1

    decision = mss.decide(
        result, poster_only_auto=settings.SCRAPE_BATCH_POSTER_ONLY_AUTO,
    )

    if decision.action == "review" or decision.candidate is None:
        await _queue_review(
            job, row, media_type=media_type, reason=decision.reason,
            candidates=result.candidates, session_factory=session_factory,
            dry_run=dry_run,
        )
        return

    candidate = decision.candidate
    if decision.rule == "A":
        job["autoAppliedA"] += 1
    else:
        job["autoAppliedB"] += 1
    if dry_run:
        return

    outcome = await mss.apply_candidate(
        server_id=row.server_id, rating_key=row.rating_key, media_type=media_type,
        tmdb_id=candidate.tmdb_id, imdb_id=candidate.imdb_id,
        source="batch_auto", confidence=candidate.combined_score,
        force=False, propagate=False, schedule_rebuild=False,
        session_factory=session_factory,
    )
    _account_outcome(job, outcome)


def _account_outcome(job: dict[str, Any], outcome) -> None:
    """A batch write never forces: a row an operator locked between the scan
    and the write comes back `skipped_locked` (the UPDATE's own
    `match_locked = 0` guard), and a conflicting identity comes back
    `conflict` — both are counted, neither is an error."""
    if outcome.status == "skipped_locked":
        job["skippedLocked"] += 1
    elif outcome.status != "applied":
        job["errors"] += 1
        job["lastError"] = f"{outcome.status} on {outcome.server_id}/{outcome.rating_key}"


async def _queue_review(
    job: dict[str, Any], row, *, media_type: str, reason: str,
    candidates: list, session_factory, dry_run: bool,
) -> None:
    job["queuedForReview"] += 1
    if dry_run:
        return
    await mss.upsert_review(
        server_id=row.server_id, rating_key=row.rating_key,
        media_type=media_type, title=row.title, year=row.year,
        reason=reason, candidates=candidates, session_factory=session_factory,
    )


async def _run_pass(
    job_id: str,
    *,
    session_factory,
    media_types: tuple[str, ...],
    pass_one: bool,
    dry_run: bool,
    remaining: int,
    tally,
    touched: set[str],
) -> int:
    """Run one pass to completion (or until budget/cancel/limit). Returns the
    number of items scanned."""
    job = _jobs[job_id]
    cursor: tuple[str, str] | None = None
    scanned = 0
    semaphore = asyncio.Semaphore(max(1, settings.SCRAPE_BATCH_CONCURRENCY))

    async def _handle(row) -> None:
        async with semaphore:
            # Checked INSIDE the semaphore, i.e. immediately before the item
            # is actually worked on: a cancel or an exhausted budget must not
            # be honoured only after every already-gathered item has run.
            if job["cancelRequested"] or job["budgetExhausted"]:
                return
            if tally.count >= settings.SCRAPE_BATCH_TMDB_LIMIT:
                job["budgetExhausted"] = True
                return
            try:
                if pass_one:
                    await _pair_one(
                        job, row, session_factory=session_factory, dry_run=dry_run,
                    )
                else:
                    await _scrape_one(
                        job, row, session_factory=session_factory, dry_run=dry_run,
                    )
            except Exception as exc:  # one bad item never kills the run
                job["errors"] += 1
                # Type only: TMDB/OMDb exception strings embed the request URL,
                # which carries the api key (ADR 0005 F2).
                job["lastError"] = (
                    f"{type(exc).__name__} on {row.server_id}/{row.rating_key}"
                )
                logger.warning(
                    "scrape-batch: item %s/%s failed (%s)",
                    row.server_id, row.rating_key, type(exc).__name__,
                )
            else:
                touched.add("movie" if (row.type or "") == "movie" else "show")
            finally:
                job["scanned"] += 1

    while remaining > 0:
        if job["cancelRequested"] or job["budgetExhausted"]:
            break
        page = await _fetch_page(
            session_factory, media_types, pass_one=pass_one,
            cursor=cursor, limit=min(PAGE_SIZE, remaining),
        )
        if not page:
            break
        cursor = (page[-1].server_id, page[-1].rating_key)
        await asyncio.gather(*(_handle(row) for row in page))
        scanned += len(page)
        remaining -= len(page)
        job["tmdbCalls"] = tally.count

    return scanned


async def run(
    job_id: str,
    *,
    session_factory,
    media_type: MediaKind | Literal["all"] = "all",
    dry_run: bool = False,
    max_items: int | None = None,
) -> None:
    """Execute the batch. Never raises: every failure is recorded on the job."""
    global _running
    job = _jobs.get(job_id)
    if job is None:  # evicted between start() and here (JOBS_CAP) — nothing to report
        _running = False
        return

    job["status"] = "running"
    media_types = _media_types(media_type)
    cap = settings.SCRAPE_BATCH_MAX_ITEMS
    budget_items = min(max_items, cap) if max_items is not None else cap
    touched: set[str] = set()

    try:
        # A SINGLE tally context wraps both passes: the TMDB budget is for the
        # whole run, and the ContextVar propagates into the per-item tasks
        # spawned by `asyncio.gather` below (context copy => same object).
        with tmdb_count_requests() as tmdb_tally, omdb_count_requests() as omdb_tally:
            job["phase"] = "pairing"
            scanned = await _run_pass(
                job_id, session_factory=session_factory, media_types=media_types,
                pass_one=True, dry_run=dry_run, remaining=budget_items,
                tally=tmdb_tally, touched=touched,
            )
            if not job["cancelRequested"]:
                job["phase"] = "scraping"
                await _run_pass(
                    job_id, session_factory=session_factory, media_types=media_types,
                    pass_one=False, dry_run=dry_run,
                    remaining=max(0, budget_items - scanned),
                    tally=tmdb_tally, touched=touched,
                )
            job["tmdbCalls"] = tmdb_tally.count
            job["omdbCalls"] = omdb_tally.count

        if touched and not dry_run:
            # ONE rebuild per touched type, at the end (every apply above
            # passed schedule_rebuild=False). delay=0 + flush: the job is only
            # reported "completed" once the unified snapshot actually reflects
            # what it wrote.
            job["phase"] = "rebuild"
            for mt in sorted(touched):
                unified_group_service.schedule_rebuild(
                    mt, session_factory=session_factory, delay=0,
                )
            await unified_group_service.flush_scheduled()

        job["status"] = "canceled" if job["cancelRequested"] else "completed"
    except asyncio.CancelledError:
        job["status"] = "canceled"
        raise
    except Exception as exc:
        job["status"] = "failed"
        job["errors"] += 1
        job["lastError"] = f"{type(exc).__name__}: {exc}"
        logger.error("scrape-batch %s failed", job_id, exc_info=True)
    finally:
        job["phase"] = "done"
        job["finishedAt"] = now_ms()
        _running = False


__all__ = [
    "BatchAlreadyRunningError", "JOBS_CAP", "DISTANCE_BUCKETS",
    "is_running", "start", "get", "get_latest", "cancel", "run",
]
