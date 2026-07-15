"""Admin web UI -- "Telecharger Plex" tab (feature "Telecharger Plex",
ticket C6, docs/10-prd-media-download.md).

Mirrors `app.api.admin_downloads` (the Xtream-sourced "Telecharger" tab)
structure/conventions exactly, but reads from the Plex-only catalogue
(`app.services.plex_catalog_service`, tables `plex_server`/`plex_media_item`)
and enqueues via `app.services.plex_download_service` instead. The shared
download queue (`download_job`/`download_batch`) is common to both sources --
this router reuses the EXISTING queue fragment/routes
(`admin/_downloads_queue.html`, `/admin/downloads/queue`,
`/admin/downloads/{id}/cancel|retry`, `/admin/downloads/clear-finished`)
rather than redefining them, so cancel/retry/clear behave identically
regardless of which router enqueued the job.

Mounted at ``/admin/plex-downloads`` (Basic Auth applied at mount time in
``main.py``, same convention as ``admin_downloads.router``). Routes here
render HTML fragments and delegate ALL business logic to
``app.services.plex_catalog_service`` (read-only browse) and
``app.services.plex_download_service``/``app.services.plex_sync_service``
(enqueue / catalogue sync) -- this router itself contains **no** business
logic, per house convention (router = validation + delegation only).

Security invariants (house law + PlexServer/PlexMediaItem docstrings):
  * a Plex server's ``access_token``/``base_uri`` (secrets -- see
    ``PlexServer`` in ``models/database.py``) are NEVER read, logged, or
    rendered here -- only ``server_name``/``owner_title``/reachability/
    ``last_synced_at`` (all secret-free, per ``plex_catalog_service.list_servers``)
    ever reach a template;
  * the client never supplies a filesystem path -- only a selection
    (``type``, ``unificationId``, a ``source`` of ``server_id|rating_key``,
    or repeated ``season_pick``/``episode_pick`` selectors); the destination
    is computed server-side by ``download_service.compute_dest_path`` (via
    ``plex_download_service``).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.services import download_service, plex_catalog_service, plex_download_service
from app.utils.tasks import create_background_task

logger = logging.getLogger("plexhub.api.admin_plex_downloads")

router = APIRouter(prefix="/admin/plex-downloads", tags=["admin-plex-downloads"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_GIB = 1024**3
_MIB = 1024**2


def _feature_enabled() -> bool:
    """Read fresh on every call (never cached at import time) so tests can
    monkeypatch ``settings.PLEX_ACCOUNT_TOKEN`` per-case, mirroring
    ``plex_sync_service.run_full_sync``'s own guard."""
    return bool(settings.PLEX_ACCOUNT_TOKEN)


def _fmt_size(size_bytes: Optional[int]) -> str:
    """``"4.2 Go"`` (Gio) for >=1 GiB, ``"850.0 Mo"`` (Mio) otherwise,
    ``"—"`` for an unknown size. Same Mio-based "Mo" label convention
    already used by ``_downloads_queue.html``'s byte counters."""
    if size_bytes is None:
        return "—"
    if size_bytes >= _GIB:
        return f"{size_bytes / _GIB:.1f} Go"
    return f"{size_bytes / _MIB:.1f} Mo"


# ──────────────────────────────────────────────────────────────────────────────
# Shared queue panel (the queue itself belongs to admin_downloads.py's
# `_downloads_queue.html`/`/admin/downloads/queue`/cancel/retry/clear routes --
# replicated here ONLY because that fragment expects a `{jobs, total,
# enqueue_error}` context and this router's own POST needs to re-render it
# after an enqueue attempt, per ticket C6 scope).
# ──────────────────────────────────────────────────────────────────────────────


