"""JSON read-only mirror of the Plex shared-servers catalogue (feature
"Télécharger Plex", Tâche C7, docs/10-prd-plex-download.md).

Mounted at ``/api/admin/plex-downloads``, guarded **module-level** by
``verify_master_key`` — Pattern C (self-prefix + self-guard), same convention
as ``app/api/downloads.py`` (the Xtream download-queue JSON mirror) and
``app/api/api_keys.py`` — so only the master secret (never a per-user key)
can read this catalogue. Additive, never touched by the app Android client
(the Plex catalogue is admin-only, out of scope for PlexHubTV — same
non-goal as the Xtream download feature, PRD §6).

Read-only: all mutations (catalogue sync, enqueue/cancel/retry) stay on the
HTMX admin router (``app/api/admin_plex_downloads.py``); this router exists
purely so QA/automation can read the deduplicated catalogue + known servers
over JSON, mirroring ``app/api/downloads.py``'s scope for the shared
download-job queue.

Delegates every read to ``app.services.plex_catalog_service`` — no business
logic here (router = validation + delegation only, house convention).

Never exposes ``PlexServer.access_token``/``base_uri`` (per-server secret +
its connection URI — see ``PlexServer``'s docstring in
``app/models/database.py``): ``plex_catalog_service.list_servers`` doesn't
even carry those fields in the dict it returns, so there is nothing to leak.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_master_key
from app.db.database import get_db
from app.models.schemas import (
    PlexCatalogResponse,
    PlexServerListResponse,
    PlexServerResponse,
    PlexSourceResponse,
    PlexUnifiedItemResponse,
)
from app.services import plex_catalog_service

logger = logging.getLogger("plexhub.api.plex_downloads")

router = APIRouter(
    prefix="/api/admin/plex-downloads",
    tags=["plex-downloads"],
    dependencies=[Depends(verify_master_key)],
)


@router.get("/servers", response_model=PlexServerListResponse, response_model_by_alias=True)
async def list_plex_servers(db: AsyncSession = Depends(get_db)):
    """All known Plex servers (owned + shared), secret-free.

    ``plex_catalog_service.list_servers`` never carries ``access_token``/
    ``base_uri`` in the dicts it returns — there is no field to strip here,
    only to map onto the wire schema.
    """
    rows = await plex_catalog_service.list_servers(db)
    items = [
        PlexServerResponse(
            server_id=row["server_id"],
            client_identifier=row["client_identifier"],
            name=row["name"],
            owner_title=row["owner_title"],
            owned=row["owned"],
            is_reachable=row["is_reachable"],
            last_synced_at=row["last_synced_at"],
            last_sync_error=row["last_sync_error"],
        )
        for row in rows
    ]
    return PlexServerListResponse(items=items, total=len(items))


@router.get("/catalog", response_model=PlexCatalogResponse, response_model_by_alias=True)
async def list_plex_catalog(
    type: str = Query("movie", description="'movie' or 'show'"),
    search: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Paginated, deduplicated browse list (``GROUP BY unification_id``).

    ``sources`` is always ``[]`` here — ``plex_catalog_service.list_unified``
    deliberately doesn't hydrate per-source rows for a list endpoint (same
    convention as its own docstring); use the detail route below for a
    single group's sources.
    """
    media_type = type if type in ("movie", "show") else "movie"
    items, total = await plex_catalog_service.list_unified(
        db, media_type, search or None, limit=limit, offset=offset,
    )
    return PlexCatalogResponse(
        items=[
            PlexUnifiedItemResponse(
                unification_id=it.unification_id,
                type=it.type,
                title=it.title,
                year=it.year,
                source_count=it.source_count,
                sources=[],
            )
            for it in items
        ],
        total=total,
    )


@router.get(
    "/catalog/{type}/{unification_id:path}",
    response_model=PlexUnifiedItemResponse,
    response_model_by_alias=True,
)
async def get_plex_catalog_group(
    type: str,
    unification_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Full detail for one unified group, sources hydrated.

    ``:path`` converter required — ``unification_id`` values embed a literal
    ``://`` (``imdb://tt…``/``tmdb://…``/``plexsrc://…``), same reasoning as
    ``admin_plex_downloads.admin_plex_downloads_versions``.
    """
    if type not in ("movie", "show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown type")

    group = await plex_catalog_service.get_group(db, type, unification_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unificationId inconnu")

    return PlexUnifiedItemResponse(
        unification_id=group.unification_id,
        type=group.type,
        title=group.title,
        year=group.year,
        source_count=group.source_count,
        sources=[
            PlexSourceResponse(
                server_id=s.server_id,
                rating_key=s.rating_key,
                server_name=s.server_name,
                resolution=s.resolution,
                size_bytes=s.size_bytes,
                video_codec=s.video_codec,
                container=s.container,
            )
            for s in group.sources
        ],
    )


__all__ = ["router"]
