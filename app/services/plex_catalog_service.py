"""Read-only access to the deduplicated Plex shared-servers catalogue
(feature "Télécharger Plex", Tâche C4, docs/10-prd-media-download.md).

``plex_media_item`` already carries a precomputed ``unification_id`` (filled
at sync time, C2/C3) for `movie`/`show` rows — so, unlike the Xtream
`aggregation_service` (which has to converge split identities in memory,
CR-F05), read-side dedup here is a plain SQL ``GROUP BY unification_id``:
O(page) for the browse list, no whole-catalog load.

Pure reads: no FastAPI/HTTPException here (that belongs to the router layer,
not shipped by this ticket) and no writes (the sync worker owns writes).
Every function takes an already-open ``AsyncSession`` (``get_db``/
``async_session_factory``), matching ``media_service``'s convention.

Never returns ``PlexServer.access_token``/``base_uri`` — see
``models.database.PlexServer`` docstring (per-server secret, Fernet-encrypted
at rest, must never leave this process in an API response, HTML, or log
line).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import PlexMediaItem, PlexServer, PlexSyncStatus
from app.utils.server_id import build_plex_server_id, parse_plex_server_id


# --- Dataclasses (template-friendly; mappable to the Pydantic schemas in
#     app/models/schemas.py for the C7 JSON mirror) --------------------------


@dataclass
class PlexSource:
    server_id: str
    rating_key: str
    server_name: str
    owner_title: str | None
    resolution: str            # "1080p" (=f"{height}p"), "" if height unknown
    height: int | None
    size_bytes: int | None     # PlexMediaItem.part_size
    video_codec: str | None
    audio_codec: str | None
    container: str | None


@dataclass
class PlexUnifiedItem:
    unification_id: str
    type: str                  # 'movie' | 'show'
    title: str
    year: int | None
    source_count: int
    sources: list[PlexSource] = field(default_factory=list)
    # [] from list_unified (only source_count is cheap to compute there),
    # populated from get_group.


@dataclass
class PlexSeasonSources:
    season: int
    sources: list[dict] = field(default_factory=list)
    # per source: {server_id, show_rating_key, server_name, episode_count,
    #              resolution, size_bytes (total for that season)}


@dataclass
class PlexEpisodeSources:
    season: int
    episode: int
    title: str
    sources: list[dict] = field(default_factory=list)
    # per source: {server_id, episode_rating_key, server_name, resolution,
    #              height, size_bytes}


# --- Helpers -----------------------------------------------------------------


def _resolution_label(height: int | None) -> str:
    """``f"{height}p"`` for a known height, ``""`` otherwise."""
    return f"{height}p" if height else ""


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally.

    Same escaping convention as ``media_service.get_unified_list``/
    ``get_media_list`` (backslash-escaped ``%``/``_``, paired with
    ``.ilike(..., escape="\\\\")``).
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _load_servers_by_client_id(db: AsyncSession) -> dict[str, PlexServer]:
    """All known ``plex_server`` rows keyed by their raw ``client_identifier``
    (NOT the ``plex_<cid>`` server_id form used on ``plex_media_item``).

    The server table is small (one row per discovered PMS, owned or shared)
    — loading it whole is cheap and avoids a fragile SQL join against a
    prefixed string column.
    """
    rows = (await db.execute(select(PlexServer))).scalars().all()
    return {s.client_identifier: s for s in rows}


def _resolve_server(servers: dict[str, PlexServer], server_id: str) -> PlexServer | None:
    cid = parse_plex_server_id(server_id)
    if cid is None:
        return None
    return servers.get(cid)


def _server_display(servers: dict[str, PlexServer], server_id: str) -> tuple[str, str | None]:
    """Return (server_name, owner_title) for *server_id*, falling back to the
    raw server_id if the server row is missing (defensive — should not
    happen for a synced item, but never let a dangling FK 500 a read)."""
    server = _resolve_server(servers, server_id)
    if server is None:
        return server_id, None
    return server.name, server.owner_title


def _row_to_source(row: PlexMediaItem, servers: dict[str, PlexServer]) -> PlexSource:
    server_name, owner_title = _server_display(servers, row.server_id)
    return PlexSource(
        server_id=row.server_id,
        rating_key=row.rating_key,
        server_name=server_name,
        owner_title=owner_title,
        resolution=_resolution_label(row.height),
        height=row.height,
        size_bytes=row.part_size,
        video_codec=row.video_codec,
        audio_codec=row.audio_codec,
        container=row.container,
    )


def _sort_key_representative(row: PlexMediaItem) -> tuple[int, str]:
    """Deterministic "most recently added" tie-break used to pick a group's
    representative title/year, both in ``list_unified`` and ``get_group``:
    highest ``added_at`` first, ``rating_key`` ascending as a stable
    tie-break when ``added_at`` is equal (or NULL on both)."""
    return (row.added_at or 0, row.rating_key)


def _sort_key_source_quality(source: PlexSource) -> tuple[int, int]:
    """Sort sources by resolution (height) descending, then size descending.
    Unknown height/size sorts last."""
    return (source.height if source.height is not None else -1,
            source.size_bytes if source.size_bytes is not None else -1)


# --- Public reads --------------------------------------------------------


async def list_unified(
    db: AsyncSession,
    media_type: str,
    search: str | None,
    limit: int,
    offset: int,
) -> tuple[list[PlexUnifiedItem], int]:
    """Paginated, deduplicated browse list for *media_type* ('movie'|'show').

    Dedup = SQL ``GROUP BY unification_id`` over rows of that type (rows with
    a NULL/empty ``unification_id`` — not yet resolved by sync — are
    excluded, same convention as the Xtream catalogue). ``source_count`` =
    ``COUNT(*)`` per group: a `plex_media_item` row's primary key is
    ``(server_id, rating_key)``, so one row is exactly one source/version and
    plain row-counting is correct.

    Sort = recency, i.e. ``MAX(added_at)`` per group descending (ties broken
    by ``unification_id`` ascending for a stable page boundary).

    Each group's representative ``title``/``year`` are those of its member
    row with the most recent ``added_at`` (see ``_sort_key_representative``)
    — picking a single real row's fields rather than an arbitrary SQL
    aggregate (``MIN(title)`` could mix a truncated/mistyped variant with
    another row's year). ``sources`` is left ``[]`` here (deliberately not
    hydrated for a list endpoint) — use ``get_group`` for a single group's
    per-source details.

    O(page): the group listing + count are pure aggregate queries over the
    filtered rows; the representative-row lookup only loads the member rows
    of THIS PAGE's groups (bounded IN-join), never the whole catalogue.
    """
    base_filters = [
        PlexMediaItem.type == media_type,
        PlexMediaItem.unification_id.isnot(None),
        PlexMediaItem.unification_id != "",
    ]
    if search:
        base_filters.append(
            PlexMediaItem.title.ilike(f"%{_escape_like(search)}%", escape="\\")
        )

    max_added = func.max(PlexMediaItem.added_at)
    group_query = (
        select(
            PlexMediaItem.unification_id.label("unification_id"),
            func.count().label("source_count"),
            max_added.label("max_added"),
        )
        .where(*base_filters)
        .group_by(PlexMediaItem.unification_id)
    )

    total = (await db.execute(
        select(func.count()).select_from(group_query.subquery())
    )).scalar() or 0

    page = (await db.execute(
        group_query
        .order_by(max_added.desc(), PlexMediaItem.unification_id.asc())
        .limit(limit).offset(offset)
    )).all()

    if not page:
        return [], total

    group_ids = [row.unification_id for row in page]
    source_counts = {row.unification_id: row.source_count for row in page}
    order = {uid: i for i, uid in enumerate(group_ids)}

    member_rows = (await db.execute(
        select(PlexMediaItem).where(
            *base_filters,
            PlexMediaItem.unification_id.in_(group_ids),
        )
    )).scalars().all()

    representatives: dict[str, PlexMediaItem] = {}
    for row in member_rows:
        current = representatives.get(row.unification_id)
        if current is None or _sort_key_representative(row) > _sort_key_representative(current):
            representatives[row.unification_id] = row

    items = [
        PlexUnifiedItem(
            unification_id=uid,
            type=media_type,
            title=representatives[uid].title if uid in representatives else "",
            year=representatives[uid].year if uid in representatives else None,
            source_count=source_counts.get(uid, 0),
            sources=[],
        )
        for uid in group_ids
        if uid in representatives  # defensive: skip a group whose members vanished mid-query
    ]
    items.sort(key=lambda it: order[it.unification_id])
    return items, total


async def get_group(
    db: AsyncSession,
    media_type: str,
    unification_id: str,
) -> PlexUnifiedItem | None:
    """Full detail for one unified group: every ``plex_media_item`` row of
    ``(type=media_type, unification_id)`` as a ``PlexSource``.

    For a movie, each row IS a distinct playable version (resolution/size/
    codecs populated). For a show, each row is one server's copy of the
    series container — a show row carries no per-file media fields, so its
    ``PlexSource.height``/``size_bytes``/codecs come out ``None`` by
    construction (no special-casing needed: they're simply not populated on
    a 'show' row).

    Sources are sorted by resolution (height) descending, then size
    descending — unknown values sort last. Representative ``title``/``year``
    use the same "most recently added member" rule as ``list_unified``.

    Returns ``None`` if no row matches (unknown/foreign unification_id).
    """
    rows = (await db.execute(
        select(PlexMediaItem).where(
            PlexMediaItem.type == media_type,
            PlexMediaItem.unification_id == unification_id,
        )
    )).scalars().all()
    if not rows:
        return None

    servers = await _load_servers_by_client_id(db)
    sources = [_row_to_source(row, servers) for row in rows]
    sources.sort(key=_sort_key_source_quality, reverse=True)

    representative = max(rows, key=_sort_key_representative)

    return PlexUnifiedItem(
        unification_id=unification_id,
        type=media_type,
        title=representative.title,
        year=representative.year,
        source_count=len(rows),
        sources=sources,
    )


async def list_seasons_with_sources(
    db: AsyncSession,
    unification_id: str,
) -> list[PlexSeasonSources]:
    """Per-season source availability for a unified show.

    For every 'show' row sharing *unification_id* (one per server carrying
    that series), aggregate its 'episode' children
    (``grandparent_rating_key`` = that show's own ``rating_key``, scoped to
    the SAME server — episodes are server-local) grouped by ``parent_index``
    (season number). Each (server, season) bucket becomes one source entry:
    ``episode_count`` = number of episodes that server has for that season,
    ``resolution`` = the best (max) episode height in the bucket, formatted
    ``f"{height}p"`` (or ``""`` if unknown), ``size_bytes`` = the SUM of
    ``part_size`` across the bucket's episodes.

    Returns seasons sorted ascending; within a season, sources are in show
    (server) discovery order.
    """
    show_rows = (await db.execute(
        select(PlexMediaItem.server_id, PlexMediaItem.rating_key).where(
            PlexMediaItem.type == "show",
            PlexMediaItem.unification_id == unification_id,
        )
    )).all()
    if not show_rows:
        return []

    servers = await _load_servers_by_client_id(db)
    by_season: dict[int, list[dict]] = {}

    for server_id, show_rating_key in show_rows:
        server_name, _owner = _server_display(servers, server_id)
        agg = (await db.execute(
            select(
                PlexMediaItem.parent_index.label("season"),
                func.count().label("episode_count"),
                func.max(PlexMediaItem.height).label("max_height"),
                func.sum(PlexMediaItem.part_size).label("total_size"),
            )
            .where(
                PlexMediaItem.type == "episode",
                PlexMediaItem.server_id == server_id,
                PlexMediaItem.grandparent_rating_key == show_rating_key,
            )
            .group_by(PlexMediaItem.parent_index)
        )).all()

        for row in agg:
            if row.season is None:
                continue  # episode with no season assigned — nothing to bucket it under
            by_season.setdefault(row.season, []).append({
                "server_id": server_id,
                "show_rating_key": show_rating_key,
                "server_name": server_name,
                "episode_count": row.episode_count,
                "resolution": _resolution_label(row.max_height),
                "size_bytes": row.total_size,
            })

    return [
        PlexSeasonSources(season=season, sources=sources)
        for season, sources in sorted(by_season.items(), key=lambda kv: kv[0])
    ]


async def list_episodes_with_sources(
    db: AsyncSession,
    unification_id: str,
    season: int,
) -> list[PlexEpisodeSources]:
    """Per-episode source availability for one season of a unified show.

    For every 'show' row sharing *unification_id*, loads that server's
    'episode' children for *season* (``parent_index == season``) and groups
    them by episode number (``PlexMediaItem.index`` — the SQL column is
    ``"index"``, a reserved word, hence the ORM attribute name). Each episode
    slot collects one source per server that has it, carrying THAT server's
    own episode ``rating_key`` (``episode_rating_key`` — deliberately NOT the
    show's rating_key, and generally different across servers for the "same"
    episode).

    ``title`` = the longest (most descriptive) non-empty episode title seen
    across sources for that slot — a reasonable proxy for "most complete"
    when servers title episodes slightly differently.

    Returns episodes sorted ascending by episode number.
    """
    show_rows = (await db.execute(
        select(PlexMediaItem.server_id, PlexMediaItem.rating_key).where(
            PlexMediaItem.type == "show",
            PlexMediaItem.unification_id == unification_id,
        )
    )).all()
    if not show_rows:
        return []

    servers = await _load_servers_by_client_id(db)
    by_episode: dict[int, list[dict]] = {}
    titles: dict[int, str] = {}

    for server_id, show_rating_key in show_rows:
        server_name, _owner = _server_display(servers, server_id)
        episodes = (await db.execute(
            select(PlexMediaItem).where(
                PlexMediaItem.type == "episode",
                PlexMediaItem.server_id == server_id,
                PlexMediaItem.grandparent_rating_key == show_rating_key,
                PlexMediaItem.parent_index == season,
            )
        )).scalars().all()

        for ep in episodes:
            if ep.index is None:
                continue  # episode with no episode number — nothing to slot it under
            by_episode.setdefault(ep.index, []).append({
                "server_id": server_id,
                "episode_rating_key": ep.rating_key,
                "server_name": server_name,
                "resolution": _resolution_label(ep.height),
                "height": ep.height,
                "size_bytes": ep.part_size,
            })
            if ep.title and len(ep.title) > len(titles.get(ep.index, "")):
                titles[ep.index] = ep.title

    return [
        PlexEpisodeSources(
            season=season,
            episode=episode,
            title=titles.get(episode, ""),
            sources=sources,
        )
        for episode, sources in sorted(by_episode.items(), key=lambda kv: kv[0])
    ]


async def get_sync_status(db: AsyncSession) -> dict:
    """Read the singleton `plex_sync_status` row (id=1) for the admin UI's
    status panel (ticket C6).

    Defaults to `{"state": "idle", ...}` when the row doesn't exist yet (a
    fresh DB, before `plex_sync_service` has ever run — it upserts the row
    lazily on first claim/reap, not at migration time). Never exposes a
    secret: this table carries only `state`/`started_at`/`finished_at`/a
    bounded `error` string (`plex_sync_service._safe_error`), never a
    token/URL — see `PlexSyncStatus`'s docstring.
    """
    row = await db.get(PlexSyncStatus, 1)
    if row is None:
        return {"state": "idle", "started_at": None, "finished_at": None, "error": None}
    return {
        "state": row.state,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "error": row.error,
    }


async def list_servers(db: AsyncSession) -> list[dict]:
    """All known Plex servers, safe for API/HTML exposure.

    NEVER includes ``access_token``/``base_uri`` (per-server secret + its
    connection URI — ``PlexServer`` docstring, house-law piège on secrets).
    """
    rows = (await db.execute(select(PlexServer))).scalars().all()
    return [
        {
            "server_id": build_plex_server_id(s.client_identifier),
            "client_identifier": s.client_identifier,
            "name": s.name,
            "owner_title": s.owner_title,
            "owned": s.owned,
            "is_reachable": s.is_reachable,
            "last_synced_at": s.last_synced_at,
            "last_sync_error": s.last_sync_error,
        }
        for s in rows
    ]
