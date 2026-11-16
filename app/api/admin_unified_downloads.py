"""Admin web UI — unified "Téléchargements" tab merging the Xtream and Plex
download catalogues into ONE deduplicated browse screen (feature "écran de
téléchargement unifié", Vague W3).

A third tab alongside the source-specific "Télécharger" (Xtream) and
"Télécharger Plex" tabs — those stay untouched. This router only BROWSES:
`unified_download_catalog_service` merges the two catalogues by
`unification_id` so a title present in both sources shows once, with a badge
per origin. The actual per-source selection + enqueue is delegated VERBATIM to
the existing origin routers: each merged card (a ``<details>``) carries, as
STATIC children, one lazy loader per present origin that pulls that origin's
existing `/versions` fragment (`/admin/downloads/…` and
`/admin/plex-downloads/…`), whose enqueue forms already POST to their own
endpoints and target the SHARED `#downloads-queue`. So there is **no new
enqueue/worker/queue logic here** — the worker already dispatches by
`is_plex_server_id`, and the queue is common.

The loaders fire on the ``<details>``'s real ``toggle`` DOM event
(``hx-trigger="toggle once from:closest details"``) rather than on an
auto-trigger inside HTMX-swapped content: htmx does NOT fire ``load``/
``revealed``/``intersect`` on content swapped into a target that is a
*descendant* of the element carrying the ``hx-get`` (verified in-browser), so
the earlier ``<details hx-get>`` → ``/sources`` → nested ``hx-trigger="load"``
two-hop silently never fired. Because the card already knows which origins it
has (``UnifiedCard.origins``), the loaders are rendered directly into the list
fragment and no intermediate ``/sources`` round-trip is needed.

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

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_downloads import _queue_context  # shared queue panel context
from app.config import settings
from app.db.database import get_db
from app.services import plex_catalog_service
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


_ID_FILTERS = ("all", "has_id", "imdb", "tmdb", "none")
_SORT_OPTIONS = ("title_asc", "added_desc", "year_desc")


def _parse_opt_int(raw: Optional[str]) -> Optional[int]:
    if not raw or not raw.strip().isdigit():
        return None
    val = int(raw.strip())
    return val if val > 0 else None


async def _load_cards(
    db: AsyncSession, *, media_type: str, search: Optional[str], genre: Optional[str],
    plex_server_id: Optional[str], id_filter: str,
    days: Optional[int], year: Optional[int], sort: str,
    page: int, page_size: int,
):
    offset = max(0, (page - 1) * page_size)
    norm_id = id_filter if id_filter in _ID_FILTERS else "all"
    norm_sort = sort if sort in _SORT_OPTIONS else "title_asc"
    cards, total, truncated = await unified_catalog.list_unified(
        db, media_type=media_type, search=search or None, genre=genre or None,
        plex_server_id=plex_server_id or None, id_filter=norm_id,
        days=days, year=year, sort=norm_sort,
        limit=page_size, offset=offset,
    )
    return [_card_to_dict(c) for c in cards], total, truncated, offset


def _list_context(
    cards, total, truncated, offset,
    *, page, page_size, media_type, search, genre, plex_server_id, id_filter,
    days, year, sort,
):
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
        "plex_server_id": plex_server_id or "",
        "id_filter": id_filter if id_filter in _ID_FILTERS else "all",
        "days": str(days) if days else "",
        "year": str(year) if year else "",
        "sort": sort if sort in _SORT_OPTIONS else "title_asc",
    }


# ── Browse: index + list fragment ──────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_unified_downloads_index(
    request: Request,
    type: str = Query("movie"),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    plex_server_id: Optional[str] = Query(None),
    id_filter: str = Query("all"),
    days: Optional[str] = Query(None),
    year: Optional[str] = Query(None),
    sort: str = Query("title_asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=6, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = type if type in ("movie", "show") else "movie"
    days_i = _parse_opt_int(days)
    year_i = _parse_opt_int(year)
    cards, total, truncated, offset = await _load_cards(
        db, media_type=media_type, search=search, genre=genre,
        plex_server_id=plex_server_id, id_filter=id_filter,
        days=days_i, year=year_i, sort=sort,
        page=page, page_size=page_size,
    )
    ctx = _list_context(
        cards, total, truncated, offset,
        page=page, page_size=page_size, media_type=media_type,
        search=search, genre=genre, plex_server_id=plex_server_id, id_filter=id_filter,
        days=days_i, year=year_i, sort=sort,
    )
    ctx["plex_enabled"] = bool(settings.PLEX_ACCOUNT_TOKEN)
    ctx["plex_servers"] = await plex_catalog_service.list_servers(db)
    ctx["sync_status"] = await plex_catalog_service.get_sync_status(db)
    ctx.update(await _queue_context(db))
    return templates.TemplateResponse(request, "admin/unified_downloads.html", ctx)


@router.get("/list", response_class=HTMLResponse)
async def admin_unified_downloads_list_fragment(
    request: Request,
    type: str = Query("movie"),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    plex_server_id: Optional[str] = Query(None),
    id_filter: str = Query("all"),
    days: Optional[str] = Query(None),
    year: Optional[str] = Query(None),
    sort: str = Query("title_asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=6, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = type if type in ("movie", "show") else "movie"
    days_i = _parse_opt_int(days)
    year_i = _parse_opt_int(year)
    cards, total, truncated, offset = await _load_cards(
        db, media_type=media_type, search=search, genre=genre,
        plex_server_id=plex_server_id, id_filter=id_filter,
        days=days_i, year=year_i, sort=sort,
        page=page, page_size=page_size,
    )
    return templates.TemplateResponse(
        request, "admin/_unified_downloads_list.html",
        _list_context(
            cards, total, truncated, offset,
            page=page, page_size=page_size, media_type=media_type,
            search=search, genre=genre, plex_server_id=plex_server_id, id_filter=id_filter,
            days=days_i, year=year_i, sort=sort,
        ),
    )
