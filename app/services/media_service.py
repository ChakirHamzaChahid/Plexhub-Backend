import asyncio
import logging
from typing import Optional

from sqlalchemy import select, func, delete, update, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Media, EnrichmentQueue, XtreamAccount
from app.services.aggregation_service import (
    MovieGroup, SeriesGroup, aggregate_movies, aggregate_series,
)
from app.utils.server_id import build_server_id
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.media")


def _aggregate_and_sort_movies(rows: list[Media]) -> list[MovieGroup]:
    """CPU-bound grouping + sort, run off the event loop (see CR-P01).

    Pure function operating only on scalar columns of already-loaded `Media`
    rows (no relationships/lazy attributes on this model, no session/DB access)
    — safe to execute in a worker thread via ``asyncio.to_thread``.
    """
    groups = aggregate_movies(rows)  # generic: groups by key + picks best row
    groups.sort(key=lambda g: (g.best.added_at or 0), reverse=True)
    return groups


class MediaService:

    async def get_media_list(
        self,
        db: AsyncSession,
        media_type: str,
        limit: int = 500,
        offset: int = 0,
        sort: str = "added_desc",
        server_id: Optional[str] = None,
        parent_rating_key: Optional[str] = None,
        include_filtered: bool = False,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        year: Optional[int] = None,
        missing_imdb: bool = False,
        missing_tmdb: bool = False,
    ) -> tuple[list[Media], int]:
        """Get paginated media list with total count.

        When both missing_imdb and missing_tmdb are True, the filter is OR (rows
        with imdb_id missing OR tmdb_id missing). When only one is True, only
        that condition applies.
        """
        logger.debug(f"get_media_list: type={media_type}, limit={limit}, offset={offset}, "
                    f"sort={sort}, server_id={server_id}, parent={parent_rating_key}, "
                    f"include_filtered={include_filtered}, search={search}, "
                    f"missing_imdb={missing_imdb}, missing_tmdb={missing_tmdb}")

        query = select(Media).where(Media.type == media_type)

        if server_id:
            query = query.where(Media.server_id == server_id)
        if search:
            safe_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(Media.title.ilike(f"%{safe_search}%", escape="\\"))
        if genre:
            safe_genre = genre.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(Media.genres.ilike(f"%{safe_genre}%", escape="\\"))
        if year:
            query = query.where(Media.year == year)
        imdb_missing = or_(Media.imdb_id.is_(None), Media.imdb_id == "")
        tmdb_missing = or_(Media.tmdb_id.is_(None), Media.tmdb_id == "")
        if missing_imdb and missing_tmdb:
            query = query.where(or_(imdb_missing, tmdb_missing))
        elif missing_imdb:
            query = query.where(imdb_missing)
        elif missing_tmdb:
            query = query.where(tmdb_missing)
        if parent_rating_key:
            # Auto-detect series queries: if parent_rating_key starts with "series_",
            # filter by grandparent_rating_key (episodes belong to series via grandparent)
            if parent_rating_key.startswith("series_"):
                query = query.where(Media.grandparent_rating_key == parent_rating_key)
            else:
                query = query.where(Media.parent_rating_key == parent_rating_key)
        if not include_filtered:
            query = query.where(Media.is_in_allowed_categories == True)

        # Count total
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        # Apply sorting
        if sort == "added_desc":
            query = query.order_by(Media.added_at.desc())
        elif sort == "added_asc":
            query = query.order_by(Media.added_at.asc())
        elif sort == "title_asc":
            query = query.order_by(Media.title_sortable.asc())
        elif sort == "title_desc":
            query = query.order_by(Media.title_sortable.desc())
        elif sort == "rating_desc":
            query = query.order_by(Media.display_rating.desc())
        elif sort == "year_desc":
            query = query.order_by(Media.year.desc().nulls_last())
        else:
            query = query.order_by(Media.added_at.desc())

        # Apply pagination
        query = query.offset(offset).limit(limit)

        result = await db.execute(query)
        items = list(result.scalars().all())

        logger.debug(f"get_media_list result: found {len(items)} items (total={total})")
        return items, total

    async def account_labels(self, db: AsyncSession) -> dict[str, str]:
        """Map server_id -> account label (falls back to the id)."""
        rows = await db.execute(select(XtreamAccount.id, XtreamAccount.label))
        return {build_server_id(i): (label or i) for i, label in rows}

    async def get_unified_list(
        self,
        db: AsyncSession,
        media_type: str,
        *,
        limit: int = 500,
        offset: int = 0,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        year: Optional[int] = None,
        include_broken: bool = True,
    ) -> tuple[list[MovieGroup], int]:
        """Aggregate movie/show rows across ALL accounts by unification_id.

        Returns (page_of_groups, total_groups). Grouping is in-memory over the
        filtered, category-allowed rows (the same logic the Plex/Jellyfin
        generator uses), then groups are sorted by recency and paginated."""
        query = select(Media).where(
            Media.type == media_type,
            Media.is_in_allowed_categories == True,  # noqa: E712
        )
        if search:
            safe = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(Media.title.ilike(f"%{safe}%", escape="\\"))
        if genre:
            safe = genre.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(Media.genres.ilike(f"%{safe}%", escape="\\"))
        if year:
            query = query.where(Media.year == year)
        if not include_broken:
            query = query.where(Media.is_broken == False)  # noqa: E712

        rows = list((await db.execute(query)).scalars().all())
        # CR-P01 (P0): the grouping + sort below is CPU-bound Python running
        # over every category-allowed row — offloaded via asyncio.to_thread so
        # a large catalog no longer stalls the event loop (and every other
        # in-flight request) for the duration of the aggregation.
        # Residual follow-up (NOT fixed here, tracked as CR-P01 follow-up): the
        # SELECT above still loads/hydrates the ENTIRE category-allowed catalog
        # every call, with limit/offset applied only after grouping — still
        # O(catalog) memory + DB read per request. Needs a SQL-side windowed
        # page or a cached/denormalized grouping, not just off-loop execution.
        groups = await asyncio.to_thread(_aggregate_and_sort_movies, rows)
        total = len(groups)
        return groups[offset:offset + limit], total

    async def get_unified_group(
        self,
        db: AsyncSession,
        media_type: str,
        unification_id: str,
    ) -> Optional[MovieGroup]:
        """Return the single MovieGroup whose key matches *unification_id* exactly.

        Queries all category-allowed rows of *media_type* that carry this
        unification_id, runs them through the same ``aggregate_movies`` pass used
        by the list endpoint (so convergence / _converge logic fires identically),
        and returns the first (and only expected) group — or None when no row is
        found.

        This mirrors the pattern of ``get_unified_episodes`` for an exact-id
        look-up, keeping the response shape (versions[], version_count, best-row
        metadata) byte-identical to the paginated list.
        """
        rows = list((await db.execute(
            select(Media).where(
                Media.type == media_type,
                Media.unification_id == unification_id,
                Media.is_in_allowed_categories == True,  # noqa: E712
            )
        )).scalars().all())
        if not rows:
            return None
        groups = aggregate_movies(rows)
        # All rows share the same unification_id → exactly one group after
        # convergence (or the strongest representative when ids diverge slightly).
        return groups[0]

    async def get_unified_episodes(
        self, db: AsyncSession, unification_id: str,
    ) -> Optional[tuple[list[Media], SeriesGroup]]:
        """Aggregate episodes of a unified show (all member accounts) into
        per-(season, episode) slots. Returns (member_shows, series_group) or
        None if no show carries that unification_id."""
        shows = list((await db.execute(
            select(Media).where(
                Media.type == "show",
                Media.unification_id == unification_id,
                Media.is_in_allowed_categories == True,  # noqa: E712
            )
        )).scalars().all())
        if not shows:
            return None

        server_ids = {s.server_id for s in shows}
        show_keys = {s.rating_key for s in shows}
        episodes = list((await db.execute(
            select(Media).where(
                Media.type == "episode",
                Media.server_id.in_(server_ids),
                Media.grandparent_rating_key.in_(show_keys),
            )
        )).scalars().all())

        # Same CPU-bound-on-event-loop pattern as get_unified_list (CR-P01) —
        # offloaded identically. Scoped to one show's members/episodes here
        # (not the whole catalog), but the grouping itself is pure Python work
        # over already-loaded rows, so it's just as safe to run off-thread.
        groups = await asyncio.to_thread(aggregate_series, shows, episodes)
        # All member shows share unification_id => exactly one group.
        return shows, groups[0]

    async def get_media_by_key(
        self,
        db: AsyncSession,
        rating_key: str,
        server_id: str,
    ) -> Optional[Media]:
        """Get a single media item by its composite key."""
        result = await db.execute(
            select(Media).where(
                Media.rating_key == rating_key,
                Media.server_id == server_id,
            ).limit(1)
        )
        return result.scalars().first()

    async def count_movies_missing_external(
        self, db: AsyncSession,
    ) -> tuple[int, int, int]:
        """Return (total_movies, missing_imdb, missing_tmdb)."""
        base = select(func.count()).select_from(Media).where(Media.type == "movie")
        total = (await db.execute(base)).scalar() or 0
        missing_imdb = (await db.execute(
            base.where(or_(Media.imdb_id.is_(None), Media.imdb_id == ""))
        )).scalar() or 0
        missing_tmdb = (await db.execute(
            base.where(or_(Media.tmdb_id.is_(None), Media.tmdb_id == ""))
        )).scalar() or 0
        return total, missing_imdb, missing_tmdb

    async def update_external_ids(
        self,
        db: AsyncSession,
        rating_key: str,
        server_id: str,
        *,
        fields: dict,
    ) -> Optional[Media]:
        """Patch a media item with the given fields (imdb_id and/or tmdb_id).

        `fields` only contains keys the caller wants to update — empty dict is a
        no-op that returns the current row. Update applies to every (filter,
        sort_order) variant under (rating_key, server_id). Caller commits via
        Depends(get_db).
        """
        if not fields:
            return await self.get_media_by_key(db, rating_key, server_id)

        values = dict(fields)
        values["updated_at"] = now_ms()
        result = await db.execute(
            update(Media)
            .where(Media.rating_key == rating_key, Media.server_id == server_id)
            .values(**values)
        )
        if result.rowcount == 0:
            return None
        await db.flush()
        return await self.get_media_by_key(db, rating_key, server_id)

    async def enqueue_rescrape(
        self,
        db: AsyncSession,
        rating_key: str,
        server_id: str,
    ) -> bool:
        """Mark a movie for re-enrichment.

        If an EnrichmentQueue row already exists for (rating_key, server_id), reset it
        to pending. Otherwise insert a new pending entry. Returns False if the media
        item doesn't exist.
        """
        media = await self.get_media_by_key(db, rating_key, server_id)
        if not media:
            return False

        existing = await db.execute(
            select(EnrichmentQueue).where(
                EnrichmentQueue.rating_key == rating_key,
                EnrichmentQueue.server_id == server_id,
            )
        )
        row = existing.scalars().first()
        ts = now_ms()
        if row:
            row.status = "pending"
            row.attempts = 0
            row.last_error = None
            row.processed_at = None
            row.created_at = ts
            row.existing_imdb_id = media.imdb_id
            row.existing_tmdb_id = media.tmdb_id
            row.existing_summary = media.summary
        else:
            db.add(EnrichmentQueue(
                rating_key=rating_key,
                server_id=server_id,
                media_type=media.type,
                title=media.title,
                year=media.year,
                status="pending",
                attempts=0,
                created_at=ts,
                existing_imdb_id=media.imdb_id,
                existing_tmdb_id=media.tmdb_id,
                existing_summary=media.summary,
            ))
        await db.flush()
        return True

    async def get_stats(self, db: AsyncSession) -> dict:
        """Get media statistics for health endpoint."""
        total_result = await db.execute(select(func.count()).select_from(Media))
        total = total_result.scalar() or 0

        enriched_result = await db.execute(
            select(func.count()).select_from(Media).where(Media.tmdb_id.isnot(None))
        )
        enriched = enriched_result.scalar() or 0

        broken_result = await db.execute(
            select(func.count()).select_from(Media).where(Media.is_broken == True)
        )
        broken = broken_result.scalar() or 0

        return {
            "total_media": total,
            "enriched_media": enriched,
            "broken_streams": broken,
        }


media_service = MediaService()
