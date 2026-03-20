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
    from app.plex_generator.generator import PlexLibraryGenerator
    from app.plex_generator.source import DatabaseSource
    from app.plex_generator.storage import LocalStorage, DryRunStorage

    await init_db()

    storage = DryRunStorage() if dry_run else LocalStorage(output)

    if all_accounts:
        from sqlalchemy import select
        from app.db.database import async_session_factory
        from app.models.database import XtreamAccount

        async with async_session_factory() as db:
            result = await db.execute(
                select(XtreamAccount.id).where(XtreamAccount.is_active == True)
            )
            account_ids = [row[0] for row in result]

        if not account_ids:
            logger.error("No active accounts found")
            raise typer.Exit(code=1)

        logger.info(f"Generating Plex library for {len(account_ids)} accounts")
        for aid in account_ids:
            source = DatabaseSource(aid)
            gen = PlexLibraryGenerator(source, storage, output, strm_only)
            report = await gen.generate()
            _print_report(aid, report)
    else:
        source = DatabaseSource(account_id)
        gen = PlexLibraryGenerator(source, storage, output, strm_only)
        report = await gen.generate()
        _print_report(account_id, report)


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
