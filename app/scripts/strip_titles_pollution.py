"""One-shot migration: strip country prefixes and quality suffixes from existing media.

Renames folders and files on disk (preserving .nfo and .jpg already present),
updates the DB (Media + EnrichmentQueue) and .plex_mapping.json.

Usage:
    python -m app.scripts.strip_titles_pollution [--dry-run] [--account-id ID]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import EnrichmentQueue, Media, XtreamAccount
from app.plex_generator.mapping import MappingStore
from app.plex_generator.naming import _movie_folder, _series_folder
from app.utils.server_id import build_server_id
from app.utils.string_normalizer import (
    normalize_for_sorting, parse_title_and_year, parse_title_year_and_suffix,
)
from app.utils.time import now_ms


def _short_id(rating_key: str, length: int = 6) -> str:
    """Match generator._short_id so migration produces the same disambiguator
    as fresh runs would."""
    safe = rating_key or ""
    for ext in (".mkv", ".mp4", ".avi", ".ts"):
        if safe.endswith(ext):
            safe = safe[: -len(ext)]
            break
    return safe[:length] or "x"

logger = logging.getLogger("plexhub.scripts.strip_titles")


@dataclass
class FolderRename:
    """A planned folder rename operation."""
    rating_key: str
    server_id: str
    media_type: str  # 'movie' or 'show'
    old_title: str
    new_title: str
    new_year: int | None
    old_folder: str
    new_folder: str
    old_rel_dir: str  # e.g. "Films/FR - Foo (2020)"
    new_rel_dir: str  # e.g. "Films/Foo (2020)"


@dataclass
class MigrationReport:
    movies_renamed: int = 0
    series_renamed: int = 0
    episode_files_renamed: int = 0
    db_rows_updated: int = 0
    mapping_entries_updated: int = 0
    enrichment_queue_reset: int = 0
    conflicts_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "movies_renamed", "series_renamed", "episode_files_renamed",
            "db_rows_updated", "mapping_entries_updated", "enrichment_queue_reset",
            "conflicts_skipped", "errors",
        )}


def _build_movie_rename(
    rating_key: str, server_id: str, old_title: str, db_year: int | None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> FolderRename | None:
    new_title, parsed_year, parsed_suffix = parse_title_year_and_suffix(old_title or "")
    if new_title == "Unknown":
        return None
    effective_year = db_year if db_year is not None else parsed_year
    # Caller may pre-decide suffix/fallback after collision resolution; otherwise
    # default to None (singleton = canonical Jellyfin name).
    old_folder = _movie_folder(old_title, db_year)
    new_folder = _movie_folder(new_title, effective_year, suffix, fallback_id)
    if old_folder == new_folder:
        return None
    return FolderRename(
        rating_key=rating_key, server_id=server_id, media_type="movie",
        old_title=old_title, new_title=new_title, new_year=effective_year,
        old_folder=old_folder, new_folder=new_folder,
        old_rel_dir=f"Films/{old_folder}", new_rel_dir=f"Films/{new_folder}",
    )


def _build_series_rename(
    rating_key: str, server_id: str, old_title: str,
    db_year: int | None = None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> FolderRename | None:
    new_title, parsed_year, parsed_suffix = parse_title_year_and_suffix(old_title or "")
    if new_title == "Unknown":
        return None
    effective_year = db_year if db_year is not None else parsed_year
    old_folder = _series_folder(old_title)
    new_folder = _series_folder(new_title, effective_year, suffix, fallback_id)
    if old_folder == new_folder:
        return None
    return FolderRename(
        rating_key=rating_key, server_id=server_id, media_type="show",
        old_title=old_title, new_title=new_title, new_year=effective_year,
        old_folder=old_folder, new_folder=new_folder,
        old_rel_dir=f"Series/{old_folder}", new_rel_dir=f"Series/{new_folder}",
    )


def _resolve_disambiguators(
    items: list[tuple[str, str, int | None]],
) -> dict[str, tuple[str | None, str | None]]:
    """Decide suffix / fallback_id per rating_key based on (clean, year) collisions.

    `items` = list of (rating_key, raw_title, db_year). Returns
    {rating_key: (suffix, fallback_id)}.
    """
    parsed: dict[str, tuple[str, int | None, str | None]] = {}
    groups: dict[tuple[str, int | None], list[str]] = {}
    for rating_key, raw_title, db_year in items:
        clean, parsed_year, suffix = parse_title_year_and_suffix(raw_title or "")
        year = db_year if db_year is not None else parsed_year
        parsed[rating_key] = (clean, year, suffix)
        groups.setdefault((clean, year), []).append(rating_key)

    decisions: dict[str, tuple[str | None, str | None]] = {}
    for (_clean, _year), keys in groups.items():
        if len(keys) == 1:
            decisions[keys[0]] = (None, None)
            continue
        suffix_buckets: dict[str | None, list[str]] = {}
        for rk in keys:
            _, _, suffix = parsed[rk]
            suffix_buckets.setdefault(suffix, []).append(rk)
        for suffix, sids in suffix_buckets.items():
            if len(sids) == 1:
                decisions[sids[0]] = (suffix, None)
            else:
                for sid in sids:
                    decisions[sid] = (suffix, _short_id(sid))
    return decisions


def _rename_path(src: Path, dst: Path) -> None:
    """Rename src to dst. Falls back to shutil.move if cross-volume."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:
        shutil.move(str(src), str(dst))


