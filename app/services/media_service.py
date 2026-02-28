import logging
from typing import Optional

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Media

logger = logging.getLogger("plexhub.media")


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
    ) -> tuple[list[Media], int]:
        """Get paginated media list with total count."""
        logger.debug(f"get_media_list: type={media_type}, limit={limit}, offset={offset}, "
                    f"sort={sort}, server_id={server_id}, parent={parent_rating_key}, "
                    f"include_filtered={include_filtered}")

        query = select(Media).where(Media.type == media_type)

        if server_id:
            query = query.where(Media.server_id == server_id)
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

    async def get_stats(self, db: AsyncSession) -> dict:
        """Get media statistics for health endpoint."""
        total_result = await db.execute(select(func.count()).select_from(Media))
        total = total_result.scalar() or 0

        enriched_result = await db.execute(
            select(func.count()).select_from(
                select(Media).where(Media.tmdb_id.isnot(None)).subquery()
            )
        )
        enriched = enriched_result.scalar() or 0

        broken_result = await db.execute(
            select(func.count()).select_from(
                select(Media).where(Media.is_broken == True).subquery()
            )
        )
        broken = broken_result.scalar() or 0

        return {
            "total_media": total,
            "enriched_media": enriched,
            "broken_streams": broken,
        }


media_service = MediaService()
