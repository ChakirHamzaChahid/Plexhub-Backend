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


def _resolve_confined_output_dir(client_output_dir: Optional[str]) -> Path:
    """Resolve the effective output directory, confined to ``settings.PLEX_LIBRARY_DIR``.

    CR-S01 fix: `output_dir` (camel `outputDir`) is client-supplied and reaches any
    holder of a valid API key (master OR per-user, `verify_backend_secret`), not just
    an operator. It must never be trusted as an arbitrary filesystem root — otherwise
    a low-trust per-user key can write/delete files anywhere the process can reach
    (see `docs/audit/cleanroom-2026-07-11/40-security.md`, CR-S01).

    The only allowed base is the operator-configured ``settings.PLEX_LIBRARY_DIR``.
    A client-supplied path is accepted only if, once resolved, it IS that base or a
    descendant of it (containment via `resolve()` + `Path.parents` membership — not a
    naive string prefix, which `..`/absolute-escape/mixed-separator paths can defeat).
    """
    base_raw = settings.PLEX_LIBRARY_DIR
    if not client_output_dir:
        if not base_raw:
            raise HTTPException(
                status_code=400,
                detail="outputDir is required (or set PLEX_LIBRARY_DIR in .env)",
            )
        return Path(base_raw)

    if not base_raw:
        # No configured safe root to confine an arbitrary client path to — reject
        # rather than allow a client to pick any writable location on the host.
        raise HTTPException(
            status_code=400,
            detail="outputDir is not allowed: PLEX_LIBRARY_DIR is not configured on the server",
        )

    base = Path(base_raw).resolve()
    candidate = Path(client_output_dir).resolve()
    if candidate != base and base not in candidate.parents:
        raise HTTPException(
            status_code=400,
            detail="outputDir must be inside the configured PLEX_LIBRARY_DIR",
        )
    return candidate


@router.post("/generate", response_model=GenerateResponse)
async def generate_plex_library(req: GenerateRequest):
    """Generate a Plex-compatible library from synced Xtream VOD data."""
    # Determine output directory, confined to settings.PLEX_LIBRARY_DIR (CR-S01).
    output = _resolve_confined_output_dir(req.output_dir)

    # CR-A02: generation wiring (DatabaseSource -> PlexLibraryGenerator ->
    # LocalStorage/DryRunStorage -> generate()) lives in one shared service now,
    # instead of being reconstructed inline here (and independently in
    # app/main.py and app/cli.py). Aliased to avoid shadowing this endpoint
    # function's own name.
    from app.services.plex_generation_service import (
        generate_plex_library as _run_plex_generation,
    )

    # Unified library: one flat, deduped tree. `accountId` (optional) restricts
    # the aggregation to a single account; otherwise all active accounts are
    # merged. `allAccounts` is kept for backward compatibility (now the default).
    account_ids = [req.account_id] if req.account_id else None

    report = await _run_plex_generation(
        account_ids=account_ids,
        output_dir=output,
        strm_only=req.strm_only,
        dry_run=req.dry_run,
    )

    return GenerateResponse(
        created=report.created,
        updated=report.updated,
        deleted=report.deleted,
        unchanged=report.unchanged,
        pruned=report.pruned,
        errors=report.errors,
        duration_seconds=round(report.duration_seconds, 2),
    )
