"""Admin web UI — unified "Téléchargements" tab merging the Xtream and Plex
download catalogues into ONE deduplicated browse screen (feature "écran de
téléchargement unifié", Vague W3).

A third tab alongside the source-specific "Télécharger" (Xtream) and
"Télécharger Plex" tabs — those stay untouched. This router only BROWSES:
`unified_download_catalog_service` merges the two catalogues by
`unification_id` so a title present in both sources shows once, with a badge
per origin. The actual per-source selection + enqueue is delegated VERBATIM to
the existing origin routers: the merged card lazy-loads each origin's existing
`/versions` fragment (`/admin/downloads/…` and `/admin/plex-downloads/…`),
whose enqueue forms already POST to their own endpoints and target the SHARED
`#downloads-queue`. So there is **no new enqueue/worker/queue logic here** — the
worker already dispatches by `is_plex_server_id`, and the queue is common.

Mounted at ``/admin/unified-downloads`` (Basic Auth at mount time in
``main.py``, same convention as the other two admin download tabs). Router =
validation + delegation only; the merge/dedup lives in the service. Secret-free
by construction (reuses the secret-free `plex_catalog_service` reads — never a
Plex ``access_token``/``base_uri``).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_downloads import _queue_context  # shared queue panel context
from app.db.database import get_db
from app.services import unified_download_catalog_service as unified_catalog

logger = logging.getLogger("plexhub.api.admin_unified_downloads")

router = APIRouter(prefix="/admin/unified-downloads", tags=["admin-unified-downloads"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _card_to_dict(card: "unified_catalog.UnifiedCard") -> dict:
    return {
        "unification_id": card.unification_id,
        "type": card.type,
        "title": card.title,
        "year": card.year,
        "origins": card.origins,          # e.g. ["plex", "xtream"]
        "source_count": card.source_count,
    }


async def _load_cards(
    db: AsyncSession, *, media_type: str, search: Optional[str], genre: Optional[str],
    page: int, page_size: int,
):
    offset = max(0, (page - 1) * page_size)
    cards, total, truncated = await unified_catalog.list_unified(
        db, media_type=media_type, search=search or None, genre=genre or None,
        limit=page_size, offset=offset,
    )
    return [_card_to_dict(c) for c in cards], total, truncated, offset


def _list_context(cards, total, truncated, offset, *, page, page_size, media_type, search, genre):
    # `catalogue_total` is the browse-count the list fragment renders. It is
    # DISTINCT from `total` because the full-page index also merges the shared
    # queue panel's context (`_queue_context` sets its own `total` = job count),
    # which would otherwise clobber the catalogue count on first render (the
    # `/list` HTMX refresh never merges the queue, so it was correct there only).
    return {
        "items": cards, "total": total, "catalogue_total": total,
        "truncated": truncated, "offset": offset,
        "page": page, "page_size": page_size, "type": media_type,
        "search": search or "", "genre": genre or "",
    }


# ── Browse: index + list fragment ──────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_unified_downloads_index(
    request: Request,
    type: str = Query("movie"),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=6, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = type if type in ("movie", "show") else "movie"
    cards, total, truncated, offset = await _load_cards(
        db, media_type=media_type, search=search, genre=genre, page=page, page_size=page_size,
    )
    ctx = _list_context(
        cards, total, truncated, offset,
        page=page, page_size=page_size, media_type=media_type, search=search, genre=genre,
    )
    ctx.update(await _queue_context(db))
    return templates.TemplateResponse(request, "admin/unified_downloads.html", ctx)


@router.get("/list", response_class=HTMLResponse)
async def admin_unified_downloads_list_fragment(
    request: Request,
    type: str = Query("movie"),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=6, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = type if type in ("movie", "show") else "movie"
    cards, total, truncated, offset = await _load_cards(
        db, media_type=media_type, search=search, genre=genre, page=page, page_size=page_size,
    )
    return templates.TemplateResponse(
        request, "admin/_unified_downloads_list.html",
        _list_context(
            cards, total, truncated, offset,
            page=page, page_size=page_size, media_type=media_type, search=search, genre=genre,
        ),
    )


# ── Per-card sources: which origins carry it (each origin's existing version
#    picker is embedded via HTMX from here) ──────────────────────────────────


@router.get("/{type}/{unification_id:path}/sources", response_class=HTMLResponse)
async def admin_unified_downloads_sources(
    type: str,
    unification_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """``unification_id`` carries literal ``/`` (``imdb://tt…``) — the
    ``:path`` converter is required, same as the origin routers' ``/versions``.
    Renders one section per origin present; each lazy-loads that origin's
    EXISTING version fragment, whose enqueue forms target the shared queue."""
    if type not in ("movie", "show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown type")

    avail = await unified_catalog.get_group_availability(db, type, unification_id)
    if avail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unificationId inconnu")

    return templates.TemplateResponse(
        request, "admin/_unified_downloads_sources.html",
        {"avail": avail, "type": type, "unification_id": unification_id},
    )
