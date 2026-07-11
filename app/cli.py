"""CLI for PlexHub Plex library generation.

Usage:
    python -m app.cli generate --account-id <ID> --output ./Media
    python -m app.cli generate --all --output ./Media
    python -m app.cli generate --account-id <ID> --output ./Media --dry-run
    python -m app.cli generate --account-id <ID> --output ./Media --strm-only
"""

import asyncio
import logging
import sys
from pathlib import Path

import typer

app = typer.Typer(help="PlexHub - Plex library generator from Xtream VOD")

# Configure logging for CLI usage
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("plexhub.cli")


async def _run_generate(
    account_id: str | None,
    all_accounts: bool,
    output: Path,
    dry_run: bool,
    strm_only: bool,
) -> None:
    from app.db.database import init_db
    # CR-A02: generation wiring shared with app/api/plex.py and app/main.py
    # instead of being reconstructed inline here.
    from app.services.plex_generation_service import generate_plex_library

    await init_db()

    # Unified library: one flat, deduped tree. `--account-id` restricts the
    # aggregation to one account; `--all` (or neither) merges every active one.
    account_ids = [account_id] if account_id and not all_accounts else None
    report = await generate_plex_library(
        account_ids=account_ids,
        output_dir=output,
        strm_only=strm_only,
        dry_run=dry_run,
    )
    _print_report("all" if account_ids is None else account_id, report)


async def _run_recompute_unification(dry_run: bool) -> tuple[int, int]:
    """Recompute media.unification_id from current ids; returns (scanned, fixed)."""
    from sqlalchemy import select, update

    from app.db.database import init_db, async_session_factory
    from app.models.database import Media
    from app.utils.db_retry import commit_with_retry
    from app.utils.unification import (
        calculate_unification_id,
        calculate_history_group_key,
    )

    await init_db()
    scanned = fixed = 0
    async with async_session_factory() as db:
        result = await db.execute(
            select(
                Media.rating_key, Media.server_id, Media.title, Media.year,
                Media.imdb_id, Media.tmdb_id, Media.unification_id,
            )
            .where(Media.type.in_(("movie", "show")))
            .distinct()
        )
        pending = 0
        for rk, sid, title, year, imdb, tmdb, old_unif in result.all():
            scanned += 1
            new_unif = calculate_unification_id(title or "", year, imdb, tmdb)
            if not new_unif or new_unif == (old_unif or ""):
                continue
            fixed += 1
            if dry_run:
                continue
            await db.execute(
                update(Media)
                .where(Media.rating_key == rk, Media.server_id == sid)
                .values(
                    unification_id=new_unif,
                    history_group_key=calculate_history_group_key(new_unif, rk, sid),
                )
            )
            pending += 1
            if pending >= 500:
                await commit_with_retry(db)
                pending = 0
        if not dry_run and pending:
            await commit_with_retry(db)
    return scanned, fixed


@app.command(name="recompute-unification")
def recompute_unification(
    dry_run: bool = typer.Option(False, "--dry-run", help="Count changes without writing"),
) -> None:
    """Recompute media.unification_id from current imdb_id/tmdb_id/title/year.

    Fixes library entries that split into separate folders (e.g. "[imdb]" vs
    "[title_]") when an id was set out-of-band (e.g. a .nfo import) without
    refreshing the unification key the Plex generator groups by. Run, then
    regenerate the library.
    """
    mode = "DRY-RUN" if dry_run else "LIVE"
    typer.echo(f"Recompute unification_id [{mode}]")
    scanned, fixed = asyncio.run(_run_recompute_unification(dry_run))
    typer.echo(f"  Scanned: {scanned}")
    typer.echo(f"  {'Would update' if dry_run else 'Updated'}: {fixed}")


def _print_report(account_id: str, report) -> None:
    typer.echo(f"\n--- Sync Report (account: {account_id}) ---")
    typer.echo(f"  Created:   {report.created}")
    typer.echo(f"  Updated:   {report.updated}")
    typer.echo(f"  Deleted:   {report.deleted}")
    typer.echo(f"  Unchanged: {report.unchanged}")
    typer.echo(f"  Errors:    {len(report.errors)}")
    typer.echo(f"  Duration:  {report.duration_seconds}s")
    if report.errors:
        typer.echo("  Error details:")
        for err in report.errors[:10]:
            typer.echo(f"    - {err}")
        if len(report.errors) > 10:
            typer.echo(f"    ... and {len(report.errors) - 10} more")


@app.command()
def generate(
    account_id: str = typer.Option(None, "--account-id", "-a", help="Xtream account ID to generate for"),
    all_accounts: bool = typer.Option(False, "--all", help="Generate for all active accounts"),
    output: Path = typer.Option(..., "--output", "-o", help="Output directory for Plex library"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without writing files"),
    strm_only: bool = typer.Option(False, "--strm-only", help="Generate .strm files only (no NFO, no images)"),
) -> None:
    """Generate a Plex-compatible library from synced Xtream VOD data."""
    if not account_id and not all_accounts:
        typer.echo("Error: provide --account-id or --all", err=True)
        raise typer.Exit(code=1)

    if not output:
        typer.echo("Error: --output is required", err=True)
        raise typer.Exit(code=1)

    mode = "DRY-RUN" if dry_run else "LIVE"
    typer.echo(f"Plex library generation [{mode}] -> {output}")

    asyncio.run(_run_generate(account_id, all_accounts, output, dry_run, strm_only))


if __name__ == "__main__":
    app()
