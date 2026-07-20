"""Manual OMDb ratings backfill admin endpoint — Wave 3 of the dual-provider
enrichment refacto (`docs/plans/2026-07-20-omdb-rating-enrichment-design.md`
§C4).

**Pattern C** (self-prefix + self-guard, mirrors `app/api/downloads.py` /
`app/api/plex_downloads.py`): mounted at `/api/admin/enrichment`,
module-level `dependencies=[Depends(verify_master_key)]` — admin-grade
(spends the OMDb daily budget + mutates the whole catalog) -> the master
secret ONLY, never a per-user `api_keys` row.

    POST /api/admin/enrichment/omdb-backfill  -> 202 {jobId, status, ...}
    GET  /api/admin/enrichment/jobs/{jobId}   -> 200 full job record | 404

Delegates all logic to `app.workers.enrichment_backfill_worker` (in-memory
202-job store, mirrors the AI embed-rebuild precedent, `app/api/ai.py`
`/embed/rebuild` + `/embed/jobs/{job_id}`) — this router is validation +
delegation only, no business logic (CLAUDE.md §house rule).
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api.deps import verify_master_key
from app.workers import enrichment_backfill_worker as backfill

logger = logging.getLogger("plexhub.api.enrichment")

router = APIRouter(
    prefix="/api/admin/enrichment",
    tags=["enrichment"],
    dependencies=[Depends(verify_master_key)],
)

_CAMEL_CONFIG = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class OmdbBackfillRequest(BaseModel):
    model_config = _CAMEL_CONFIG

    media_type: Literal["movie", "show", "all"] = "all"
    recompute_display_rating: bool = True
    limit: int | None = Field(default=None, ge=1)


class OmdbBackfillJobResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    scanned: int = 0
    omdb_fetched: int = 0
    imdb_filled: int = 0
    display_recomputed: int = 0
    errors: int = 0
    last_error: str | None = None
    started_at: int
    finished_at: int | None = None


def _to_response(job_id: str, job: dict) -> OmdbBackfillJobResponse:
    return OmdbBackfillJobResponse(job_id=job_id, **job)


@router.post(
    "/omdb-backfill",
    response_model=OmdbBackfillJobResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_omdb_backfill(
    body: OmdbBackfillRequest = OmdbBackfillRequest(),
) -> OmdbBackfillJobResponse:
    """Trigger a background OMDb ratings backfill.

    Returns immediately with 202 + the fresh job record. Poll
    `GET /jobs/{jobId}` for progress. Single-run guard: 409 if a backfill is
    already running in this process (never auto-runs at boot — admin
    button/script only).
    """
    if backfill.is_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An OMDb backfill is already running",
        )
    job_id = await backfill.enqueue_backfill(
        media_type=body.media_type,
        recompute_display_rating=body.recompute_display_rating,
        limit=body.limit,
    )
    job = backfill.get_job(job_id)
    if job is None:  # enqueue_backfill registers before returning; guard anyway (no `python -O` assert on the request path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Backfill job could not be registered",
        )
    return _to_response(job_id, job)


@router.get(
    "/jobs/{job_id}",
    response_model=OmdbBackfillJobResponse,
    response_model_by_alias=True,
)
async def get_omdb_backfill_job(job_id: str) -> OmdbBackfillJobResponse:
    job = backfill.get_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Backfill job not found")
    return _to_response(job_id, job)


__all__ = ["router"]
