from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
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
    UnifiedMediaResponse,
    UnifiedMediaListResponse,
    UnifiedEpisodeResponse,
    UnifiedEpisodeListResponse,
    apply_adult_prefix,
)
from app.services.aggregation_service import (
    build_versions, canonical_title_year,
)
from app.services.media_service import media_service

router = APIRouter(prefix="/media", tags=["media"])


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
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="movie", limit=limit, offset=offset,
        sort=sort, server_id=server_id,
        search=search, genre=genre, year=year,
        missing_imdb=missing_imdb, missing_tmdb=missing_tmdb,
    )
    return _single_pass_json(MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
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
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="show", limit=limit, offset=offset,
        sort=sort, server_id=server_id,
        search=search, genre=genre, year=year,
    )
    return _single_pass_json(MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
    ))


@router.get("/episodes", response_model=MediaListResponse)
async def list_episodes(
    parent_rating_key: str = Query(...),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    server_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="episode", limit=limit, offset=offset,
        server_id=server_id, parent_rating_key=parent_rating_key,
    )
    return _single_pass_json(MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
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