def _has_conflict(old_dir: Path, new_dir: Path) -> bool:
    """True iff new_dir already exists as a distinct directory from old_dir."""
    if not new_dir.exists():
        return False
    if not old_dir.exists():
        return False
    try:
        return new_dir.resolve() != old_dir.resolve()
    except OSError:
        return True


def apply_movie_rename(rename: FolderRename, account_dir: Path, report: MigrationReport) -> None:
    """Rename Films/old_folder/ -> Films/new_folder/, and the .strm file inside."""
    old_dir = account_dir / rename.old_rel_dir
    new_dir = account_dir / rename.new_rel_dir

    if not old_dir.exists():
        return  # Nothing on disk; DB-only update will be handled later.

    if _has_conflict(old_dir, new_dir):
        logger.warning(f"Conflict: {new_dir} already exists, skipping {old_dir}")
        report.conflicts_skipped += 1
        return

    _rename_path(old_dir, new_dir)

    old_strm = new_dir / f"{rename.old_folder}.strm"
    new_strm = new_dir / f"{rename.new_folder}.strm"
    if old_strm.exists() and old_strm != new_strm:
        _rename_path(old_strm, new_strm)
    report.movies_renamed += 1
    logger.info(f"Renamed movie folder: {rename.old_folder} -> {rename.new_folder}")


def apply_series_rename(rename: FolderRename, account_dir: Path, report: MigrationReport) -> None:
    """Rename Series/old_folder/ and every episode .strm/.nfo inside Season XX/ subfolders."""
    old_dir = account_dir / rename.old_rel_dir
    new_dir = account_dir / rename.new_rel_dir

    if not old_dir.exists():
        return

    if _has_conflict(old_dir, new_dir):
        logger.warning(f"Conflict: {new_dir} already exists, skipping {old_dir}")
        report.conflicts_skipped += 1
        return

    _rename_path(old_dir, new_dir)

    old_prefix = rename.old_folder
    new_prefix = rename.new_folder
    for season_dir in new_dir.iterdir():
        if not season_dir.is_dir():
            continue
        for f in season_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name
            if name.startswith(old_prefix + " S") and (name.endswith(".strm") or name.endswith(".nfo")):
                new_name = new_prefix + name[len(old_prefix):]
                _rename_path(f, season_dir / new_name)
                report.episode_files_renamed += 1
    report.series_renamed += 1
    logger.info(f"Renamed series folder: {rename.old_folder} -> {rename.new_folder}")


def update_mapping_paths(
    mapping: MappingStore, renames: list[FolderRename], report: MigrationReport,
) -> None:
    """Rewrite path entries in .plex_mapping.json affected by renames."""
    if not renames:
        return
    pairs = sorted(
        [(r.old_rel_dir + "/", r.new_rel_dir + "/", r.old_folder, r.new_folder) for r in renames],
        key=lambda p: -len(p[0]),
    )
    for source_id, entry in list(mapping._data.items()):
        for old_p, new_p, old_folder, new_folder in pairs:
            if not entry.path.startswith(old_p):
                continue
            rebuilt = new_p + entry.path[len(old_p):]
            # Movie .strm files: ".../New/Old.strm" -> ".../New/New.strm"
            if old_folder != new_folder:
                rebuilt = rebuilt.replace(f"/{old_folder}.strm", f"/{new_folder}.strm")
                # Episode files embed series folder in their basename
                rebuilt = rebuilt.replace(f"/{old_folder} S", f"/{new_folder} S")
            mapping._data[source_id].path = rebuilt
            report.mapping_entries_updated += 1
            break


