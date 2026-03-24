import logging

from fastapi import APIRouter, HTTPException
from app.models.schemas import SyncRequest, SyncStatusResponse
from app.utils.tasks import create_background_task, cancel_task_by_name

logger = logging.getLogger("plexhub.api.sync")
router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/xtream", status_code=202)
async def trigger_sync(body: SyncRequest):
    """Trigger sync for a specific account."""
    from app.workers.sync_worker import sync_account

    task = create_background_task(
        sync_account(body.account_id), name=f"sync_{body.account_id}"
    )
    job_id = f"sync_{body.account_id}_{id(task)}"
    return {"jobId": job_id}


@router.post("/xtream/all", status_code=202)
async def trigger_sync_all():
    """Trigger sync for all active accounts."""
    from app.workers.sync_worker import run_all_accounts

    task = create_background_task(run_all_accounts(), name="sync_all")
    job_id = f"sync_all_{id(task)}"
    return {"jobId": job_id}


@router.delete("/cancel/{task_name}", status_code=200)
async def cancel_sync(task_name: str):
    """Cancel a running sync task by name (e.g., 'sync_abc123' or 'sync_all')."""
    cancelled = cancel_task_by_name(task_name)
    if not cancelled:
        raise HTTPException(404, f"No running task named '{task_name}'")
    return {"message": f"Task '{task_name}' cancelled"}


@router.post("/enrichment", status_code=202)
async def trigger_enrichment():
    """Trigger TMDB enrichment manually."""
    from app.workers.enrichment_worker import run

    task = create_background_task(run(), name="enrichment_manual")
    return {"jobId": f"enrichment_{id(task)}"}


@router.get("/status/{job_id}", response_model=SyncStatusResponse)
async def get_sync_status(job_id: str):
    """Check sync job status from in-memory tracker."""
    from app.workers.sync_worker import get_sync_job
    job = get_sync_job(job_id)
    if not job:
        return SyncStatusResponse(status="unknown")
    return SyncStatusResponse(status=job.get("status", "unknown"))


@router.get("/jobs")
async def list_sync_jobs():
    """List all recent sync jobs with their status."""
    from app.workers.sync_worker import get_all_sync_jobs
    return {"jobs": get_all_sync_jobs()}
