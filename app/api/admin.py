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

from app.config import settings
from app.db.database import get_db
from app.models.schemas import MediaUpdate
from app.services.media_service import media_service
from app.services import nfo_import_service


router = APIRouter(prefix="/admin", tags=["admin"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


async def _load_movies_page(
    db: AsyncSession,
    *,
    missing_imdb: bool,
    missing_tmdb: bool,
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
        missing_tmdb=missing_tmdb,
    )
    return items, total, offset


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_index(
    request: Request,
    missing_imdb: bool = Query(True),
    missing_tmdb: bool = Query(False),
    search: Optional[str] = Query(None),
    sort: str = Query("added_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    db: AsyncSession = Depends(get_db),
):
    items, total, offset = await _load_movies_page(
        db, missing_imdb=missing_imdb, missing_tmdb=missing_tmdb,
        search=search, sort=sort, page=page, page_size=page_size,
    )
    total_movies, missing_imdb_count, missing_tmdb_count = (
        await media_service.count_movies_missing_external(db)
    )
    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "items": items,
            "total": total,
            "offset": offset,
            "page": page,
            "page_size": page_size,
            "missing_imdb": missing_imdb,
            "missing_tmdb": missing_tmdb,
            "search": search or "",
            "sort": sort,
            "total_movies": total_movies,
            "missing_imdb_count": missing_imdb_count,
            "missing_tmdb_count": missing_tmdb_count,
        },
    )


@router.get("/movies", response_class=HTMLResponse)
async def admin_movies_fragment(
    request: Request,
    missing_imdb: bool = Query(True),
    missing_tmdb: bool = Query(False),
    search: Optional[str] = Query(None),
    sort: str = Query("added_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    db: AsyncSession = Depends(get_db),
):
    items, total, offset = await _load_movies_page(
        db, missing_imdb=missing_imdb, missing_tmdb=missing_tmdb,
        search=search, sort=sort, page=page, page_size=page_size,
    )
    return templates.TemplateResponse(
        request,
        "admin/_movies_table.html",
        {
            "items": items,
            "total": total,
            "offset": offset,
            "page": page,
            "page_size": page_size,
            "missing_imdb": missing_imdb,
            "missing_tmdb": missing_tmdb,
            "search": search or "",
            "sort": sort,
        },
    )


@router.get("/movies/stats", response_class=HTMLResponse)
async def admin_stats_fragment(
    request: Request, db: AsyncSession = Depends(get_db),
):
    total, missing_imdb, missing_tmdb = (
        await media_service.count_movies_missing_external(db)
    )
    return templates.TemplateResponse(
        request,
        "admin/_stats.html",
        {
            "total_movies": total,
            "missing_imdb_count": missing_imdb,
            "missing_tmdb_count": missing_tmdb,
        },
    )


@router.post("/movies/{rating_key}/ids", response_class=HTMLResponse)
async def admin_update_ids(
    rating_key: str,
    request: Request,
    server_id: str = Form(...),
    imdb_id: Optional[str] = Form(None),
    tmdb_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Update one or both external IDs from a row form. Fields not submitted
    (i.e. None) are left untouched; empty string clears them."""
    payload_data: dict = {}
    if imdb_id is not None:
        payload_data["imdb_id"] = imdb_id
    if tmdb_id is not None:
        payload_data["tmdb_id"] = tmdb_id

    try:
        payload = MediaUpdate(**payload_data)
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

    fields = {
        k: v for k, v in payload.model_dump(exclude_unset=True).items()
        if k in ("imdb_id", "tmdb_id")
    }
    updated = await media_service.update_external_ids(
        db, rating_key, server_id, fields=fields,
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


@router.get("/import-nfo", response_class=HTMLResponse)
async def admin_import_nfo_form(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/import_nfo.html",
        {
            "library_dir": settings.PLEX_LIBRARY_DIR,
            "report_groups": None,
            "submitted": False,
            "kinds": ["movies", "shows"],
            "overwrite": False,
            "dry_run": True,
            "error": None,
        },
    )


@router.post("/import-nfo", response_class=HTMLResponse)
async def admin_import_nfo_run(
    request: Request,
    movies: Optional[str] = Form(None),
    shows: Optional[str] = Form(None),
    overwrite: Optional[str] = Form(None),
    dry_run: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    from pathlib import Path

    library_dir = settings.PLEX_LIBRARY_DIR
    selected_kinds: list[str] = []
    if movies:
        selected_kinds.append("movies")
    if shows:
        selected_kinds.append("shows")

    error: Optional[str] = None
    reports = None

    if not library_dir:
        error = (
            "PLEX_LIBRARY_DIR n'est pas défini dans .env — "
            "le backend ne sait pas où plexhub-backend stocke les .nfo."
        )
    elif not selected_kinds:
        error = "Sélectionne au moins un type (films ou séries)."
    else:
        root_path = Path(library_dir)
        if not root_path.exists():
            error = f"PLEX_LIBRARY_DIR introuvable côté serveur : {library_dir}"
        elif not root_path.is_dir():
            error = f"PLEX_LIBRARY_DIR n'est pas un dossier : {library_dir}"
        else:
            reports = await nfo_import_service.import_nfo(
                db, root_path,
                kinds=tuple(selected_kinds),
                overwrite=bool(overwrite),
                dry_run=bool(dry_run),
            )

    return templates.TemplateResponse(
        request,
        "admin/import_nfo.html",
        {
            "library_dir": library_dir,
            "report_groups": reports,
            "submitted": True,
            "kinds": selected_kinds or ["movies", "shows"],
            "overwrite": bool(overwrite),
            "dry_run": bool(dry_run),
            "error": error,
        },
        headers={"HX-Trigger": "refresh-stats"} if reports and not dry_run else None,
    )


__all__ = ["router"]
