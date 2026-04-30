"""Import IMDb / TMDB IDs from tinyMediaManager .nfo files into the media table.

Reconciliation is fully deterministic — no fuzzy matching, no title comparison.

For each active Xtream account we read the per-account mapping that
plex_generator already maintains (`<root>/<account_id>/.plex_mapping.json`).
That JSON is keyed by `rating_key` and stores the relative path of every
generated `.strm`. The companion `movie.nfo` lives in the same folder, so we
join on `rating_key` directly:

    movies: Films/<folder>/<folder>.strm
            -> Films/<folder>/movie.nfo
            -> UPDATE media WHERE rating_key=<key> AND server_id=<server_id>

For shows the mapping only contains episodes, but `tvshow.nfo` is a single
file at the series root. We iterate `Media WHERE type='show'` for the account
and compute the expected NFO path with the same `series_nfo_path()` helper
the generator uses. If the file exists we read it and update the row.

Only `imdb_id` and `tmdb_id` are written. By default we only fill missing
values (`overwrite=True` to replace existing ones). Caller commits via
Depends(get_db); when dry_run=True nothing is written.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Media, XtreamAccount
from app.plex_generator.mapping import MappingStore
from app.plex_generator.naming import series_nfo_path
from app.utils.server_id import build_server_id
from app.utils.time import now_ms


logger = logging.getLogger("plexhub.nfo_import")

_IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
_TMDB_ID_RE = re.compile(r"^\d{1,9}$")


@dataclass
class NfoEntry:
    path: Path
    folder_name: str
    media_type: str  # "movie" or "show"
    nfo_year: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None


@dataclass
class ImportReport:
    account_id: str
    media_type: str
    scanned: int = 0
    matched: int = 0
    written: int = 0
    skipped_no_change: int = 0
    skipped_id_already_set: int = 0
    unmatched: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    overwrite: bool = False
    # Kept for template compatibility; deterministic matching never produces ambiguous hits.
    ambiguous: list[str] = field(default_factory=list)


def _first_text(root: ET.Element, *tags: str) -> Optional[str]:
    for tag in tags:
        el = root.find(tag)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return None


def _uniqueid(root: ET.Element, type_: str) -> Optional[str]:
    for el in root.findall("uniqueid"):
        if (el.get("type") or "").lower() == type_ and el.text and el.text.strip():
            return el.text.strip()
    return None


def _validate_imdb(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    return value if _IMDB_ID_RE.match(value) else None


def _validate_tmdb(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    return value if _TMDB_ID_RE.match(value) else None


def parse_nfo_file(path: Path, media_type: str) -> Optional[NfoEntry]:
    """Parse a movie.nfo / tvshow.nfo. Returns None on hard failure."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as exc:
        logger.warning("NFO parse error %s: %s", path, exc)
        return None
    except OSError as exc:
        logger.warning("NFO read error %s: %s", path, exc)
        return None

    imdb = (
        _validate_imdb(_uniqueid(root, "imdb"))
        or _validate_imdb(_first_text(root, "imdbid", "imdb_id"))
    )
    tmdb = (
        _validate_tmdb(_uniqueid(root, "tmdb"))
        or _validate_tmdb(_first_text(root, "tmdbid", "tmdb_id"))
    )

    year_raw = _first_text(root, "year")
    nfo_year: Optional[int] = None
    if year_raw:
        try:
            nfo_year = int(year_raw)
        except ValueError:
            nfo_year = None

    return NfoEntry(
        path=path,
        folder_name=path.parent.name,
        media_type=media_type,
        nfo_year=nfo_year,
        imdb_id=imdb,
        tmdb_id=tmdb,
    )


def _compute_updates(row: Media, parsed: NfoEntry, overwrite: bool) -> dict:
    updates: dict[str, str] = {}
    if parsed.imdb_id and (overwrite or not row.imdb_id):
        if parsed.imdb_id != row.imdb_id:
            updates["imdb_id"] = parsed.imdb_id
    if parsed.tmdb_id and (overwrite or not row.tmdb_id):
        if parsed.tmdb_id != row.tmdb_id:
            updates["tmdb_id"] = parsed.tmdb_id
    return updates


def _classify_no_update(
    row: Media, parsed: NfoEntry, overwrite: bool, report: ImportReport,
) -> None:
    if (parsed.imdb_id and row.imdb_id and not overwrite) or (
        parsed.tmdb_id and row.tmdb_id and not overwrite
    ):
        report.skipped_id_already_set += 1
    else:
        report.skipped_no_change += 1


