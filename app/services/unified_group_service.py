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

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Media, MediaGroup, MediaGroupMember
from app.services.aggregation_service import aggregate_movies
from app.utils.db_retry import run_with_retry
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


async def rebuild_all(session_factory) -> dict[str, int]:
    """Rebuild the snapshot for every grouped media_type.

    Each type is (re)built + committed in its OWN lock-retried transaction on a
    FRESH session — so a `database is locked` retry re-opens a clean session
    (no lingering ``PendingRollbackError``) and one type's failure never
    half-writes another's."""
    counts: dict[str, int] = {}
    for media_type in GROUP_MEDIA_TYPES:
        async def _attempt(_mt: str = media_type) -> int:
            async with session_factory() as db:
                n = await rebuild(db, _mt)
                await db.commit()
                return n

        # Isolate each type: a failure building one (e.g. an unexpected data
        # shape) must not prevent the other from being built — nor leave the
        # whole snapshot empty. The unified list falls back to live aggregation
        # for any type left unbuilt.
        try:
            counts[media_type] = await run_with_retry(
                _attempt, op=f"rebuild_media_group[{media_type}]"
            )
            logger.info(
                "Unified-group snapshot: %s -> %d groups", media_type, counts[media_type]
            )
        except Exception:
            logger.error(
                "Unified-group snapshot rebuild failed for %s (browsing falls back "
                "to live aggregation for this type)", media_type, exc_info=True,
            )
    return counts
