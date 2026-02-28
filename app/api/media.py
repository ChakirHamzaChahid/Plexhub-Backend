from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.database import get_db
from app.models.schemas import MediaResponse, MediaListResponse
from app.services.media_service import media_service

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/movies", response_model=MediaListResponse)
async def list_movies(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("added_desc"),
    server_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="movie", limit=limit, offset=offset,
        sort=sort, server_id=server_id,
    )
    return MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/shows", response_model=MediaListResponse)
async def list_shows(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("added_desc"),
    server_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="show", limit=limit, offset=offset,
        sort=sort, server_id=server_id,
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
