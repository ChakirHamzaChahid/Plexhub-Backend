import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pydantic.alias_generators import to_camel

from app.config import settings

logger = logging.getLogger("plexhub.api.plex")
router = APIRouter(prefix="/plex", tags=["plex"])


class GenerateRequest(BaseModel):
    model_config = {"alias_generator": to_camel, "populate_by_name": True}
    account_id: Optional[str] = None
    all_accounts: bool = False
    output_dir: Optional[str] = None
    strm_only: bool = False
    dry_run: bool = False


class GenerateResponse(BaseModel):
    model_config = {"alias_generator": to_camel, "populate_by_name": True}
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    pruned: int = 0
    errors: list[str] = []
    duration_seconds: float = 0.0


@router.post("/generate", response_model=GenerateResponse)
async def generate_plex_library(req: GenerateRequest):
    """Generate a Plex-compatible library from synced Xtream VOD data."""
    # Determine output directory
    output_dir = req.output_dir or settings.PLEX_LIBRARY_DIR
    if not output_dir:
        raise HTTPException(
            status_code=400,
            detail="outputDir is required (or set PLEX_LIBRARY_DIR in .env)",
        )
    output = Path(output_dir)

    from app.plex_generator.generator import PlexLibraryGenerator
    from app.plex_generator.source import DatabaseSource
    from app.plex_generator.storage import LocalStorage, DryRunStorage

    # Unified library: one flat, deduped tree. `accountId` (optional) restricts
    # the aggregation to a single account; otherwise all active accounts are
    # merged. `allAccounts` is kept for backward compatibility (now the default).
    account_ids = [req.account_id] if req.account_id else None

    storage = DryRunStorage() if req.dry_run else LocalStorage(output)
    source = DatabaseSource(account_ids)
    gen = PlexLibraryGenerator(source, storage, output, req.strm_only)
    report = await gen.generate()

    return GenerateResponse(
        created=report.created,
        updated=report.updated,
        deleted=report.deleted,
        unchanged=report.unchanged,
        pruned=report.pruned,
        errors=report.errors,
        duration_seconds=round(report.duration_seconds, 2),
    )
