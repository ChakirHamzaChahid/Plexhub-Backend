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

    if not req.account_id and not req.all_accounts:
        raise HTTPException(
            status_code=400,
            detail="Provide accountId or set allAccounts to true",
        )

    from app.plex_generator.generator import PlexLibraryGenerator
    from app.plex_generator.source import DatabaseSource
    from app.plex_generator.storage import LocalStorage, DryRunStorage

    storage = DryRunStorage() if req.dry_run else LocalStorage(output)
    reports = []

    if req.all_accounts:
        from sqlalchemy import select
        from app.db.database import async_session_factory
        from app.models.database import XtreamAccount

        async with async_session_factory() as db:
            result = await db.execute(
                select(XtreamAccount.id).where(XtreamAccount.is_active == True)
            )
            account_ids = [row[0] for row in result]

        if not account_ids:
            raise HTTPException(status_code=404, detail="No active accounts found")

        for aid in account_ids:
            source = DatabaseSource(aid)
            gen = PlexLibraryGenerator(source, storage, output, req.strm_only)
            reports.append(await gen.generate())
    else:
        source = DatabaseSource(req.account_id)
        gen = PlexLibraryGenerator(source, storage, output, req.strm_only)
        reports.append(await gen.generate())

    # Aggregate reports
    total = GenerateResponse()
    for r in reports:
        total.created += r.created
        total.updated += r.updated
        total.deleted += r.deleted
        total.unchanged += r.unchanged
        total.errors.extend(r.errors)
        total.duration_seconds += r.duration_seconds
    total.duration_seconds = round(total.duration_seconds, 2)

    return total
