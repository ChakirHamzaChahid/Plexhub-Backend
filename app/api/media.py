import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.database import get_db
from app.models.database import Media
from app.models.schemas import (
    MediaResponse,
    MediaListResponse,
    MediaUpdate,
    MediaStatsResponse,
    MediaVersionResponse,
    TrailerResolveResponse,
    UnifiedMediaResponse,
    UnifiedMediaListResponse,
    UnifiedEpisodeResponse,
    UnifiedEpisodeListResponse,
    apply_adult_prefix,
)
from app.services import trailer_service
from app.services.aggregation_service import (
    build_versions, canonical_title_year,
)
from app.services.media_service import media_service, encode_media_cursor
from app.utils.metrics import media_episodes_missing_server_id_total

router = APIRouter(prefix="/media", tags=["media"])

logger = logging.getLogger("plexhub.api.media")

# AUDIT-P5-004 (S2.6): bound the logged User-Agent so a looping/misbehaving
# legacy client can't inflate log volume (CR-S09 note — client-supplied
# strings landing in logs must stay bounded).
_USER_AGENT_LOG_MAX_LEN = 200


def _single_pass_json(model: BaseModel) -> JSONResponse:
    """CR-P07: serialize the response model ONCE and hand FastAPI a ready
    ``Response`` so it skips its own re-validation + re-serialization pass.

    The list endpoints build up to ``limit`` (500 raw / 2000 unified) Pydantic
    models, then FastAPI used to re-validate + re-dump every one of them against
    ``response_model`` — a second full Pydantic pass over ~60 fields/row on the
    event loop. Returning a ``Response`` short-circuits that: FastAPI does not
    re-validate when the return value is already a ``Response``.

    ``response_model=`` is KEPT on every route purely for the OpenAPI schema
    (the documented contract is therefore unchanged). Output is byte-identical
    to FastAPI's default path — same ``JSONResponse`` class (no custom
    ``default_response_class`` on the app), same ``by_alias=True`` default, no
    ``exclude_*`` — see ADR 0001. Model fields are JSON-native scalars/lists
    (no ``datetime``), so ``mode="json"`` matches ``jsonable_encoder``."""
    return JSONResponse(content=model.model_dump(mode="json", by_alias=True))


_KEYSET_SORTS = ("added_desc", "added_asc")


def _page_meta(items, limit, offset, total, cursor, sort) -> tuple[bool, Optional[str]]:
    """Compute ``(has_more, next_cursor)`` for a raw list page (CR-P04).

    ``has_more`` ALWAYS keeps the exact ``(offset + limit) < total`` offset
    formula — unchanged for every existing caller regardless of cursor.

    ``next_cursor`` is emitted on a recency sort (added_desc/added_asc) whenever
    the page came back FULL (``len(items) == limit``), pointing at the last row
    — independent of whether an incoming ``cursor`` was supplied, so a keyset
    client can start paging with no cursor and simply follow ``next_cursor``
    until it is null (a full final page yields one extra empty request — the
    standard, correct cursor-pagination boundary). For non-recency sorts it
    stays null. It is purely additive: offset callers ignore it."""
    next_cursor = None
    if sort in _KEYSET_SORTS and items and len(items) == limit:
        next_cursor = encode_media_cursor(items[-1])
    return (offset + limit) < total, next_cursor


def _build_versions(
    members: list[Media], labels: dict[str, str],
) -> list[MediaVersionResponse]:
    """Turn group member rows into unique-labelled version entries.

    CR-A07: delegates the sort/label/dedup sequence to the shared
    `aggregation_service.build_versions` helper so the API and the on-disk
    library (`DatabaseSource._build_versions`) label versions identically and
    deterministically — see that helper's docstring for why the stable-identity
    sort must happen before labelling."""
    return [
        MediaVersionResponse(
            server_id=m.server_id, rating_key=m.rating_key,
            title=m.title, label=label, is_broken=m.is_broken,
        )
        for m, label in build_versions(members, lambda m: labels.get(m.server_id, m.server_id))
    ]


def _tmdb_str(value) -> str | None:
    return str(value) if value not in (None, "") else None


