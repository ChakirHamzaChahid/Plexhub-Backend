"""CR-P01 builder: materialize the unified-group snapshot tables.

Runs the SAME whole-catalog aggregation the live ``/movies|shows/unified`` path
uses (``aggregate_movies`` + ``_converge``) and persists it into
``media_group`` (one row per converged group) + ``media_group_member`` (its
member pointers). The browse endpoints then page over the snapshot with a DB
``LIMIT`` instead of loading + aggregating the entire catalog per request.

Rebuilt at pipeline time (after enrichment/generation, so the snapshot reflects
the fully-enriched catalog — the same freshness as the generated Plex library).
The read path falls back to live aggregation whenever the snapshot is empty, so
a never-built / stale snapshot is always safe.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, insert, select

from app.config import settings
from app.models.database import Media, MediaGroup, MediaGroupMember
from app.services.aggregation_service import aggregate_movies
from app.utils.db_retry import run_with_retry
from app.utils.tasks import create_background_task
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.unified_group")

# The unified LIST endpoints group these two types (episodes are handled
# per-show by get_unified_episodes, not by this snapshot). Both go through
# aggregate_movies — it groups Media rows generically by unification key.
GROUP_MEDIA_TYPES = ("movie", "show")


async def rebuild(db: AsyncSession, media_type: str) -> int:
    """Rebuild the snapshot for one media_type on the given session (no commit).

    Loads every category-allowed row of *media_type*, aggregates it off the
    event loop (CR-P01), then atomically replaces that type's snapshot rows.
    Returns the number of groups written. The caller commits."""
    rows = list((await db.execute(
        select(Media).where(
            Media.type == media_type,
            Media.is_in_allowed_categories == True,  # noqa: E712
        )
    )).scalars().all())

    # Same CPU-bound aggregation as the live path — offloaded so a large catalog
    # doesn't stall the event loop while the pipeline builds the snapshot.
    groups = await asyncio.to_thread(aggregate_movies, rows)
    built_at = now_ms()

    group_values = [
        {
            "media_type": media_type,
            "group_key": g.key,
            "sort_added_at": int(g.best.added_at or 0),
            "version_count": len(g.members),
            "built_at": built_at,
        }
        for g in groups
    ]

    # A single physical item listed by the provider under N synced categories
    # produces N `media` rows that share (server_id, rating_key) but differ on
    # `filter` (= category_id — Media's real PK is
    # (rating_key, server_id, filter, sort_order), NOT (server_id, rating_key)).
    # They carry the same unification_id, so aggregate_movies groups them as
    # multiple members. media_group_member is keyed by (server_id, rating_key)
    # only, so we store ONE pointer per (server_id, rating_key) — the read path's
    # (server_id, rating_key) IN-join re-loads ALL of that item's filter variants
    # and re-aggregates them back into the same versions[] the live path builds,
    # so dedup here is loss-less AND avoids a media_group_member PK collision.
    member_values = []
    for g in groups:
        seen_member_pk: set[tuple] = set()
        for m in g.members:
            pk = (m.server_id, m.rating_key)
            if pk in seen_member_pk:
                continue
            seen_member_pk.add(pk)
            member_values.append({
                "media_type": media_type,
                "group_key": g.key,
                "server_id": m.server_id,
                "rating_key": m.rating_key,
            })

    # Replace this type's snapshot atomically (delete members first — no FK, but
    # keeps the two tables consistent if anything reads mid-transaction).
    await db.execute(
        delete(MediaGroupMember).where(MediaGroupMember.media_type == media_type)
    )
    await db.execute(delete(MediaGroup).where(MediaGroup.media_type == media_type))
    if group_values:
        await db.execute(insert(MediaGroup), group_values)
    if member_values:
        await db.execute(insert(MediaGroupMember), member_values)

    return len(groups)


async def _rebuild_one(media_type: str, session_factory: Callable[[], AsyncSession]) -> int:
    """(Re)build + commit ONE media_type's snapshot in its own lock-retried
    transaction on a FRESH session — so a `database is locked` retry re-opens
    a clean session (no lingering ``PendingRollbackError``). Shared by
    `rebuild_all` (every type) and `schedule_rebuild` (one type, debounced,
    ADR 0005 D9)."""

    async def _attempt() -> int:
        async with session_factory() as db:
            n = await rebuild(db, media_type)
            await db.commit()
            return n

    return await run_with_retry(_attempt, op=f"rebuild_media_group[{media_type}]")


async def rebuild_all(session_factory) -> dict[str, int]:
    """Rebuild the snapshot for every grouped media_type.

    Each type is (re)built + committed in its OWN lock-retried transaction on a
    FRESH session — so a `database is locked` retry re-opens a clean session
    (no lingering ``PendingRollbackError``) and one type's failure never
    half-writes another's."""
    counts: dict[str, int] = {}
    for media_type in GROUP_MEDIA_TYPES:
        # Isolate each type: a failure building one (e.g. an unexpected data
        # shape) must not prevent the other from being built — nor leave the
        # whole snapshot empty. The unified list falls back to live aggregation
        # for any type left unbuilt.
        try:
            counts[media_type] = await _rebuild_one(media_type, session_factory)
            logger.info(
                "Unified-group snapshot: %s -> %d groups", media_type, counts[media_type]
            )
        except Exception:
            logger.error(
                "Unified-group snapshot rebuild failed for %s (browsing falls back "
                "to live aggregation for this type)", media_type, exc_info=True,
            )
    return counts