async def _import_movies_for_account(
    db: AsyncSession,
    account: XtreamAccount,
    account_root: Path,
    server_id: str,
    overwrite: bool,
    dry_run: bool,
) -> ImportReport:
    report = ImportReport(
        account_id=account.id, media_type="movie",
        overwrite=overwrite, dry_run=dry_run,
    )

    mapping = MappingStore(account_root)
    mapping.load()
    # MappingStore exposes no public iteration; access internal dict directly.
    movie_entries = [
        (rk, entry) for rk, entry in mapping._data.items()
        if entry.path.startswith("Films/")
    ]
    logger.info(
        "NFO movies account=%s: %d film entries in mapping",
        account.id, len(movie_entries),
    )

    for rating_key, entry in movie_entries:
        strm_full = account_root / entry.path
        nfo_path = strm_full.parent / "movie.nfo"
        folder_name = strm_full.parent.name

        if not nfo_path.exists():
            report.unmatched.append(f"{folder_name}: missing movie.nfo")
            continue
        report.scanned += 1

        parsed = parse_nfo_file(nfo_path, "movie")
        if parsed is None:
            report.parse_errors.append(folder_name)
            continue
        if not parsed.imdb_id and not parsed.tmdb_id:
            report.unmatched.append(f"{folder_name}: NFO has no imdb/tmdb")
            continue

        row = (await db.execute(
            select(Media).where(
                Media.rating_key == rating_key,
                Media.server_id == server_id,
                Media.type == "movie",
            ).limit(1)
        )).scalars().first()
        if row is None:
            report.unmatched.append(
                f"{folder_name}: no DB row for rating_key={rating_key}"
            )
            continue

        report.matched += 1
        updates = _compute_updates(row, parsed, overwrite)
        if not updates:
            _classify_no_update(row, parsed, overwrite, report)
            continue
        if dry_run:
            report.written += 1
            continue

        await db.execute(
            update(Media)
            .where(
                Media.rating_key == row.rating_key,
                Media.server_id == row.server_id,
            )
            .values(**updates, updated_at=now_ms())
        )
        report.written += 1

    if not dry_run and report.written:
        await db.flush()
    logger.info(
        "NFO movies account=%s done: matched=%d written=%d unmatched=%d "
        "skipped_already=%d skipped_nochange=%d",
        account.id, report.matched, report.written,
        len(report.unmatched),
        report.skipped_id_already_set, report.skipped_no_change,
    )
    return report


async def _import_shows_for_account(
    db: AsyncSession,
    account: XtreamAccount,
    account_root: Path,
    server_id: str,
    overwrite: bool,
    dry_run: bool,
) -> ImportReport:
    report = ImportReport(
        account_id=account.id, media_type="show",
        overwrite=overwrite, dry_run=dry_run,
    )

    shows = list((await db.execute(
        select(Media).where(
            Media.type == "show", Media.server_id == server_id,
        )
    )).scalars().all())
    # The same show can appear under several (filter, sort_order) variants —
    # de-dupe on rating_key so we don't process / log the NFO multiple times.
    seen: set[str] = set()
    unique_shows: list[Media] = []
    for show in shows:
        if show.rating_key in seen:
            continue
        seen.add(show.rating_key)
        unique_shows.append(show)
    logger.info(
        "NFO shows account=%s: %d unique shows in DB",
        account.id, len(unique_shows),
    )

    for show in unique_shows:
        nfo_rel = series_nfo_path(show.title)  # "Series/<safe_title>/tvshow.nfo"
        nfo_path = account_root / nfo_rel
        if not nfo_path.exists():
            report.unmatched.append(f"{show.title}: {nfo_rel} not found")
            continue
        report.scanned += 1

        parsed = parse_nfo_file(nfo_path, "show")
        if parsed is None:
            report.parse_errors.append(show.title)
            continue
        if not parsed.imdb_id and not parsed.tmdb_id:
            report.unmatched.append(f"{show.title}: NFO has no imdb/tmdb")
            continue

        report.matched += 1
        updates = _compute_updates(show, parsed, overwrite)
        if not updates:
            _classify_no_update(show, parsed, overwrite, report)
            continue
        if dry_run:
            report.written += 1
            continue

        await db.execute(
            update(Media)
            .where(
                Media.rating_key == show.rating_key,
                Media.server_id == show.server_id,
            )
            .values(**updates, updated_at=now_ms())
        )
        report.written += 1

    if not dry_run and report.written:
        await db.flush()
    logger.info(
        "NFO shows account=%s done: matched=%d written=%d unmatched=%d "
        "skipped_already=%d skipped_nochange=%d",
        account.id, report.matched, report.written,
        len(report.unmatched),
        report.skipped_id_already_set, report.skipped_no_change,
    )
    return report


async def import_nfo(
    db: AsyncSession,
    root: Path,
    *,
    kinds: tuple[str, ...] = ("movies", "shows"),
    overwrite: bool = False,
    dry_run: bool = False,
    account_ids: Optional[tuple[str, ...]] = None,
) -> list[ImportReport]:
    """Run a deterministic NFO import for every active Xtream account.

    Returns one ImportReport per (account, kind).
    """
    logger.info(
        "NFO import start: root=%s kinds=%s overwrite=%s dry_run=%s account_filter=%s",
        root, kinds, overwrite, dry_run, account_ids,
    )

    accounts_q = await db.execute(
        select(XtreamAccount).where(XtreamAccount.is_active == True)  # noqa: E712
    )
    accounts: list[XtreamAccount] = list(accounts_q.scalars().all())
    if account_ids is not None:
        wanted = set(account_ids)
        accounts = [a for a in accounts if a.id in wanted]
    logger.info("NFO import: %d active account(s)", len(accounts))

    reports: list[ImportReport] = []
    for account in accounts:
        account_root = root / account.id
        if not account_root.exists():
            logger.warning(
                "NFO account dir missing: %s (account=%s)", account_root, account.id,
            )
            continue
        server_id = build_server_id(account.id)

        if "movies" in kinds:
            reports.append(await _import_movies_for_account(
                db, account, account_root, server_id, overwrite, dry_run,
            ))
        if "shows" in kinds:
            reports.append(await _import_shows_for_account(
                db, account, account_root, server_id, overwrite, dry_run,
            ))

    logger.info("NFO import end: %d report(s)", len(reports))
    return reports
