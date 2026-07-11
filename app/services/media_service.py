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
from app.utils.ttl_cache import TTLCache

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


# ─── CR-P01 residual mitigation (see docs/audit/cleanroom-2026-07-11/50-perf.md) ──
#
# get_unified_list still SELECTs + hydrates every category-allowed row and
# aggregates on EVERY call, with limit/offset applied only after the full
# load — O(catalog) DB read + memory per request regardless of page. A true
# fix is SQL-side windowed grouping (denormalized group table refreshed at
# sync/enrichment time) — out of scope for this contained fix.
#
# Mitigation: cache the expensive SORTED GROUPS LIST (never the sliced page)
# for a short TTL, keyed by:
#   - the filter tuple (media_type, search, genre, year, include_broken) —
#     NOT offset/limit, so every page of the same filter reuses one entry;
#   - a cheap freshness "fingerprint" (COUNT + MAX(updated_at) computed with
#     the SAME filters, narrow/index-backed — no SELECT * subquery, cf.
#     CR-P03) so the cache busts as soon as the filtered set gains/loses rows
#     (COUNT moves) or any row bumps updated_at.
#     CAVEAT: some in-place writes do NOT bump updated_at (enrichment
#     `update_values`, `is_broken` flips — column has default=0, no onupdate),
#     so a freshly-broken/freshly-enriched row can be reflected up to one TTL
#     window late. Acceptable staleness for a browse endpoint; not a
#     correctness guarantee of instant invalidation.
#   - the bound Engine's identity, which is a no-op in production (one
#     long-lived Engine for the process) but makes the cache structurally
#     unable to leak results across independent databases (e.g. per-test
#     isolated in-memory SQLite engines).
# Bounded LRU (size-capped) so memory stays predictable. Each entry can pin an
# O(catalog) MovieGroup+Media snapshot, so the cap is deliberately small (a few
# common filter combos) to stay well under the 2 GB container limit even when
# fingerprint churn during sync produces several live snapshots.
_UNIFIED_GROUPS_CACHE_TTL_SECONDS = 45.0
_UNIFIED_GROUPS_CACHE_MAX_SIZE = 12