async def _collect_renames_for_account(db, account_id: str) -> list[FolderRename]:
    server_id = build_server_id(account_id)
    renames: list[FolderRename] = []

    # Movies — resolve collisions globally first, then plan each rename with
    # its decided suffix/fallback.
    result = await db.execute(
        select(Media.rating_key, Media.title, Media.year)
        .where(Media.server_id == server_id)
        .where(Media.type == "movie")
    )
    movie_rows = list(result.all())
    movie_decisions = _resolve_disambiguators(
        [(rk, title, year) for rk, title, year in movie_rows]
    )
    for rating_key, title, year in movie_rows:
        suffix, fallback = movie_decisions.get(rating_key, (None, None))
        r = _build_movie_rename(rating_key, server_id, title, year, suffix, fallback)
        if r:
            renames.append(r)

    # Series — same global pass.
    result = await db.execute(
        select(Media.rating_key, Media.title, Media.year)
        .where(Media.server_id == server_id)
        .where(Media.type == "show")
    )
    show_rows = list(result.all())
    show_decisions = _resolve_disambiguators(
        [(rk, title, year) for rk, title, year in show_rows]
    )
    for rating_key, title, year in show_rows:
        suffix, fallback = show_decisions.get(rating_key, (None, None))
        r = _build_series_rename(rating_key, server_id, title, year, suffix, fallback)
        if r:
            renames.append(r)

    return renames


async def _update_db_for_account(
    db, account_id: str, dry_run: bool, report: MigrationReport,
) -> list[str]:
    """Update Media titles. Returns rating_keys with cleaned titles for queue reset."""
    server_id = build_server_id(account_id)
    cleaned: list[str] = []

    result = await db.execute(
        select(Media.rating_key, Media.title, Media.year, Media.type)
        .where(Media.server_id == server_id)
        .where(Media.type.in_(("movie", "show")))
    )
    # Use the suffix-aware parser so we strip "(US)", "(HD)" etc. from the
    # stored title — they remain encoded on disk via the folder name when
    # needed for collision disambiguation.
    for rating_key, title, year, mtype in result.all():
        new_title, parsed_year, _suffix = parse_title_year_and_suffix(title or "")
        if new_title == "Unknown" or new_title == (title or ""):
            continue
        new_year = year if year is not None else parsed_year
        cleaned.append(rating_key)
        if dry_run:
            logger.info(f"[DRY-RUN] DB {mtype} {rating_key}: '{title}' -> '{new_title}' (year={new_year})")
            continue
        await db.execute(
            update(Media)
            .where(Media.rating_key == rating_key, Media.server_id == server_id)
            .values(
                title=new_title,
                title_sortable=normalize_for_sorting(new_title).lower(),
                year=new_year,
                updated_at=now_ms(),
            )
        )
        report.db_rows_updated += 1

    # Episodes: fix grandparent_title
    # Episodes: clean title, parent_title (season label) and grandparent_title.
    # Some IPTV providers prefix every episode title with "FR - " too — leaving
    # these polluted hides the dirt in Plex/Jellyfin even after the show folder
    # is renamed.
    result = await db.execute(
        select(
            Media.rating_key, Media.title, Media.parent_title,
            Media.grandparent_title,
        )
        .where(Media.server_id == server_id)
        .where(Media.type == "episode")
    )

    def _clean(value: str | None) -> str | None:
        """Return cleaned value if it actually changed, else None."""
        if not value:
            return None
        new, _, _ = parse_title_year_and_suffix(value)
        if new == "Unknown" or new == value:
            return None
        return new

    for rating_key, title, parent_title, gp_title in result.all():
        new_title = _clean(title)
        new_parent = _clean(parent_title)
        new_gp = _clean(gp_title)
        if not (new_title or new_parent or new_gp):
            continue

        if dry_run:
            if new_title:
                logger.info(f"[DRY-RUN] DB episode {rating_key}: title '{title}' -> '{new_title}'")
            if new_parent:
                logger.info(f"[DRY-RUN] DB episode {rating_key}: parent '{parent_title}' -> '{new_parent}'")
            if new_gp:
                logger.info(f"[DRY-RUN] DB episode {rating_key}: gp '{gp_title}' -> '{new_gp}'")
            continue

        values: dict = {"updated_at": now_ms()}
        if new_title:
            values["title"] = new_title
            # title_sortable is also indexed; keep it consistent with title.
            values["title_sortable"] = normalize_for_sorting(new_title).lower()
        if new_parent:
            values["parent_title"] = new_parent
        if new_gp:
            values["grandparent_title"] = new_gp

        await db.execute(
            update(Media)
            .where(Media.rating_key == rating_key, Media.server_id == server_id)
            .values(**values)
        )
        report.db_rows_updated += 1

    return cleaned


