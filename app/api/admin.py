"""Admin web UI — HTMX + Jinja2 catalogue editor.

Mounted at `/admin`, separate from the JSON API (`/api/...`). Routes here render
HTML fragments rather than JSON; they call the same `media_service` functions
the API uses, so business logic isn't duplicated.
"""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.schemas import MediaUpdate
from app.services.media_service import media_service


router = APIRouter(prefix="/admin", tags=["admin"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


async def _load_movies_page(
    db: AsyncSession,
    *,
    missing_imdb: bool,
    search: Optional[str],
    sort: str,
    page: int,
    page_size: int,
):
    offset = max(0, (page - 1) * page_size)
    items, total = await media_service.get_media_list(
        db,
        media_type="movie",
        limit=page_size,
        offset=offset,
        sort=sort,
        search=search or None,
        missing_imdb=missing_imdb,
        canonical_only=True,
    )
    return items, total, offset


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_index(
    request: Request,
    missing_imdb: bool = Query(True),
    search: Optional[str] = Query(None),
    sort: str = Query("added_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    db: AsyncSession = Depends(get_db),
):
    items, total, offset = await _load_movies_page(
        db, missing_imdb=missing_imdb, search=search,
        sort=sort, page=page, page_size=page_size,
    )
    total_movies, missing_count = await media_service.count_movies_missing_imdb(db)
    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "request": request,
            "items": items,
            "total": total,
            "offset": offset,
            "page": page,
            "page_size": page_size,
            "missing_imdb": missing_imdb,
            "search": search or "",
            "sort": sort,
            "total_movies": total_movies,
            "missing_count": missing_count,
        },
    )


@router.get("/movies", response_class=HTMLResponse)
async def admin_movies_fragment(
    request: Request,
    missing_imdb: bool = Query(True),
    search: Optional[str] = Query(None),
    sort: str = Query("added_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    db: AsyncSession = Depends(get_db),
):
    items, total, offset = await _load_movies_page(
        db, missing_imdb=missing_imdb, search=search,
        sort=sort, page=page, page_size=page_size,
    )
    return templates.TemplateResponse(
        request,
        "admin/_movies_table.html",
        {
            "request": request,
            "items": items,
            "total": total,
            "offset": offset,
            "page": page,
            "page_size": page_size,
            "missing_imdb": missing_imdb,
            "search": search or "",
            "sort": sort,
        },
    )


@router.get("/movies/stats", response_class=HTMLResponse)
async def admin_stats_fragment(
    request: Request, db: AsyncSession = Depends(get_db),
):
    total, missing = await media_service.count_movies_missing_imdb(db)
    return templates.TemplateResponse(
        request,
        "admin/_stats.html",
        {"total_movies": total, "missing_count": missing},
    )


@router.post("/movies/{rating_key}/imdb", response_class=HTMLResponse)
async def admin_update_imdb(
    rating_key: str,
    request: Request,
    server_id: str = Form(...),
    imdb_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    try:
        payload = MediaUpdate(imdb_id=imdb_id)
    except ValueError as exc:
        item = await media_service.get_media_by_key(db, rating_key, server_id)
        if not item:
            raise HTTPException(404, "Media not found")
        return templates.TemplateResponse(
            request,
            "admin/_movie_row.html",
            {"item": item, "error": str(exc)},
            status_code=422,
        )

    updated = await media_service.update_imdb_id(
        db, rating_key, server_id, payload.imdb_id,
    )
    if not updated:
        raise HTTPException(404, "Media not found")

    return templates.TemplateResponse(
        request,
        "admin/_movie_row.html",
        {"item": updated, "saved": True},
        headers={"HX-Trigger": "refresh-stats"},
    )


@router.post("/movies/{rating_key}/rescrape", response_class=HTMLResponse)
async def admin_rescrape(
    rating_key: str,
    request: Request,
    server_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ok = await media_service.enqueue_rescrape(db, rating_key, server_id)
    if not ok:
        raise HTTPException(404, "Media not found")
    item = await media_service.get_media_by_key(db, rating_key, server_id)
    return templates.TemplateResponse(
        request,
        "admin/_movie_row.html",
        {"item": item, "rescraped": True},
        headers={"HX-Trigger": "refresh-stats"},
    )


__all__ = ["router"]