_unified_groups_cache: TTLCache[tuple, list[MovieGroup]] = TTLCache(
    max_size=_UNIFIED_GROUPS_CACHE_MAX_SIZE,
    ttl_seconds=_UNIFIED_GROUPS_CACHE_TTL_SECONDS,
)


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

        # Count total.
        # CR-P03: count with a narrow `func.count()` over the base table using
        # the SAME filters, instead of wrapping a `SELECT *` subquery — avoids
        # materializing every matched row's ~60 columns just to count them.
        count_query = select(func.count()).select_from(Media)
        if query.whereclause is not None:
            count_query = count_query.where(query.whereclause)
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
        generator uses), then groups are sorted by recency and paginated.

        CR-P01 (P0, residual — see module-level comment above
        ``_unified_groups_cache``): the full load + CPU-bound aggregation
        (offloaded via asyncio.to_thread, fixing the original event-loop
        stall) still happens on every call in the worst case. Mitigated with
        a short-TTL cache of the SORTED GROUPS list, keyed by the filter
        tuple + a cheap freshness fingerprint — repeated pages and concurrent
        callers for the same filters reuse one load+aggregate; only
        offset/limit slicing (cheap) happens per request on a cache hit.
        True SQL-side windowed grouping remains a future optimization.
        """
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

        # Cheap freshness fingerprint over the SAME filters — narrow COUNT +
        # MAX(updated_at), no SELECT * subquery (cf. CR-P03) — so a cache hit
        # still costs one small indexed-ish aggregate query, and the cache
        # busts when the filtered set gains/loses rows or any row bumps
        # updated_at. (In-place writes that don't touch updated_at — enrichment
        # / is_broken flips — surface within one TTL window; see module note.)
        fingerprint_query = select(func.count(), func.max(Media.updated_at)).select_from(Media)
        if query.whereclause is not None:
            fingerprint_query = fingerprint_query.where(query.whereclause)
        fp_count, fp_max_updated = (await db.execute(fingerprint_query)).one()

        # Engine identity scopes the cache to one running app/DB — a no-op in
        # production (single long-lived Engine) but makes the cache
        # structurally unable to leak a result across independent databases
        # (e.g. isolated per-test in-memory SQLite engines) even if their
        # filtered row sets coincidentally fingerprint the same.
        cache_key = (
            id(db.get_bind()), media_type, search, genre, year, include_broken,
            fp_count or 0, fp_max_updated or 0,
        )

        groups = _unified_groups_cache.get(cache_key, None)
        if groups is None:
            rows = list((await db.execute(query)).scalars().all())
            # CR-P01 (P0): the grouping + sort below is CPU-bound Python
            # running over every category-allowed row — offloaded via
            # asyncio.to_thread so a large catalog no longer stalls the event
            # loop (and every other in-flight request) for the duration of
            # the aggregation.
            groups = await asyncio.to_thread(_aggregate_and_sort_movies, rows)
            _unified_groups_cache.set(cache_key, groups)

        total = len(groups)
        # `groups` is the cached list itself — slicing returns a NEW list and
        # never mutates it, so the cached snapshot stays immutable.
        return groups[offset:offset + limit], total

    async def get_unified_group(
        self,
        db: AsyncSession,
        media_type: str,
        unification_id: str,
    ) -> Optional[MovieGroup]:
        """Return the MovieGroup the list endpoint would produce for *unification_id*.

        CR-F05: an exact-`unification_id` match alone under-reports for a
        "split identity" title — `calculate_unification_id`'s imdb>tmdb>title
        priority means the SAME film can key `imdb://…` on one account's row
        and `tmdb://…` (or an unresolved `title_…`) on another's, and it's
        exactly ``aggregation_service._converge`` (Pass A: shared imdb/tmdb id;
        Pass B: same canonical title+year) that folds those rows into ONE
        group for the list endpoint. Filtering by ONE exact `unification_id`
        (the old behaviour) silently drops the twin(s).

        Fix: load the exact-match "seed" rows, ALSO load the bounded pool of
        candidate twins that `_converge` could fold them with (shared
        imdb_id/tmdb_id, or same year — see ``_load_convergence_candidates``),
        run the seeds+candidates through the SAME ``aggregate_movies``
        (hence `_converge`) pass the list endpoint uses, and return whichever
        resulting group still contains a seed row. This never scans the whole
        catalog (unlike ``get_unified_list``) — only rows plausibly linked to
        the requested title.
        """
        seed_rows = list((await db.execute(
            select(Media).where(
                Media.type == media_type,
                Media.unification_id == unification_id,
                Media.is_in_allowed_categories == True,  # noqa: E712
            )
        )).scalars().all())
        if not seed_rows:
            return None

        seed_pk = {(r.server_id, r.rating_key) for r in seed_rows}
        candidates = await self._load_convergence_candidates(db, media_type, seed_rows)
        all_rows = list(seed_rows) + [
            r for r in candidates if (r.server_id, r.rating_key) not in seed_pk
        ]

        groups = aggregate_movies(all_rows)
        for g in groups:
            if any((m.server_id, m.rating_key) in seed_pk for m in g.members):
                return g
        return groups[0]  # defensive: seed rows always land in some group

    async def _load_convergence_candidates(
        self,
        db: AsyncSession,
        media_type: str,
        seed_rows: list[Media],
    ) -> list[Media]:
        """CR-F05 helper: bounded candidate pool for ``get_unified_group``.

        Two narrow queries (both scoped to *media_type* + allowed categories,
        never the whole catalog):
          (a) rows sharing a physical imdb_id/tmdb_id with a seed row —
              repairs the imdb-vs-tmdb key split (Pass A /
              ``_merge_by_shared_ids``).
          (b) rows for the same year(s) as the seed rows — a coarse,
              cheap pre-filter; the EXACT title-normalization check that
              decides absorption (Pass B / ``_absorb_title_groups``) still
              runs unchanged inside ``aggregate_movies``/`_converge`, so
              widening the candidate pool here can never mis-absorb an
              unrelated same-year title — it just becomes its own separate
              group that ``get_unified_group`` ignores.
        Deduplicated by (server_id, rating_key).
        """
        imdb_ids = {
            str(r.imdb_id).strip() for r in seed_rows
            if r.imdb_id and str(r.imdb_id).strip()
        }
        tmdb_ids = {
            str(r.tmdb_id).strip() for r in seed_rows
            if r.tmdb_id is not None and str(r.tmdb_id).strip().isdigit()
            and str(r.tmdb_id).strip() != "0"
        }
        years = {r.year for r in seed_rows if r.year is not None}

        found: dict[tuple[str, str], Media] = {}

        if imdb_ids or tmdb_ids:
            id_filters = []
            if imdb_ids:
                id_filters.append(Media.imdb_id.in_(imdb_ids))
            if tmdb_ids:
                id_filters.append(Media.tmdb_id.in_(tmdb_ids))
            rows = (await db.execute(
                select(Media).where(
                    Media.type == media_type,
                    Media.is_in_allowed_categories == True,  # noqa: E712
                    or_(*id_filters),
                )
            )).scalars().all()
            for row in rows:
                found[(row.server_id, row.rating_key)] = row

        if years:
            rows = (await db.execute(
                select(Media).where(
                    Media.type == media_type,
                    Media.is_in_allowed_categories == True,  # noqa: E712
                    Media.year.in_(years),
                )
            )).scalars().all()
            for row in rows:
                found.setdefault((row.server_id, row.rating_key), row)

        return list(found.values())

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
