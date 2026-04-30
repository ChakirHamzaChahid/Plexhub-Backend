"""Import IMDb / TMDB IDs from tinyMediaManager .nfo files into the media table.

Expected layout under the library root::

    <root>/Films/<Title> (<Year>)/movie.nfo
    <root>/Series/<Title>/tvshow.nfo

Matching strategy: for each NFO we try several (title, year) candidates against
an in-memory index of movies/shows in the DB, keyed by
(normalized_title_for_sorting, year). The first candidate yielding a single
match wins; ambiguous (multiple) or empty matches are reported as 'unmatched'.

Only `imdb_id` and `tmdb_id` are written. By default we only fill missing
values; pass `overwrite=True` to replace existing ones.
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

from app.models.database import Media
from app.utils.string_normalizer import parse_title_and_year, normalize_for_sorting
from app.utils.time import now_ms


logger = logging.getLogger("plexhub.nfo_import")

_IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
_TMDB_ID_RE = re.compile(r"^\d{1,9}$")


@dataclass
class NfoEntry:
    """One parsed .nfo file ready to be matched against the DB."""
    path: Path
    folder_name: str
    media_type: str  # "movie" or "show"
    nfo_title: Optional[str] = None
    nfo_originaltitle: Optional[str] = None
    nfo_year: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None


@dataclass
class ImportReport:
    media_type: str
    scanned: int = 0
    parsed: int = 0
    parse_errors: list[str] = field(default_factory=list)
    matched: int = 0
    written: int = 0
    skipped_no_change: int = 0
    skipped_id_already_set: int = 0
    unmatched: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    dry_run: bool = False
    overwrite: bool = False


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
    """Parse a movie.nfo or tvshow.nfo. Returns None on hard failure."""
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
        nfo_title=_first_text(root, "title"),
        nfo_originaltitle=_first_text(root, "originaltitle"),
        nfo_year=nfo_year,
        imdb_id=imdb,
        tmdb_id=tmdb,
    )


def scan_directory(root: Path, kind: str) -> list[NfoEntry]:
    """Scan one of the expected sub-trees.

    kind="movies" -> root/Films/<Title (Year)>/movie.nfo
    kind="shows"  -> root/Series/<Title>/tvshow.nfo
    """
    if kind == "movies":
        sub, nfo_name, media_type = "Films", "movie.nfo", "movie"
    elif kind == "shows":
        sub, nfo_name, media_type = "Series", "tvshow.nfo", "show"
    else:
        raise ValueError(f"Unknown kind: {kind}")

    base = root / sub
    if not base.exists():
        logger.info("NFO subdir not present: %s", base)
        return []

    out: list[NfoEntry] = []
    for nfo in base.glob(f"*/{nfo_name}"):
        entry = parse_nfo_file(nfo, media_type)
        if entry is not None:
            out.append(entry)
    return out


def _candidate_keys(entry: NfoEntry) -> list[tuple[str, Optional[int]]]:
    """Yield (normalized_title, year) candidates to look up in the DB index.

    Order matters: try the most reliable candidate first.
    """
    candidates: list[tuple[str, Optional[int]]] = []
    seen: set[tuple[str, Optional[int]]] = set()

    def push(raw: Optional[str], year_hint: Optional[int]) -> None:
        if not raw:
            return
        title, year_in_str = parse_title_and_year(raw)
        year = year_in_str or year_hint
        norm = normalize_for_sorting(title).strip().lower()
        if not norm:
            return
        key = (norm, year)
        if key not in seen:
            seen.add(key)
            candidates.append(key)

    push(entry.folder_name, entry.nfo_year)
    push(entry.nfo_originaltitle, entry.nfo_year)
    push(entry.nfo_title, entry.nfo_year)
    return candidates


async def _build_db_index(
    db: AsyncSession, media_type: str,
) -> dict[tuple[str, Optional[int]], list[Media]]:
    """Index every media row of `media_type` by (normalized_title, year).

    The same key may map to several rows in case of homonyms; callers should
    treat multi-hits as ambiguous.
    """
    result = await db.execute(select(Media).where(Media.type == media_type))
    rows = list(result.scalars().all())
    index: dict[tuple[str, Optional[int]], list[Media]] = {}
    for row in rows:
        title, year_in_str = parse_title_and_year(row.title)
        year = year_in_str or row.year
        norm = normalize_for_sorting(title).strip().lower()
        if not norm:
            continue
        index.setdefault((norm, year), []).append(row)
    return index


async def import_nfo(
    db: AsyncSession,
    root: Path,
    *,
    kinds: tuple[str, ...] = ("movies", "shows"),
    overwrite: bool = False,
    dry_run: bool = False,
) -> list[ImportReport]:
    """Run a scan+match+write cycle. Returns one ImportReport per kind.

    Caller commits via Depends(get_db); when dry_run=True nothing is updated.
    """
    reports: list[ImportReport] = []
    for kind in kinds:
        media_type = "movie" if kind == "movies" else "show"
        report = ImportReport(
            media_type=media_type, dry_run=dry_run, overwrite=overwrite,
        )

        entries = scan_directory(root, kind)
        report.scanned = len(entries)
        report.parsed = len(entries)

        if not entries:
            reports.append(report)
            continue

        index = await _build_db_index(db, media_type)

        # Group writes per (rating_key, server_id) — within one batch a row may
        # be touched multiple times by overlapping NFO matches (collections),
        # but the imdb/tmdb values are stable so last-write-wins is fine.
        for entry in entries:
            if not entry.imdb_id and not entry.tmdb_id:
                report.unmatched.append(
                    f"{entry.path.name} ({entry.folder_name}): NFO has no imdb/tmdb"
                )
                continue

            matched_rows: list[Media] = []
            for key in _candidate_keys(entry):
                hits = index.get(key, [])
                if len(hits) == 1:
                    matched_rows = hits
                    break
                if len(hits) > 1:
                    matched_rows = hits  # ambiguous — keep going to maybe find unique
            if not matched_rows:
                report.unmatched.append(entry.folder_name)
                continue
            if len(matched_rows) > 1:
                report.ambiguous.append(
                    f"{entry.folder_name} → {len(matched_rows)} candidats"
                )
                continue

            report.matched += 1
            row = matched_rows[0]

            updates: dict[str, str] = {}
            if entry.imdb_id and (overwrite or not row.imdb_id):
                if entry.imdb_id != row.imdb_id:
                    updates["imdb_id"] = entry.imdb_id
            if entry.tmdb_id and (overwrite or not row.tmdb_id):
                if entry.tmdb_id != row.tmdb_id:
                    updates["tmdb_id"] = entry.tmdb_id

            if not updates:
                if (entry.imdb_id and row.imdb_id and not overwrite) or (
                    entry.tmdb_id and row.tmdb_id and not overwrite
                ):
                    report.skipped_id_already_set += 1
                else:
                    report.skipped_no_change += 1
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
        reports.append(report)

    return reports