def _nfo_metadata(best: Media) -> dict:
    """NFO-imported metadata (tinyMediaManager) carried on the unified card.

    Shared by /movies/unified and /shows/unified so both surface the same
    extended fields from the group's best row."""
    return dict(
        original_title=best.original_title,
        tagline=best.tagline,
        premiered=best.premiered,
        status=best.status,
        studio=best.studio,
        country=best.country,
        tvdb_id=best.tvdb_id,
        wikidata_id=best.wikidata_id,
        imdb_rating=best.imdb_rating,
        imdb_votes=best.imdb_votes,
        tmdb_rating=best.tmdb_rating,
        tmdb_votes=best.tmdb_votes,
        cast_json=best.cast_json,
        youtube_trailer=best.youtube_trailer,
    )


@router.get("/movies", response_model=MediaListResponse)
async def list_movies(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("added_desc"),
    server_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    missing_imdb: bool = Query(False),
    missing_tmdb: bool = Query(False),
    cursor: Optional[str] = Query(
        None,
        description="Opaque keyset cursor (CR-P04). With sort=added_desc/added_asc "
                    "it seeks past this row and ignores offset; use the response's "
                    "nextCursor to page. Ignored for other sorts.",
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        items, total = await media_service.get_media_list(
            db, media_type="movie", limit=limit, offset=offset,
            sort=sort, server_id=server_id,
            search=search, genre=genre, year=year,
            missing_imdb=missing_imdb, missing_tmdb=missing_tmdb,
            cursor=cursor,
        )
    except ValueError as e:
        raise HTTPException(400, f"Invalid cursor: {e}")
    has_more, next_cursor = _page_meta(items, limit, offset, total, cursor, sort)
    return _single_pass_json(MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total, has_more=has_more, next_cursor=next_cursor,
    ))


@router.get("/movies/stats", response_model=MediaStatsResponse)
async def movies_stats(db: AsyncSession = Depends(get_db)):
    total, missing_imdb, missing_tmdb = await media_service.count_movies_missing_external(db)
    return MediaStatsResponse(
        total=total, missing_imdb=missing_imdb, missing_tmdb=missing_tmdb,
    )


@router.get("/shows", response_model=MediaListResponse)
async def list_shows(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("added_desc"),
    server_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    cursor: Optional[str] = Query(
        None,
        description="Opaque keyset cursor (CR-P04). With sort=added_desc/added_asc "
                    "it seeks past this row and ignores offset; use the response's "
                    "nextCursor to page. Ignored for other sorts.",
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        items, total = await media_service.get_media_list(
            db, media_type="show", limit=limit, offset=offset,
            sort=sort, server_id=server_id,
            search=search, genre=genre, year=year,
            cursor=cursor,
        )
    except ValueError as e:
        raise HTTPException(400, f"Invalid cursor: {e}")
    has_more, next_cursor = _page_meta(items, limit, offset, total, cursor, sort)
    return _single_pass_json(MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total, has_more=has_more, next_cursor=next_cursor,
    ))


@router.get("/episodes", response_model=MediaListResponse)
async def list_episodes(
    request: Request,
    parent_rating_key: str = Query(...),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    server_id: Optional[str] = Query(
        None,
        description="REQUIRED. A series parent_rating_key (e.g. 'series_7724') "
                    "is unique only per Xtream account and collides across "
                    "accounts, so (parent_rating_key, server_id) is the only key "
                    "that identifies a series. Omitting it is rejected with 400 "
                    "rather than silently mixing two homonymous series' episodes.",
    ),
    cursor: Optional[str] = Query(
        None,
        description="Opaque keyset cursor (CR-P04). Episodes always sort by "
                    "added_desc, so a cursor seeks past this row and ignores "
                    "offset; use the response's nextCursor to page.",
    ),
    db: AsyncSession = Depends(get_db),
):
    # server_id is mandatory here (kept as an Optional Query so a missing value
    # is a clean 400 with this message, not FastAPI's generic 422). Without it,
    # parent_rating_key matches rows across every account and returns two
    # different series' episodes mixed together (the MAO/Treadstone bug).
    if not server_id:
        # AUDIT-P5-004 (S2.6): the 400 itself is unchanged (same status, same
        # `detail`) — this is pure telemetry so a legacy `PlexHubTV` build
        # still hitting the pre-server_id contract becomes observable instead
        # of failing silently. Never log a key/credential; the User-Agent is
        # length-bounded (piège §9-10 / CR-S09).
        user_agent = (request.headers.get("user-agent") or "-")[:_USER_AGENT_LOG_MAX_LEN]
        logger.warning(
            "GET /api/media/episodes rejected 400 for missing server_id "
            "(parent_rating_key=%s, user_agent=%s) — possible legacy client "
            "still on the pre-server_id contract.",
            parent_rating_key, user_agent,
        )
        media_episodes_missing_server_id_total.inc()
        raise HTTPException(
            400,
            "server_id is required to list episodes: a series rating key is "
            "unique only per account and collides across accounts.",
        )
    try:
        items, total = await media_service.get_media_list(
            db, media_type="episode", limit=limit, offset=offset,
            server_id=server_id, parent_rating_key=parent_rating_key,
            cursor=cursor,
        )
    except ValueError as e:
        raise HTTPException(400, f"Invalid cursor: {e}")
    # list_episodes has no `sort` param — the service defaults to added_desc.
    has_more, next_cursor = _page_meta(items, limit, offset, total, cursor, "added_desc")
    return _single_pass_json(MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total, has_more=has_more, next_cursor=next_cursor,
    ))


@router.get("/movies/unified", response_model=UnifiedMediaListResponse)
async def list_movies_unified(
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    unification_id: Optional[str] = Query(None, description="When set, return only the group matching this unification_id (total=1 or 0)."),
    db: AsyncSession = Depends(get_db),
):
    """Movies deduped across ALL accounts: one entry per title + its versions.

    When *unification_id* is provided, only the matching group is returned
    (total=1 if found, 0 if not found) — the list/search/pagination path is
    **not** exercised (byte-identical to today when the param is absent).
    """
    labels = await media_service.account_labels(db)

    if unification_id is not None:
        g = await media_service.get_unified_group(db, "movie", unification_id)
        if g is None:
            return _single_pass_json(UnifiedMediaListResponse(items=[], total=0, has_more=False))
        best = g.best
        clean_title, clean_year = canonical_title_year(best)
        is_adult = bool(getattr(best, "is_adult", False))
        item = UnifiedMediaResponse(
            unification_id=g.key, type="movie",
            title=apply_adult_prefix(clean_title, is_adult), year=clean_year,
            summary=best.summary, genres=best.genres, content_rating=best.content_rating,
            thumb_url=best.resolved_thumb_url or best.thumb_url,
            art_url=best.resolved_art_url or best.art_url,
            imdb_id=best.imdb_id, tmdb_id=_tmdb_str(best.tmdb_id),
            rating=best.display_rating or best.scraped_rating, cast=best.cast,
            is_adult=is_adult,
            versions=_build_versions(g.members, labels),
            version_count=len(g.members),
            **_nfo_metadata(best),
        )
        return _single_pass_json(UnifiedMediaListResponse(items=[item], total=1, has_more=False))

    groups, total = await media_service.get_unified_list(
        db, media_type="movie", limit=limit, offset=offset,
        search=search, genre=genre, year=year,
    )
    items = []
    for g in groups:
        best = g.best
        clean_title, clean_year = canonical_title_year(best)
        is_adult = bool(getattr(best, "is_adult", False))
        items.append(UnifiedMediaResponse(
            unification_id=g.key, type="movie",
            title=apply_adult_prefix(clean_title, is_adult), year=clean_year,
            summary=best.summary, genres=best.genres, content_rating=best.content_rating,
            thumb_url=best.resolved_thumb_url or best.thumb_url,
            art_url=best.resolved_art_url or best.art_url,
            imdb_id=best.imdb_id, tmdb_id=_tmdb_str(best.tmdb_id),
            rating=best.display_rating or best.scraped_rating, cast=best.cast,
            is_adult=is_adult,
            versions=_build_versions(g.members, labels),
            version_count=len(g.members),
            **_nfo_metadata(best),
        ))
    return _single_pass_json(UnifiedMediaListResponse(
        items=items, total=total, has_more=(offset + limit) < total,
    ))


@router.get("/shows/unified", response_model=UnifiedMediaListResponse)
async def list_shows_unified(
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    unification_id: Optional[str] = Query(None, description="When set, return only the group matching this unification_id (total=1 or 0)."),
    db: AsyncSession = Depends(get_db),
):
    """Shows deduped across ALL accounts. Episodes via /shows/{id}/episodes.

    When *unification_id* is provided, only the matching group is returned
    (total=1 if found, 0 if not found) — the list/search/pagination path is
    **not** exercised (byte-identical to today when the param is absent).
    """
    labels = await media_service.account_labels(db)

    if unification_id is not None:
        g = await media_service.get_unified_group(db, "show", unification_id)
        if g is None:
            return _single_pass_json(UnifiedMediaListResponse(items=[], total=0, has_more=False))
        best = g.best
        clean_title, clean_year = canonical_title_year(best)
        item = UnifiedMediaResponse(
            unification_id=g.key, type="show", title=clean_title, year=clean_year,
            summary=best.summary, genres=best.genres, content_rating=best.content_rating,
            thumb_url=best.resolved_thumb_url or best.thumb_url,
            art_url=best.resolved_art_url or best.art_url,
            imdb_id=best.imdb_id, tmdb_id=_tmdb_str(best.tmdb_id),
            rating=best.display_rating or best.scraped_rating, cast=best.cast,
            versions=_build_versions(g.members, labels),
            version_count=len(g.members),
            **_nfo_metadata(best),
        )
        return _single_pass_json(UnifiedMediaListResponse(items=[item], total=1, has_more=False))

    groups, total = await media_service.get_unified_list(
        db, media_type="show", limit=limit, offset=offset,
        search=search, genre=genre, year=year,
    )
    items = []
    for g in groups:
        best = g.best
        clean_title, clean_year = canonical_title_year(best)
        items.append(UnifiedMediaResponse(
            unification_id=g.key, type="show", title=clean_title, year=clean_year,
            summary=best.summary, genres=best.genres, content_rating=best.content_rating,
            thumb_url=best.resolved_thumb_url or best.thumb_url,
            art_url=best.resolved_art_url or best.art_url,
            imdb_id=best.imdb_id, tmdb_id=_tmdb_str(best.tmdb_id),
            rating=best.display_rating or best.scraped_rating, cast=best.cast,
            versions=_build_versions(g.members, labels),
            version_count=len(g.members),
            **_nfo_metadata(best),
        ))
    return _single_pass_json(UnifiedMediaListResponse(
        items=items, total=total, has_more=(offset + limit) < total,
    ))


@router.get("/episodes/unified", response_model=UnifiedEpisodeListResponse)
async def list_episodes_unified(
    unification_id: str = Query(..., description="Show unificationId, e.g. tmdb://1396"),
    db: AsyncSession = Depends(get_db),
):
    """Episodes of a unified show, deduped across accounts into (season, episode)
    slots — each slot exposes every account/quality version it exists in."""
    result = await media_service.get_unified_episodes(db, unification_id)
    if result is None:
        raise HTTPException(404, "No show found for that unificationId")
    shows, group = result
    labels = await media_service.account_labels(db)

    slots = sorted(group.slots, key=lambda s: (s.season, s.episode))
    items = [
        UnifiedEpisodeResponse(
            season=slot.season, episode=slot.episode,
            title=slot.best.title, summary=slot.best.summary,
            thumb_url=slot.best.resolved_thumb_url or slot.best.thumb_url,
            duration=slot.best.duration,
            versions=_build_versions(slot.members, labels),
            version_count=len(slot.members),
        )
        for slot in slots
    ]
    return _single_pass_json(UnifiedEpisodeListResponse(
        unification_id=unification_id, series_title=canonical_title_year(group.best)[0],
        items=items, total=len(items),
    ))


# --- Trailer resolution (Lot A trailers) ------------------------------------
# NOTE: both routes MUST stay ABOVE `GET /{rating_key}` below — a future
# single-segment literal route would otherwise silently collide with that
# catch-all path parameter (these two are 2/3-segment paths so they don't
# today, but the ordering convention is kept defensively, matching
# `/episodes/unified` above).


@router.get("/trailer/resolve", response_model=TrailerResolveResponse)
async def resolve_trailer(
    rating_key: Optional[str] = Query(None),
    server_id: Optional[str] = Query(None),
    youtube_id: Optional[str] = Query(
        None,
        description="Bare YouTube video id — generic mode (no DB lookup), "
                     "for callers that already know the key.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a YouTube trailer key to a cached local mp4 (Lot A trailers).

    Two modes:
      - `youtube_id` supplied -> generic mode, used by callers that already
        know the key (e.g. Jellyfin `RemoteTrailers`, xtream-direct
        `getVodInfo`).
      - `rating_key` + `server_id` -> reads `media.youtube_trailer`, falling
        back to a live TMDB `/videos` lookup by `tmdb_id` when the column is
        empty (write-back on success).

    `status`: "ready" (the mp4 is cached, `url` points at the file endpoint)
    | "pending" (a download was just kicked off in the background — poll
    again) | "none" (no trailer resolvable for this media/key)."""
    if youtube_id is not None:
        result = await trailer_service.resolve_by_youtube_id(youtube_id)
    elif rating_key is not None and server_id is not None:
        result = await trailer_service.resolve_by_rating_key(db, rating_key, server_id)
    else:
        raise HTTPException(
            400, "Provide either youtube_id, or both rating_key and server_id",
        )
    return TrailerResolveResponse(status=result.status, url=result.url)


@router.get("/trailer/file/{cache_key}")
async def get_trailer_file(cache_key: str):
    """Serve a cached trailer mp4 — Range-aware (native Starlette
    `FileResponse`, honours `Range`/`If-Range`, answers 206/416 as needed).

    `cache_key` MUST be a bare YouTube video id (11-char `[A-Za-z0-9_-]`,
    same shape `resolve` hands back as `url`'s trailing segment) — anything
    else is rejected as 404 before ever touching the filesystem
    (`trailer_service.is_valid_youtube_id`), and
    `resolve_confined_cache_path` additionally *proves* the resolved path
    sits under `TRAILER_CACHE_DIR` (F-007 invariant) rather than trusting
    the regex alone.

    `Content-Encoding: identity` stops `GZipMiddleware` (main.py) from
    trying to gzip an already-compressed mp4 — Starlette's `GZipMiddleware`
    treats any response that already carries a `Content-Encoding` header as
    pre-encoded and passes it through untouched
    (`starlette.middleware.gzip.IdentityResponder`/`GZipResponder`)."""
    if not trailer_service.is_valid_youtube_id(cache_key):
        raise HTTPException(404, "Trailer not found")
    try:
        path = trailer_service.resolve_confined_cache_path(cache_key)
    except (trailer_service.TrailerDisabledError, trailer_service.TrailerPathConfinementError):
        raise HTTPException(404, "Trailer not found")
    if not path.is_file():
        raise HTTPException(404, "Trailer not found")
    return FileResponse(
        path, media_type="video/mp4", headers={"Content-Encoding": "identity"},
    )


@router.get("/{rating_key}", response_model=MediaResponse)
async def get_media(
    rating_key: str,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    item = await media_service.get_media_by_key(db, rating_key, server_id)
    if not item:
        raise HTTPException(404, "Media not found")
    return MediaResponse.model_validate(item)


@router.patch("/{rating_key}", response_model=MediaResponse)
async def update_media(
    rating_key: str,
    body: MediaUpdate,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Patch a media item. Updates every (filter, sort_order) variant under the
    same (rating_key, server_id) so the value stays consistent across all
    pagination buckets."""
    fields = {
        k: v for k, v in body.model_dump(exclude_unset=True).items()
        if k in ("imdb_id", "tmdb_id")
    }
    updated = await media_service.update_external_ids(
        db, rating_key, server_id, fields=fields,
    )
    if not updated:
        raise HTTPException(404, "Media not found")
    return MediaResponse.model_validate(updated)


@router.post("/{rating_key}/rescrape", status_code=202)
async def rescrape_media(
    rating_key: str,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Re-queue a media item for enrichment."""
    ok = await media_service.enqueue_rescrape(db, rating_key, server_id)
    if not ok:
        raise HTTPException(404, "Media not found")
    return {"status": "queued"}