# ─── ADR 0005 D9 — debounced single-type rebuild (manual-scrape apply) ──────
#
# `apply_candidate`/`unlock`/`clear_ids` (and the future batch worker) each
# touch ONE media_group at a time, but an operator working through several
# rows (or a propagate=True fan-out) can trigger several applies within a
# couple of seconds. Rebuilding the WHOLE catalog's snapshot per write would
# be wasteful; a plain per-write rebuild would also race itself (two
# concurrent full-table DELETE+INSERT passes for the same media_type). A
# trailing-edge debounce collapses a burst into ONE rebuild per type, run
# `delay` seconds after the LAST call in the burst.
_pending_rebuild_tasks: dict[str, asyncio.Task] = {}
_pending_session_factories: dict[str, Callable[[], AsyncSession]] = {}
_running_rebuild_tasks: dict[str, asyncio.Task] = {}


async def _debounced_rebuild(media_type: str, delay: float, session_factory) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # Superseded by a newer schedule_rebuild() call for this type before
        # the debounce window elapsed — that newer task already took over
        # `_pending_rebuild_tasks[media_type]`; this one has nothing left to
        # do (never re-add itself to `_running_rebuild_tasks`).
        return
    # From here on this task IS the in-flight rebuild for `media_type` — a
    # NEW schedule_rebuild() call must no longer cancel it (D9: "une
    # reconstruction DÉJÀ en cours n'est jamais annulée"), only start
    # another one to run after it.
    _pending_rebuild_tasks.pop(media_type, None)
    _pending_session_factories.pop(media_type, None)
    current = asyncio.current_task()
    if current is not None:
        _running_rebuild_tasks[media_type] = current
    try:
        n = await _rebuild_one(media_type, session_factory)
        logger.info(
            "Unified-group snapshot (debounced): %s -> %d groups", media_type, n
        )
    except Exception:
        logger.error(
            "Debounced unified-group snapshot rebuild failed for %s (browsing "
            "falls back to live aggregation for this type)", media_type,
            exc_info=True,
        )
    finally:
        if current is not None:
            _running_rebuild_tasks.pop(media_type, None)


def schedule_rebuild(
    media_type: str,
    *,
    session_factory: Callable[[], AsyncSession],
    delay: float | None = None,
) -> None:
    """Debounce a snapshot rebuild for ONE `media_type` (ADR 0005 D9).

    Trailing-edge: cancels any still-PENDING (not yet started) debounce task
    for this same type and schedules a fresh one `delay` seconds out. A
    rebuild already IN PROGRESS is never cancelled — a call arriving while
    one is running simply schedules another debounce window to start once
    the current one finishes settling into the pending slot (so it, in turn,
    can still be superseded by an even newer call before it starts).

    Types outside `GROUP_MEDIA_TYPES` are a no-op (nothing to rebuild).
    Failures are logged, never raised — the live aggregation path is always
    the fallback for browsing, so a rebuild failure must never propagate
    into (and fail) the caller's own write.
    """
    if media_type not in GROUP_MEDIA_TYPES:
        return
    if delay is None:
        delay = settings.SCRAPE_REBUILD_DEBOUNCE_SECONDS

    existing = _pending_rebuild_tasks.get(media_type)
    if existing is not None and not existing.done():
        existing.cancel()

    task = create_background_task(
        _debounced_rebuild(media_type, delay, session_factory),
        name=f"unified-rebuild-{media_type}",
    )
    _pending_rebuild_tasks[media_type] = task
    _pending_session_factories[media_type] = session_factory


async def flush_scheduled() -> None:
    """Run every still-pending debounced rebuild immediately, and await every
    currently-running one. Intended for tests (and end-of-batch callers, e.g.
    the future manual-scrape batch worker) that need the snapshot to reflect
    every `schedule_rebuild()` call made so far before proceeding.

    A pending task is still sleeping out its debounce window — cancelling it
    alone would make `_debounced_rebuild` take the `CancelledError` short-
    circuit and return WITHOUT rebuilding (see that function's docstring: a
    cancel there means "superseded", not "run now"). So instead we cancel the
    sleep AND run the rebuild ourselves, directly, using the session_factory
    captured at schedule time — exactly once per still-pending type."""
    pending_types = list(_pending_rebuild_tasks.keys())
    for media_type in pending_types:
        task = _pending_rebuild_tasks.pop(media_type, None)
        factory = _pending_session_factories.pop(media_type, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if factory is not None:
            try:
                n = await _rebuild_one(media_type, factory)
                logger.info(
                    "Unified-group snapshot (flushed): %s -> %d groups", media_type, n
                )
            except Exception:
                logger.error(
                    "Flushed unified-group snapshot rebuild failed for %s "
                    "(browsing falls back to live aggregation for this type)",
                    media_type, exc_info=True,
                )
    running = [t for t in _running_rebuild_tasks.values() if not t.done()]
    for t in running:
        try:
            await t
        except asyncio.CancelledError:
            pass
