"""JSON read-only mirror of the download-job queue (P1, docs/20-impl-media-download.md §7.2).

Mounted at ``/api/admin/downloads``, guarded **module-level** by
``verify_master_key`` — Pattern C (self-prefix + self-guard), same convention
as ``app/api/api_keys.py`` — so only the master secret (never a per-user
key) can read the queue. Additive, never touched by the app Android client
(PRD §2: "download = admin-only, out of scope for PlexHubTV").

MVP scope is **P1 (read) only**: list + get-by-id, for QA/automation
consumption (spec §7.2). Mutation over JSON (P2: enqueue/cancel/retry) is out
of scope for this ticket — the HTMX admin router (``admin_downloads.py``)
owns the operator-facing mutations.

``download_service`` (PH-DL-03/04) is a parallel lot's file with a figée
contract this router codes against (``enqueue_selection``/``list_jobs``/
``get_job``/``cancel_job``/``retry_job``/``to_download_response`` —
``docs/20-impl-media-download.md`` §5).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_master_key
from app.db.database import get_db
from app.models.schemas import DownloadJobListResponse, DownloadJobResponse
from app.services import download_service

logger = logging.getLogger("plexhub.api.downloads")

router = APIRouter(
    prefix="/api/admin/downloads",
    tags=["downloads"],
    dependencies=[Depends(verify_master_key)],
)


@router.get("", response_model=DownloadJobListResponse, response_model_by_alias=True)
async def list_downloads(
    state: str | None = Query(
        None, description="Filter by a single state (queued|running|completed|failed|canceled)."
    ),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    states = [state] if state else None
    jobs, total = await download_service.list_jobs(db, states=states, limit=limit, offset=offset)
    return DownloadJobListResponse(
        items=[download_service.to_download_response(j) for j in jobs], total=total,
    )


@router.get("/{job_id}", response_model=DownloadJobResponse, response_model_by_alias=True)
async def get_download(job_id: str, db: AsyncSession = Depends(get_db)):
    job = await download_service.get_job(db, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Download job not found")
    return download_service.to_download_response(job)


__all__ = ["router"]
