from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.database import get_db
from app.models.schemas import (
    MediaResponse,
    MediaListResponse,
    MediaUpdate,
    MediaStatsResponse,
)
from app.services.media_service import media_service

router = APIRouter(prefix="/media", tags=["media"])


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
    return MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
    )


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
    return MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
    )


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
    return MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
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