async def _reset_enrichment_queue(
    db, server_id: str, rating_keys: list[str], dry_run: bool, report: MigrationReport,
) -> None:
    """Reset/insert EnrichmentQueue rows to 'pending' for cleaned media that still lack
    both TMDB and IMDB IDs — so the next enrichment cycle re-queries TMDB with clean titles."""
    if not rating_keys:
        return

    rows = await db.execute(
        select(Media.rating_key, Media.type, Media.title, Media.year, Media.tmdb_id, Media.imdb_id)
        .where(Media.server_id == server_id)
        .where(Media.rating_key.in_(rating_keys))
    )
    targets: list[dict] = []
    ts = now_ms()
    for rating_key, mtype, title, year, tmdb_id, imdb_id in rows.all():
        if mtype not in ("movie", "show"):
            continue
        if tmdb_id and imdb_id:
            continue
        targets.append({
            "rating_key": rating_key,
            "server_id": server_id,
            "media_type": mtype,
            "title": title,
            "year": year,
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "created_at": ts,
            "existing_tmdb_id": tmdb_id,
            "existing_imdb_id": imdb_id,
        })

    if not targets:
        return

    if dry_run:
        logger.info(f"[DRY-RUN] Would reset/insert EnrichmentQueue for {len(targets)} media")
        return

    for i in range(0, len(targets), 200):
        chunk = targets[i:i + 200]
        stmt = sqlite_upsert(EnrichmentQueue).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["rating_key", "server_id"],
            set_={
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "title": stmt.excluded.title,
                "year": stmt.excluded.year,
            },
        )
        await db.execute(stmt)
        report.enrichment_queue_reset += len(chunk)
    logger.info(f"Reset/queued {len(targets)} EnrichmentQueue entries")


async def run_migration_for_account(
    account_id: str, library_dir: Path, dry_run: bool,
) -> MigrationReport:
    report = MigrationReport()
    server_id = build_server_id(account_id)
    account_dir = library_dir / account_id

    async with async_session_factory() as db:
        try:
            renames = await _collect_renames_for_account(db, account_id)
            logger.info(
                f"[{account_id}] Planned: "
                f"{sum(1 for r in renames if r.media_type == 'movie')} movies, "
                f"{sum(1 for r in renames if r.media_type == 'show')} series"
            )

            for r in renames:
                if dry_run:
                    logger.info(f"[DRY-RUN] FS rename: {r.old_rel_dir} -> {r.new_rel_dir}")
                    continue
                try:
                    if r.media_type == "movie":
                        apply_movie_rename(r, account_dir, report)
                    else:
                        apply_series_rename(r, account_dir, report)
                except Exception as e:
                    msg = f"Rename failed {r.old_rel_dir} -> {r.new_rel_dir}: {e}"
                    logger.error(msg, exc_info=True)
                    report.errors.append(msg)

            if renames and account_dir.exists():
                mapping = MappingStore(account_dir)
                mapping.load()
                update_mapping_paths(mapping, renames, report)
                if not dry_run and report.mapping_entries_updated:
                    mapping.save()
                elif dry_run:
                    logger.info(f"[DRY-RUN] Would update {report.mapping_entries_updated} mapping entries")

            cleaned = await _update_db_for_account(db, account_id, dry_run, report)
            await _reset_enrichment_queue(db, server_id, cleaned, dry_run, report)

            if not dry_run:
                await db.commit()
        except Exception:
            await db.rollback()
            raise

    return report


async def run_migration(
    account_id: str | None = None, dry_run: bool = False,
) -> dict[str, MigrationReport]:
    """Run migration for one account (if specified) or all active accounts."""
    if not settings.PLEX_LIBRARY_DIR:
        raise RuntimeError("PLEX_LIBRARY_DIR is not set")
    library_dir = Path(settings.PLEX_LIBRARY_DIR)

    async with async_session_factory() as db:
        if account_id:
            account_ids = [account_id]
        else:
            result = await db.execute(
                select(XtreamAccount.id).where(XtreamAccount.is_active.is_(True))
            )
            account_ids = [row[0] for row in result.all()]

    reports: dict[str, MigrationReport] = {}
    for aid in account_ids:
        logger.info(f"=== Migrating account {aid} (dry_run={dry_run}) ===")
        try:
            reports[aid] = await run_migration_for_account(aid, library_dir, dry_run)
            logger.info(f"[{aid}] Report: {reports[aid].as_dict()}")
        except Exception as e:
            logger.error(f"Migration failed for {aid}: {e}", exc_info=True)
            r = MigrationReport()
            r.errors.append(str(e))
            reports[aid] = r
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Log actions without writing")
    parser.add_argument("--account-id", help="Limit to a single account ID")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    reports = asyncio.run(run_migration(args.account_id, args.dry_run))
    for aid, report in reports.items():
        print(f"[{aid}] {report.as_dict()}")


if __name__ == "__main__":
    main()