def _job_view(job) -> dict:
    """Presentation-only view of a ``DownloadJob`` row -- identical shape to
    ``admin_downloads._job_view`` (same shared table, same fragment)."""
    bytes_done = job.bytes_done or 0
    return {
        "id": job.id,
        "batch_id": job.batch_id,
        "type": job.media_type,
        "unification_id": job.unification_id,
        "title": job.title,
        "season": job.season,
        "episode": job.episode,
        "server_id": job.server_id,
        "rating_key": job.rating_key,
        "state": job.state,
        "bytes_downloaded": bytes_done,
        "bytes_total": job.bytes_total,
        "percent": download_service.compute_percent(job),
        "speed_bps": download_service.compute_speed_bps(job),
        "dest_path": job.dest_path,
        "error": job.error,
        "retries": job.attempts,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


async def _queue_context(db: AsyncSession, *, enqueue_error: Optional[str] = None) -> dict:
    """Shared context for the queue panel re-render after a Plex enqueue."""
    jobs, total = await download_service.list_jobs(db, limit=200, offset=0)
    return {"jobs": [_job_view(j) for j in jobs], "total": total, "enqueue_error": enqueue_error}


# ──────────────────────────────────────────────────────────────────────────────
# Catalogue -> template helpers
# ──────────────────────────────────────────────────────────────────────────────


def _item_to_card(item: "plex_catalog_service.PlexUnifiedItem") -> dict:
    """One catalogue card (mirrors admin_downloads' `_group_to_card`, minus
    `thumb_url`/`is_adult` which the Plex catalogue doesn't carry)."""
    return {
        "unification_id": item.unification_id,
        "type": item.type,
        "title": item.title,
        "year": item.year,
        "source_count": item.source_count,
    }


def _source_to_dict(source: "plex_catalog_service.PlexSource") -> dict:
    return {
        "server_id": source.server_id,
        "rating_key": source.rating_key,
        "server_name": source.server_name,
        "owner_title": source.owner_title,
        "resolution": source.resolution,
        "height": source.height,
        "size_bytes": source.size_bytes,
        "video_codec": source.video_codec,
        "audio_codec": source.audio_codec,
        "container": source.container,
        "size_h": _fmt_size(source.size_bytes),
    }


def _enrich_source_dict(source: dict) -> dict:
    """`list_seasons_with_sources`/`list_episodes_with_sources` already
    return per-source dicts (spec) -- add the pre-formatted `size_h` so the
    template never does size arithmetic itself."""
    out = dict(source)
    out["size_h"] = _fmt_size(source.get("size_bytes"))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Browse: index + list fragment
# ──────────────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_plex_downloads_index(
    request: Request,
    type: str = Query("movie"),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=6, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = type if type in ("movie", "show") else "movie"
    offset = max(0, (page - 1) * page_size)
    items, total = await plex_catalog_service.list_unified(
        db, media_type, search or None, limit=page_size, offset=offset,
    )
    servers = await plex_catalog_service.list_servers(db)
    sync_status = await plex_catalog_service.get_sync_status(db)
    ctx = {
        "items": [_item_to_card(it) for it in items],
        "total": total,
        "offset": offset,
        "page": page,
        "page_size": page_size,
        "type": media_type,
        "search": search or "",
        "servers": servers,
        "sync_status": sync_status,
        "feature_enabled": _feature_enabled(),
    }
    ctx.update(await _queue_context(db))
    return templates.TemplateResponse(request, "admin/plex_downloads.html", ctx)


@router.get("/list", response_class=HTMLResponse)
async def admin_plex_downloads_list_fragment(
    request: Request,
    type: str = Query("movie"),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=6, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = type if type in ("movie", "show") else "movie"
    offset = max(0, (page - 1) * page_size)
    items, total = await plex_catalog_service.list_unified(
        db, media_type, search or None, limit=page_size, offset=offset,
    )
    return templates.TemplateResponse(
        request,
        "admin/_plex_downloads_list.html",
        {
            "items": [_item_to_card(it) for it in items], "total": total, "offset": offset,
            "page": page, "page_size": page_size,
            "type": media_type, "search": search or "",
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Versions (movie: per-source quality; show: whole-series sources + seasons)
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/{type}/{unification_id:path}/versions", response_class=HTMLResponse)
async def admin_plex_downloads_versions(
    type: str,
    unification_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """``:path`` converter required -- ``unification_id`` values look like
    ``imdb://tt0088247``/``tmdb://12345``/``plexsrc://plex_<cid>/<rk>``
    (literal ``/`` characters), same reasoning as
    ``admin_downloads.admin_downloads_versions``."""
    if type not in ("movie", "show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown type")

    group = await plex_catalog_service.get_group(db, type, unification_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unificationId inconnu")

    sources = [_source_to_dict(s) for s in group.sources]
    seasons: list[dict] = []
    if type == "show":
        season_rows = await plex_catalog_service.list_seasons_with_sources(db, unification_id)
        seasons = [
            {
                "season": row.season,
                "sources": [_enrich_source_dict(s) for s in row.sources],
            }
            for row in season_rows
        ]

    return templates.TemplateResponse(
        request,
        "admin/_plex_downloads_versions.html",
        {
            "type": type,
            "unification_id": unification_id,
            "title": group.title,
            "year": group.year,
            "sources": sources,
            "seasons": seasons,
        },
    )


@router.get("/{type}/{unification_id:path}/episodes", response_class=HTMLResponse)
async def admin_plex_downloads_episodes(
    type: str,
    unification_id: str,
    request: Request,
    season: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    if type not in ("movie", "show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown type")

    rows = await plex_catalog_service.list_episodes_with_sources(db, unification_id, season)
    episodes = [
        {
            "season": row.season,
            "episode": row.episode,
            "title": row.title,
            "sources": [_enrich_source_dict(s) for s in row.sources],
        }
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "admin/_plex_downloads_episodes.html",
        {"unification_id": unification_id, "season": season, "episodes": episodes},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Enqueue
# ──────────────────────────────────────────────────────────────────────────────


def _parse_source(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``"server_id|rating_key"`` -> ``(server_id, rating_key)``, split on the
    FIRST ``|`` only (a Plex ``client_identifier`` never embeds ``|``, but
    splitting on the first occurrence keeps this robust either way)."""
    if not raw or "|" not in raw:
        return None, None
    server_id, rating_key = raw.split("|", 1)
    server_id, rating_key = server_id.strip(), rating_key.strip()
    if not server_id or not rating_key:
        return None, None
    return server_id, rating_key


def _parse_season_picks(raw_picks: list[str]) -> list[tuple[int, str, str]]:
    picks: list[tuple[int, str, str]] = []
    for raw in raw_picks:
        raw = (raw or "").strip()
        if not raw:
            continue  # "-- ne pas telecharger" placeholder
        parts = raw.split("|", 2)
        if len(parts) != 3:
            continue
        season_str, server_id, show_rating_key = (p.strip() for p in parts)
        if not season_str or not server_id or not show_rating_key:
            continue
        try:
            season = int(season_str)
        except ValueError:
            continue
        picks.append((season, server_id, show_rating_key))
    return picks


def _parse_episode_picks(raw_picks: list[str]) -> list[tuple[str, str]]:
    picks: list[tuple[str, str]] = []
    for raw in raw_picks:
        raw = (raw or "").strip()
        if not raw:
            continue  # "-- ne pas telecharger" placeholder
        parts = raw.split("|", 1)
        if len(parts) != 2:
            continue
        server_id, episode_rating_key = (p.strip() for p in parts)
        if not server_id or not episode_rating_key:
            continue
        picks.append((server_id, episode_rating_key))
    return picks


@router.post("", response_class=HTMLResponse)
@router.post("/", response_class=HTMLResponse)
async def admin_plex_downloads_enqueue(
    request: Request,
    type: str = Form(...),
    unification_id: str = Form(...),
    scope: str = Form(...),
    # movie / series_all: "server_id|rating_key". Unused for seasons/episodes.
    source: Optional[str] = Form(None),
    # Repeated `season_pick`/`episode_pick` selects (scope=seasons/episodes
    # only) -- FastAPI collects same-named form fields into these lists.
    season_pick: list[str] = Form(default=[]),
    episode_pick: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    error: Optional[str] = None

    if type not in ("movie", "show") or scope not in (
        "movie", "series_all", "seasons", "episodes",
    ):
        error = "Sélection invalide (type/scope inattendu)."
    elif scope in ("movie", "series_all"):
        server_id, rating_key = _parse_source(source)
        if not server_id or not rating_key:
            error = "Sélection invalide (source manquante)."
        else:
            result = await plex_download_service.enqueue_plex_selection(
                db, media_type=type, unification_id=unification_id, scope=scope,
                server_id=server_id, rating_key=rating_key,
            )
            error = result.error
    elif scope == "seasons":
        picks = _parse_season_picks(season_pick)
        if not picks:
            error = "Aucune saison sélectionnée."
        else:
            result = await plex_download_service.enqueue_plex_selection(
                db, media_type=type, unification_id=unification_id, scope=scope,
                season_picks=picks,
            )
            error = result.error
    else:  # scope == "episodes"
        picks = _parse_episode_picks(episode_pick)
        if not picks:
            error = "Aucun épisode sélectionné."
        else:
            result = await plex_download_service.enqueue_plex_selection(
                db, media_type=type, unification_id=unification_id, scope=scope,
                episode_picks=picks,
            )
            error = result.error

    return templates.TemplateResponse(
        request, "admin/_downloads_queue.html", await _queue_context(db, enqueue_error=error),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Catalogue sync
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/sync", response_class=HTMLResponse)
async def admin_plex_downloads_sync(request: Request, db: AsyncSession = Depends(get_db)):
    if not _feature_enabled():
        return templates.TemplateResponse(
            request,
            "admin/_plex_sync_status.html",
            {
                "sync_status": {"state": "disabled", "started_at": None, "finished_at": None, "error": None},
                "servers": [],
                "feature_enabled": False,
            },
        )

    # Lazy import so tests that monkeypatch `app.db.database.async_session_factory`
    # (to point background work at the in-memory test engine) are respected --
    # a module-level `from ... import async_session_factory` would bind the
    # ORIGINAL factory at import time and never see the monkeypatch, same
    # reasoning as `main.py`'s own background-task wiring.
    from app.db.database import async_session_factory
    from app.services import plex_sync_service

    create_background_task(plex_sync_service.run_full_sync(async_session_factory), name="plex_sync")

    sync_status = await plex_catalog_service.get_sync_status(db)
    servers = await plex_catalog_service.list_servers(db)
    return templates.TemplateResponse(
        request,
        "admin/_plex_sync_status.html",
        {"sync_status": sync_status, "servers": servers, "feature_enabled": True},
    )


@router.get("/sync/status", response_class=HTMLResponse)
async def admin_plex_downloads_sync_status(request: Request, db: AsyncSession = Depends(get_db)):
    sync_status = await plex_catalog_service.get_sync_status(db)
    servers = await plex_catalog_service.list_servers(db)
    return templates.TemplateResponse(
        request,
        "admin/_plex_sync_status.html",
        {"sync_status": sync_status, "servers": servers, "feature_enabled": _feature_enabled()},
    )


__all__ = ["router"]
