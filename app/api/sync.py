import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.schemas import SyncRequest, SyncStatusResponse

logger = logging.getLogger("plexhub.api.sync")
router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/xtream", status_code=202)
async def trigger_sync(body: SyncRequest):
    """Trigger sync for a specific account."""
    from app.workers.sync_worker import sync_account

    task = asyncio.create_task(sync_account(body.account_id))

    # Return a job ID (we'll use a simple approach)
    job_id = f"sync_{body.account_id}_{id(task)}"
    return {"jobId": job_id}


@router.post("/xtream/all", status_code=202)
async def trigger_sync_all():
    """Trigger sync for all active accounts."""
    from app.workers.sync_worker import run_all_accounts

    task = asyncio.create_task(run_all_accounts())
    job_id = f"sync_all_{id(task)}"
    return {"jobId": job_id}


@router.get("/status/{job_id}", response_model=SyncStatusResponse)
async def get_sync_status(job_id: str):
    """Check sync job status."""
    # Simple implementation - always return "unknown" for now
    # A production implementation would track job state
    return SyncStatusResponse(status="unknown")
