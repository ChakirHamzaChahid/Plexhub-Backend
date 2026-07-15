"""Admin web UI — "Télécharger" tab (physical media download, HTMX + Jinja2).

Mounted at ``/admin/downloads`` (Basic Auth applied at mount time in
``main.py``, same convention as ``admin.router`` — see PH-DL-06 wiring,
``docs/20-impl-media-download.md`` §7.3). Routes here render HTML fragments
and delegate ALL business logic to ``app.services.download_service``
(enqueue/list/cancel/retry) and the existing READ-only
``app.services.media_service`` (browsing the already-unified catalogue) —
this router itself contains **no** business logic, per house convention.

Security invariants (house law + spec §0.7/F-007):
  * the upstream Xtream URL (embeds user/password) is NEVER constructed,
    logged, or rendered here — only ``title``/``label``/``serverId``/
    ``ratingKey``/job state are ever displayed;
  * the client never supplies a filesystem path — only a selection
    (``type``, ``unificationId``, ``scope``, a ``source`` of
    ``server_id|rating_key``, or repeated ``season_pick``/``episode_pick``
    selectors); the destination is computed server-side by
    ``download_service``.

X2 (granular per-episode/per-season download + size display): the versions
panel now also shows a size (``estimate_media_size`` — exact via
``Media.file_size`` when the health-check has populated it, else an
indicative bitrate*duration estimate flagged ``size_estimated``) and a show's
"Par saison"/"Par épisode" pickers let the operator choose a DIFFERENT source
PER season/episode (``download_service.list_series_{seasons,episodes}
_with_sources`` + ``enqueue_selection(scope='seasons'|'episodes', ...)``),
mirroring ``app.api.admin_plex_downloads`` (the Plex-sourced twin of this
router) 1:1.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.services import download_service
from app.services.aggregation_service import build_versions, canonical_title_year
from app.services.media_service import media_service

logger = logging.getLogger("plexhub.api.admin_downloads")

router = APIRouter(prefix="/admin/downloads", tags=["admin-downloads"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_GIB = 1024**3
_MIB = 1024**2


def _fmt_size(size_bytes: Optional[int]) -> str:
    """``"4.2 Go"`` (Gio) for >=1 GiB, ``"850.0 Mo"`` (Mio) otherwise,
    ``"—"`` for an unknown size (mirrors ``admin_plex_downloads._fmt_size``
    byte-for-byte — same Mio-based "Mo" label convention already used by
    ``_downloads_queue.html``'s byte counters)."""
    if size_bytes is None:
        return "—"
    if size_bytes >= _GIB:
        return f"{size_bytes / _GIB:.1f} Go"
    return f"{size_bytes / _MIB:.1f} Mo"


def _enrich_source_dict(source: dict) -> dict:
    """``list_series_seasons_with_sources``/``list_series_episodes_with_sources``
    already return per-source dicts (``size_bytes``/``size_estimated``) — add
    the pre-formatted ``size_h`` so the template never does size arithmetic
    itself (mirrors ``admin_plex_downloads._enrich_source_dict``)."""
    out = dict(source)
    out["size_h"] = _fmt_size(source.get("size_bytes"))
    return out


def _job_view(job) -> dict:
    """Presentation-only view of a ``DownloadJob`` row for the HTMX fragments.

    ``percent``/``speed_bps`` reuse ``download_service.compute_percent``/
    ``compute_speed_bps`` (spec §6.4: "extraire un helper pur ... réutilisé
    des deux côtés") — the same functions ``to_download_response`` uses for
    the JSON mirror, so the HTMX progress bar and ``GET /api/admin/downloads``
    can never disagree on a job's percent/speed.
    """
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
    """Shared context for every fragment response that re-renders the queue
    panel (queue/enqueue/cancel/retry/clear-finished)."""
    jobs, total = await download_service.list_jobs(db, limit=200, offset=0)
    return {"jobs": [_job_view(j) for j in jobs], "total": total, "enqueue_error": enqueue_error}


def _group_to_card(media_type: str, group) -> dict:
    """One catalogue card (spec F-001: "1 par titre, versionCount visible")."""
    best = group.best
    clean_title, clean_year = canonical_title_year(best)
    return {
        "unification_id": group.key,
        "type": media_type,
        "title": clean_title,
        "year": clean_year,
        "version_count": len(group.members),
        "thumb_url": best.resolved_thumb_url or best.thumb_url,
        "is_adult": bool(getattr(best, "is_adult", False)),
    }


async def _load_downloads_titles(
    db: AsyncSession, *, media_type: str, search: Optional[str], page: int, page_size: int,
):
    """Shared loader for the browse list (F-001) — reuses
    ``media_service.get_unified_list`` verbatim (READ-only, unmodified), so
    content is identical to ``/api/media/{movies,shows}/unified`` (spec §7.1)."""
    offset = max(0, (page - 1) * page_size)
    groups, total = await media_service.get_unified_list(
        db, media_type=media_type, limit=page_size, offset=offset, search=search or None,
    )
    cards = [_group_to_card(media_type, g) for g in groups]
    return cards, total, offset


# ──────────────────────────────────────────────────────────────────────────────
# F-001 — page + browse list
# ──────────────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_downloads_index(
    request: Request,
    type: str = Query("movie"),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=6, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = type if type in ("movie", "show") else "movie"
    cards, total, offset = await _load_downloads_titles(
        db, media_type=media_type, search=search, page=page, page_size=page_size,
    )
    ctx = {
        "items": cards,
        "total": total,
        "offset": offset,
        "page": page,
        "page_size": page_size,
        "type": media_type,
        "search": search or "",
    }
    ctx.update(await _queue_context(db))
    return templates.TemplateResponse(request, "admin/downloads.html", ctx)


@router.get("/list", response_class=HTMLResponse)
async def admin_downloads_list_fragment(
    request: Request,
    type: str = Query("movie"),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=6, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = type if type in ("movie", "show") else "movie"
    cards, total, offset = await _load_downloads_titles(
        db, media_type=media_type, search=search, page=page, page_size=page_size,
    )
    return templates.TemplateResponse(
        request,
        "admin/_downloads_list.html",
        {
            "items": cards, "total": total, "offset": offset,
            "page": page, "page_size": page_size,
            "type": media_type, "search": search or "",
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# F-002/X2 — versions of a title (movie: per-source size; show: whole-series
# sources + per-season/per-episode source pickers, MIRRORS
# `admin_plex_downloads`'s `/versions`+`/episodes` shape exactly). Both cases
# reuse `media_service.get_unified_group`, the same READ path
# `/api/media/{movies,shows}/unified?unificationId=` already uses for shows
# (see `api/media.py::list_shows_unified`); the granular per-season/per
# -episode breakdown reuses `download_service.list_series_{seasons,episodes}
# _with_sources` (the same cross-account episode aggregation the "versions"
# panel and `/episodes/unified` API already use).
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/{type}/{unification_id:path}/versions", response_class=HTMLResponse)
async def admin_downloads_versions(
    type: str,
    unification_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """``unification_id`` values look like ``imdb://tt0088247``/``tmdb://12345``
    (literal ``/`` characters) — the ``:path`` Starlette converter is required
    here so this survives both a raw browser navigation (slashes sent as-is)
    and a percent-encoded one (``%2F``); FastAPI's default single-segment
    ``str`` converter 404s on either (verified against this exact id shape)."""
    if type not in ("movie", "show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown type")

    group = await media_service.get_unified_group(db, type, unification_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unificationId inconnu")

    labels = await media_service.account_labels(db)
    versions = []
    for m, label in build_versions(
        group.members, lambda m: labels.get(m.server_id, m.server_id)
    ):
        version: dict = {
            "server_id": m.server_id,
            "rating_key": m.rating_key,
            "label": label,
            "is_broken": bool(m.is_broken),
        }
        if type == "movie":
            # Whole-series sources (show case) intentionally carry no size —
            # per-season/per-episode granularity is where a show's size lives
            # (below), same convention as `admin_plex_downloads`.
            size_bytes, size_estimated = download_service.estimate_media_size(m)
            version["size_bytes"] = size_bytes
            version["size_h"] = _fmt_size(size_bytes)
            version["size_estimated"] = size_estimated
        versions.append(version)

    seasons: list[dict] = []
    if type == "show":
        season_rows = await download_service.list_series_seasons_with_sources(
            db, unification_id
        )
        seasons = [
            {
                "season": row.season,
                "sources": [_enrich_source_dict(s) for s in row.sources],
            }
            for row in season_rows
        ]

    clean_title, clean_year = canonical_title_year(group.best)
    return templates.TemplateResponse(
        request,
        "admin/_downloads_versions.html",
        {
            "type": type,
            "unification_id": unification_id,
            "title": clean_title,
            "year": clean_year,
            "versions": versions,
            "seasons": seasons,
        },
    )


@router.get("/{type}/{unification_id:path}/episodes", response_class=HTMLResponse)
async def admin_downloads_episodes(
    type: str,
    unification_id: str,
    request: Request,
    season: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Per-episode source picker for one season (X2) — ``:path`` converter
    required for the same reason as ``/versions`` above."""
    if type not in ("movie", "show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown type")

    rows = await download_service.list_series_episodes_with_sources(
        db, unification_id, season
    )
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
        "admin/_downloads_episodes.html",
        {"unification_id": unification_id, "season": season, "episodes": episodes},
    )


# ──────────────────────────────────────────────────────────────────────────────
# F-003/F-004/F-005/F-006/F-104/X2 — enqueue + queue panel + cancel/retry/clear
# ──────────────────────────────────────────────────────────────────────────────


def _parse_source(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``"server_id|rating_key"`` -> ``(server_id, rating_key)``, split on the
    FIRST ``|`` only (mirrors ``admin_plex_downloads._parse_source``)."""
    if not raw or "|" not in raw:
        return None, None
    server_id, rating_key = raw.split("|", 1)
    server_id, rating_key = server_id.strip(), rating_key.strip()
    if not server_id or not rating_key:
        return None, None
    return server_id, rating_key


def _parse_season_picks(raw_picks: list[str]) -> list[tuple[int, str, str]]:
    """``"season|server_id|series_rating_key"`` entries -> parsed picks
    (mirrors ``admin_plex_downloads._parse_season_picks``)."""
    picks: list[tuple[int, str, str]] = []
    for raw in raw_picks:
        raw = (raw or "").strip()
        if not raw:
            continue  # "-- ne pas télécharger" placeholder
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
    """``"server_id|episode_rating_key"`` entries -> parsed picks (mirrors
    ``admin_plex_downloads._parse_episode_picks``)."""
    picks: list[tuple[str, str]] = []
    for raw in raw_picks:
        raw = (raw or "").strip()
        if not raw:
            continue  # "-- ne pas télécharger" placeholder
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
async def admin_downloads_enqueue(
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
    """Dispatch by scope (X2 — mirrors ``admin_plex_downloads_enqueue`` 1:1):
    ``movie``/``series_all`` take a single ``source``; ``seasons``/``episodes``
    take one source PER unit via the repeated ``season_pick``/``episode_pick``
    selectors, so one operator action can span multiple accounts. The old
    ``series_seasons`` (checkbox) scope is intentionally no longer reachable
    from this router — ``download_service.enqueue_selection`` still supports
    it for its own tests, but the UI now offers ``seasons`` (with a per-season
    source picker) instead."""
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
            result = await download_service.enqueue_selection(
                db, media_type=type, unification_id=unification_id, scope=scope,
                server_id=server_id, rating_key=rating_key,
            )
            error = result.error
    elif scope == "seasons":
        picks = _parse_season_picks(season_pick)
        if not picks:
            error = "Aucune saison sélectionnée."
        else:
            result = await download_service.enqueue_selection(
                db, media_type=type, unification_id=unification_id, scope=scope,
                season_picks=picks,
            )
            error = result.error
    else:  # scope == "episodes"
        picks = _parse_episode_picks(episode_pick)
        if not picks:
            error = "Aucun épisode sélectionné."
        else:
            result = await download_service.enqueue_selection(
                db, media_type=type, unification_id=unification_id, scope=scope,
                episode_picks=picks,
            )
            error = result.error

    return templates.TemplateResponse(
        request, "admin/_downloads_queue.html", await _queue_context(db, enqueue_error=error),
    )


@router.get("/queue", response_class=HTMLResponse)
async def admin_downloads_queue(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse(
        request, "admin/_downloads_queue.html", await _queue_context(db),
    )


@router.post("/{job_id}/cancel", response_class=HTMLResponse)
async def admin_downloads_cancel(
    job_id: str, request: Request, db: AsyncSession = Depends(get_db),
):
    await download_service.cancel_job(db, job_id)
    return templates.TemplateResponse(
        request, "admin/_downloads_queue.html", await _queue_context(db),
    )


@router.post("/{job_id}/retry", response_class=HTMLResponse)
async def admin_downloads_retry(
    job_id: str, request: Request, db: AsyncSession = Depends(get_db),
):
    await download_service.retry_job(db, job_id)
    return templates.TemplateResponse(
        request, "admin/_downloads_queue.html", await _queue_context(db),
    )


@router.post("/clear-finished", response_class=HTMLResponse)
async def admin_downloads_clear_finished(
    request: Request, db: AsyncSession = Depends(get_db),
):
    await download_service.clear_finished(db)
    return templates.TemplateResponse(
        request, "admin/_downloads_queue.html", await _queue_context(db),
    )


__all__ = ["router"]
