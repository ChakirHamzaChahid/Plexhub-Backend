"""Shared Plex/Jellyfin library-generation orchestration (CR-A02).

Before this module existed, the sequence "build a `DatabaseSource` -> wrap in a
`PlexLibraryGenerator` -> back it with `LocalStorage`/`DryRunStorage` -> `generate()`
-> read the `SyncReport`" was independently reconstructed in three places:
`app/main.py` (`_auto_generate_plex_library`, boot + scheduled pipeline),
`app/api/plex.py` (`POST /api/plex/generate`) and `app/cli.py` (`generate` command).
Worse, `app/api/sync.py`'s `/full-pipeline` endpoint imported `app.main`'s private
`_auto_generate_plex_library` coroutine directly — a router reaching into the
application entrypoint (layering inversion + latent circular-import trap).

All four call sites now go through the two functions below instead:

- `generate_plex_library(...)` — the raw "just do it" building block. Callers
  that already know exactly what they want (an HTTP request with a validated
  `outputDir`, or an operator-invoked CLI run) call this directly.
- `generate_plex_library_auto()` — the unattended-trigger wrapper (boot,
  scheduled pipeline, and the `/full-pipeline` endpoint): resolves
  `settings.PLEX_LIBRARY_DIR`, skips gracefully (log only, never raises) if it
  isn't configured or if there are currently no active Xtream accounts (an
  empty `DatabaseSource` would otherwise make the generator treat every
  previously generated item as deleted — a destructive false-empty run), then
  delegates to `generate_plex_library` and logs the resulting report.

Neither function makes an auth/confinement decision — that stays with the
caller:
  - `app/api/plex.py` resolves + confines the client-supplied `outputDir`
    (CR-S01) BEFORE calling `generate_plex_library`, and passes the already
    validated `Path`.
  - `app/main.py` / `app/api/sync.py` call `generate_plex_library_auto()`,
    which only ever resolves the operator-configured `settings.PLEX_LIBRARY_DIR`
    — never a client-supplied path.
  - `app/cli.py` passes the operator-supplied `--output` path directly
    (trusted local operator invocation, no HTTP boundary).
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.config import settings
from app.plex_generator.generator import PlexLibraryGenerator
from app.plex_generator.models import SyncReport
from app.plex_generator.source import DatabaseSource
from app.plex_generator.storage import DryRunStorage, LocalStorage

logger = logging.getLogger("plexhub.services.plex_generation")


async def generate_plex_library(
    account_ids: list[str] | None = None,
    output_dir: Path | str | None = None,
    strm_only: bool = False,
    dry_run: bool = False,
) -> SyncReport:
    """Generate (or dry-run) the unified Plex/Jellyfin library tree.

    ``account_ids=None`` aggregates across ALL active Xtream accounts (the
    unified, deduped tree); a list restricts aggregation to those accounts.
    ``output_dir=None`` falls back to ``settings.PLEX_LIBRARY_DIR`` — raises
    ``ValueError`` if that isn't configured either. Callers that need a
    friendlier response (e.g. the HTTP 400 in `app/api/plex.py`) must resolve
    their own output directory and pass it in rather than relying on this
    fallback.
    """
    resolved_output = (
        Path(output_dir) if output_dir is not None
        else (Path(settings.PLEX_LIBRARY_DIR) if settings.PLEX_LIBRARY_DIR else None)
    )
    if resolved_output is None:
        raise ValueError("output_dir is required (or set PLEX_LIBRARY_DIR in .env)")

    storage = DryRunStorage() if dry_run else LocalStorage(resolved_output)
    source = DatabaseSource(account_ids)
    generator = PlexLibraryGenerator(source, storage, resolved_output, strm_only)
    return await generator.generate()


async def generate_plex_library_auto() -> SyncReport | None:
    """Best-effort wrapper for the unattended trigger (boot, scheduled
    pipeline, and the `/api/sync/full-pipeline` endpoint).

    Returns the `SyncReport` on success, or `None` if generation was skipped
    (not configured / no active accounts) or failed (exception is logged, not
    raised — mirrors the previous inline behaviour in `app.main`).
    """
    if not settings.PLEX_LIBRARY_DIR:
        logger.info("PLEX_LIBRARY_DIR not set — skipping Plex library generation")
        return None

    from sqlalchemy import select
    from app.db.database import async_session_factory
    from app.models.database import XtreamAccount

    output = Path(settings.PLEX_LIBRARY_DIR)

    async with async_session_factory() as db:
        result = await db.execute(
            select(XtreamAccount.id).where(XtreamAccount.is_active == True)
        )
        account_ids = [row[0] for row in result]

    if not account_ids:
        logger.warning("No active accounts — skipping Plex library generation")
        return None

    # Unified library: one flat tree deduped across ALL active accounts (the same
    # movie/series from several panels = one folder + one NFO + multiple versions).
    logger.info(
        f"Auto-generating unified Plex library across {len(account_ids)} account(s)"
    )
    try:
        report = await generate_plex_library(output_dir=output)
        logger.info(
            f"Plex generation: {report.created} created, {report.updated} updated, "
            f"{report.deleted} deleted, {report.unchanged} unchanged, "
            f"{report.pruned} pruned"
        )
        if settings.DAV_ENABLED:
            # Ticket DAV-2: the WebDAV virtual tree (app/dav/vfs.py) is built
            # from the same DB state a `.strm` generation pass just read —
            # invalidate its TTL cache so the NEXT `/dav` request rebuilds it
            # instead of serving a stale tree for up to DAV_TREE_TTL_MINUTES.
            # Deferred import: mirrors this module's own
            # `from app.db.database import async_session_factory` above —
            # `app.dav.vfs` isn't otherwise a dependency of this module, and
            # importing it at module scope would need to run even when
            # DAV_ENABLED is false.
            from app.dav.vfs import dav_tree_cache

            dav_tree_cache.invalidate()
        return report
    except Exception as e:
        logger.error(f"Plex generation failed: {e}", exc_info=True)
        return None
