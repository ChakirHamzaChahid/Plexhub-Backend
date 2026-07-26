import logging
import uuid

from fastapi import APIRouter, HTTPException
from app.models.schemas import (
    SyncRequest,
    SyncStatusResponse,
    JobIdResponse,
    MessageResponse,
    SyncJobResponse,
    SyncJobListResponse,
)
from app.services import job_registry
from app.utils.tasks import create_background_task, cancel_task_by_name

logger = logging.getLogger("plexhub.api.sync")
router = APIRouter(prefix="/sync", tags=["sync"])


def _new_job_id(prefix: str) -> str:
    """Router-owned jobId generation (ADR 0004 Décision 2): the id is
    created HERE, once, and handed unchanged to the background task -- it
    is never re-derived from task/object identity (the old
    ``f"..._{id(task)}"`` bug) nor from a timestamp picked up later by the
    worker."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@router.post(
    "/xtream", status_code=202,
    response_model=JobIdResponse, response_model_by_alias=True,
)
async def trigger_sync(body: SyncRequest):
    """Trigger sync for a specific account."""
    from app.workers.sync_worker import sync_account

    job_id = _new_job_id(f"sync_{body.account_id}")
    # Register synchronously, BEFORE scheduling the background task, so a
    # GET /status/{job_id} issued right after this response always resolves
    # (no race against the task actually starting to run).
    job_registry.create_job(job_id, phase="sync")
    create_background_task(
        sync_account(body.account_id, job_id=job_id), name=f"sync_{body.account_id}"
    )
    return JobIdResponse(job_id=job_id)


@router.post(
    "/xtream/all", status_code=202,
    response_model=JobIdResponse, response_model_by_alias=True,
)
async def trigger_sync_all():
    """Trigger sync for all active accounts."""
    from app.workers.sync_worker import run_all_accounts

    job_id = _new_job_id("sync_all")
    job_registry.create_job(job_id, phase="sync")

    async def _tracked_sync_all() -> None:
        try:
            await run_all_accounts()
            job_registry.update_job(job_id, status="completed", progress={})
        except Exception as exc:
            logger.error(f"sync_all job {job_id} failed: {exc}", exc_info=True)
            job_registry.update_job(job_id, status="failed", error=str(exc))

    create_background_task(_tracked_sync_all(), name="sync_all")
    return JobIdResponse(job_id=job_id)


@router.delete(
    "/cancel/{task_name}", status_code=200,
    response_model=MessageResponse, response_model_by_alias=True,
)
async def cancel_sync(task_name: str):
    """Cancel a running sync task by name (e.g., 'sync_abc123' or 'sync_all')."""
    cancelled = cancel_task_by_name(task_name)
    if not cancelled:
        raise HTTPException(404, f"No running task named '{task_name}'")
    return MessageResponse(message=f"Task '{task_name}' cancelled")


@router.post(
    "/enrichment", status_code=202,
    response_model=JobIdResponse, response_model_by_alias=True,
)
async def trigger_enrichment():
    """Trigger TMDB enrichment manually."""
    from app.workers.enrichment_worker import run

    job_id = _new_job_id("enrichment")
    job_registry.create_job(job_id, phase="enrichment")

    async def _tracked_enrichment() -> None:
        try:
            await run()
            job_registry.update_job(job_id, status="completed", progress={})
        except Exception as exc:
            logger.error(f"enrichment job {job_id} failed: {exc}", exc_info=True)
            job_registry.update_job(job_id, status="failed", error=str(exc))

    create_background_task(_tracked_enrichment(), name="enrichment_manual")
    return JobIdResponse(job_id=job_id)


@router.post(
    "/validate-streams", status_code=202,
    response_model=JobIdResponse, response_model_by_alias=True,
)
async def trigger_stream_validation():
    """Trigger stream validation manually (checks unchecked/stale streams)."""
    from app.workers.health_check_worker import run_pipeline_validation

    job_id = _new_job_id("validation")
    job_registry.create_job(job_id, phase="validation")

    async def _tracked_validation() -> None:
        try:
            await run_pipeline_validation()
            job_registry.update_job(job_id, status="completed", progress={})
        except Exception as exc:
            logger.error(f"validation job {job_id} failed: {exc}", exc_info=True)
            job_registry.update_job(job_id, status="failed", error=str(exc))

    create_background_task(_tracked_validation(), name="stream_validation")
    return JobIdResponse(job_id=job_id)


@router.post(
    "/full-pipeline", status_code=202,
    response_model=JobIdResponse, response_model_by_alias=True,
)
async def trigger_full_pipeline():
    """Trigger the full pipeline: sync -> enrichment -> validation -> Plex generation.

    The job's ``phase`` field is advanced at each step (``sync`` ->
    ``enrichment`` -> ``validation`` -> ``generation`` -> ``snapshot``) so a
    poller can tell which stage a long-running pipeline run is in --
    resolving AUDIT-P6-005 without a dedicated endpoint.
    """
    from app.workers.sync_worker import run_all_accounts
    from app.workers.enrichment_worker import run as run_enrichment
    from app.workers.health_check_worker import run_pipeline_validation

    job_id = _new_job_id("pipeline")
    job_registry.create_job(job_id, phase="sync")

    async def _full_pipeline():
        try:
            await run_all_accounts()
            logger.info("Full pipeline: sync done — starting enrichment")

            job_registry.update_job(job_id, phase="enrichment")
            await run_enrichment()
            logger.info("Full pipeline: enrichment done — starting stream validation")

            job_registry.update_job(job_id, phase="validation")
            await run_pipeline_validation()
            logger.info("Full pipeline: validation done — starting Plex generation")

            job_registry.update_job(job_id, phase="generation")
            # CR-A02: was `from app.main import _auto_generate_plex_library` — a
            # router reaching into a private symbol of the app entrypoint. The
            # shared generation-wiring + gating now lives in
            # app.services.plex_generation_service (also used by app.main itself).
            from app.services.plex_generation_service import generate_plex_library_auto
            await generate_plex_library_auto()

            job_registry.update_job(
                job_id, phase="snapshot", status="completed", progress={},
            )
            logger.info("Full pipeline: complete")
        except Exception as exc:
            logger.error(f"full_pipeline job {job_id} failed: {exc}", exc_info=True)
            job_registry.update_job(job_id, status="failed", error=str(exc))

    create_background_task(_full_pipeline(), name="full_pipeline")
    return JobIdResponse(job_id=job_id)


@router.get(
    "/status/{job_id}",
    response_model=SyncStatusResponse,
    response_model_by_alias=True,
    responses={404: {"description": "Unknown job id"}},
)
async def get_sync_status(job_id: str):
    """Check a job's status from the shared in-memory registry.

    404 on an unknown id (ADR 0004 Décision 2 / AUDIT-P5-008): the previous
    200 ``{"status": "unknown"}`` was indistinguishable from a genuinely
    pending job. Unknown means never registered, evicted by the FIFO cap,
    or the process-local registry lost across a worker/master boundary or a
    restart (dette AUDIT-P1-008, assumed).
    """
    job = job_registry.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Unknown job id '{job_id}'")
    return SyncStatusResponse(
        status=job.get("status", "processing"),
        progress=job.get("progress"),
        phase=job.get("phase"),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
        error=job.get("error"),
    )


@router.get("/jobs", response_model=SyncJobListResponse, response_model_by_alias=True)
async def list_sync_jobs():
    """List all recent jobs (all 5 trigger kinds) with their status."""
    jobs = job_registry.get_all_jobs()
    return SyncJobListResponse(jobs=[SyncJobResponse(**j) for j in jobs])
