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

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Media, XtreamAccount
from app.plex_generator.mapping import MappingStore
from app.plex_generator.naming import series_nfo_path
from app.utils.server_id import build_server_id
from app.utils.time import now_ms


logger = logging.getLogger("plexhub.nfo_import")

_IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
_TMDB_ID_RE = re.compile(r"^\d{1,9}$")

# Commit frequency for bulk UPDATEs. Smaller = releases the SQLite writer lock
# more often (good when the health_check_worker is also writing concurrently),
# larger = fewer fsync round-trips. 50 is a reasonable middle ground.
_COMMIT_BATCH_SIZE = 50

# How long to wait for the SQLite writer lock before giving up. The default
# (5 s, set in init_db) is too short when sync_worker / health_check_worker
# hold the writer for several seconds during a bulk run.
_BUSY_TIMEOUT_MS = 30_000


async def _prepare_db_for_bulk_writes(db: AsyncSession) -> None:
    """Local PRAGMA tweak: tolerate longer writer-lock contention during import.

    `PRAGMA busy_timeout` is per-connection and aiosqlite reuses one connection
    per session, so this only affects the import session — production paths
    keep the global 5 s default.
    """
    try:
        await db.execute(text(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}"))
    except Exception as exc:
        logger.warning("Could not raise busy_timeout: %s", exc)


# Retry budget for SQLite "database is locked" errors. With the
# health_check_worker writing continuously, a single UPDATE can lose the race
# many times in a row before catching a free slot.
_LOCK_RETRY_MAX_ATTEMPTS = 12
_LOCK_RETRY_INITIAL_DELAY = 0.25  # seconds
_LOCK_RETRY_MAX_DELAY = 5.0


def _is_locked_error(exc: Exception) -> bool:
    return "database is locked" in str(exc).lower()


async def _execute_with_lock_retry(
    db: AsyncSession, statement: Any,
) -> Any:
    """Run db.execute(...) with exponential backoff on 'database is locked'.

    Other operational errors are re-raised immediately.
    """
    delay = _LOCK_RETRY_INITIAL_DELAY
    for attempt in range(1, _LOCK_RETRY_MAX_ATTEMPTS + 1):
        try:
            return await db.execute(statement)
        except OperationalError as exc:
            if not _is_locked_error(exc):
                raise
            if attempt == _LOCK_RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "DB lock retry exhausted after %d attempts (last delay=%.2fs)",
                    _LOCK_RETRY_MAX_ATTEMPTS, delay,
                )
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, _LOCK_RETRY_MAX_DELAY)


async def _commit_with_lock_retry(db: AsyncSession) -> None:
    """Commit with the same backoff strategy as _execute_with_lock_retry."""
    delay = _LOCK_RETRY_INITIAL_DELAY
    for attempt in range(1, _LOCK_RETRY_MAX_ATTEMPTS + 1):
        try:
            await db.commit()
            return
        except OperationalError as exc:
            if not _is_locked_error(exc):
                raise
            if attempt == _LOCK_RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "DB commit lock retry exhausted after %d attempts", _LOCK_RETRY_MAX_ATTEMPTS,
                )
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, _LOCK_RETRY_MAX_DELAY)


@dataclass
class NfoEntry:
    path: Path
    folder_name: str
    media_type: str  # "movie" or "show"
    # IDs
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    # Metadata
    nfo_year: Optional[int] = None
    summary: Optional[str] = None
    duration_ms: Optional[int] = None  # <runtime> is in minutes; we store ms
    content_rating: Optional[str] = None  # <mpaa> / <certification>
    genres_csv: Optional[str] = None      # join of <genre>...</genre>
    cast_csv: Optional[str] = None        # join of <actor><name>
    rating: Optional[float] = None        # IMDb-priority pick from <ratings>


# Ratings preference order — first hit wins.
_RATING_PREFERENCE = ("imdb", "themoviedb", "tmdb", "trakt", "default")


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


def _join_multi(root: ET.Element, tag: str, sep: str = ", ") -> Optional[str]:
    """Join non-empty text content of every <tag> child. Dedupe preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for el in root.findall(tag):
        if el.text and el.text.strip():
            v = el.text.strip()
            if v not in seen:
                seen.add(v)
                out.append(v)
    return sep.join(out) if out else None


def _extract_cast(root: ET.Element) -> Optional[str]:
    """Concatenate <actor><name> in declaration order, deduplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for actor in root.findall("actor"):
        name_el = actor.find("name")
        if name_el is not None and name_el.text and name_el.text.strip():
            v = name_el.text.strip()
            if v not in seen:
                seen.add(v)
                out.append(v)
    return ", ".join(out) if out else None


def _extract_runtime_ms(root: ET.Element) -> Optional[int]:
    raw = _first_text(root, "runtime")
    if not raw:
        return None
    try:
        minutes = int(raw)
    except ValueError:
        return None
    if minutes <= 0:
        return None
    return minutes * 60 * 1000


def _extract_best_rating(root: ET.Element) -> Optional[float]:
    """Pick a single rating with IMDb > TMDB > Trakt > default > top-level <rating>."""
    ratings: dict[str, float] = {}
    for el in root.findall("ratings/rating"):
        name = (el.get("name") or "").strip().lower()
        if not name:
            continue
        value_el = el.find("value")
        if value_el is None or not value_el.text:
            continue
        try:
            ratings[name] = float(value_el.text)
        except ValueError:
            continue
    for key in _RATING_PREFERENCE:
        if key in ratings and ratings[key] > 0:
            return ratings[key]
    # Fallback: top-level <rating> (e.g. tinyMM movie.nfo doesn't always emit <ratings>)
    raw = _first_text(root, "rating")
    if raw:
        try:
            v = float(raw)
            return v if v > 0 else None
        except ValueError:
            return None
    return None


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
        imdb_id=imdb,
        tmdb_id=tmdb,
        nfo_year=nfo_year,
        summary=_first_text(root, "plot", "outline"),
        duration_ms=_extract_runtime_ms(root),
        content_rating=_first_text(root, "mpaa", "certification"),
        genres_csv=_join_multi(root, "genre"),
        cast_csv=_extract_cast(root),
        rating=_extract_best_rating(root),
    )


# (column, source attr on NfoEntry, "missing" predicate on current value)
# - For text columns "missing" means falsy (None / "")
# - For numeric columns "missing" means falsy AND <= 0 (year/duration default to 0)
def _is_text_missing(v) -> bool:
    return v is None or v == ""


def _is_numeric_missing(v) -> bool:
    return v is None or (isinstance(v, (int, float)) and v <= 0)


_FIELD_MAP: tuple[tuple[str, str, callable], ...] = (
    ("imdb_id",        "imdb_id",       _is_text_missing),
    ("tmdb_id",        "tmdb_id",       _is_text_missing),
    ("summary",        "summary",       _is_text_missing),
    ("year",           "nfo_year",      _is_numeric_missing),
    ("duration",       "duration_ms",   _is_numeric_missing),
    ("content_rating", "content_rating", _is_text_missing),
    ("genres",         "genres_csv",    _is_text_missing),
    ("cast",           "cast_csv",      _is_text_missing),
    ("scraped_rating", "rating",        _is_numeric_missing),
)


def _nfo_has_any_data(parsed: NfoEntry) -> bool:
    """True if the NFO supplies at least one writable field."""
    return any(getattr(parsed, attr) is not None for _col, attr, _ in _FIELD_MAP)


def _compute_updates(row: Media, parsed: NfoEntry, overwrite: bool) -> dict:
    """Build the SQL UPDATE values dict, respecting fill-missing-only by default."""
    updates: dict = {}
    for col, attr, is_missing in _FIELD_MAP:
        new_val = getattr(parsed, attr)
        if new_val is None:
            continue
        current = getattr(row, col, None)
        if not overwrite and not is_missing(current):
            continue  # already set, don't touch
        if current == new_val:
            continue  # already up-to-date
        updates[col] = new_val
    return updates


def _classify_no_update(
    row: Media, parsed: NfoEntry, overwrite: bool, report: ImportReport,
) -> None:
    """Distinguish 'NFO had data we'd write but column already filled' from
    'BDD already matches NFO exactly'."""
    if overwrite:
        report.skipped_no_change += 1
        return
    for col, attr, is_missing in _FIELD_MAP:
        nfo_val = getattr(parsed, attr)
        if nfo_val is None:
            continue
        if not is_missing(getattr(row, col, None)):
            report.skipped_id_already_set += 1
            return
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

    pending_writes = 0
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
        if not _nfo_has_any_data(parsed):
            report.unmatched.append(f"{folder_name}: NFO empty")
            continue

        row = (await _execute_with_lock_retry(
            db,
            select(Media).where(
                Media.rating_key == rating_key,
                Media.server_id == server_id,
                Media.type == "movie",
            ).limit(1),
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

        await _execute_with_lock_retry(
            db,
            update(Media)
            .where(
                Media.rating_key == row.rating_key,
                Media.server_id == row.server_id,
            )
            .values(**updates, updated_at=now_ms()),
        )
        report.written += 1
        pending_writes += 1
        if pending_writes >= _COMMIT_BATCH_SIZE:
            await _commit_with_lock_retry(db)
            pending_writes = 0

    if not dry_run and pending_writes:
        await _commit_with_lock_retry(db)
    logger.info(
        "NFO movies account=%s done: matched=%d written=%d unmatched=%d "
        "skipped_already=%d skipped_nochange=%d",
        account.id, report.matched, report.written,
        len(report.unmatched),
        report.skipped_id_already_set, report.skipped_no_change,
    )
    return report


async def _apply_show_nfo(
    db: AsyncSession, show: Media, nfo_path: Path,
    label: str, overwrite: bool, dry_run: bool,
    report: ImportReport, pending_writes: int,
) -> int:
    """Read tvshow.nfo at `nfo_path`, apply updates to the show row.
    Returns the new pending_writes counter (commits every _COMMIT_BATCH_SIZE)."""
    if not nfo_path.exists():
        report.unmatched.append(f"{label}: {nfo_path.relative_to(nfo_path.anchor)} not found")
        return pending_writes
    report.scanned += 1

    parsed = parse_nfo_file(nfo_path, "show")
    if parsed is None:
        report.parse_errors.append(label)
        return pending_writes
    if not _nfo_has_any_data(parsed):
        report.unmatched.append(f"{label}: NFO empty")
        return pending_writes

    report.matched += 1
    updates = _compute_updates(show, parsed, overwrite)
    if not updates:
        _classify_no_update(show, parsed, overwrite, report)
        return pending_writes
    if dry_run:
        report.written += 1
        return pending_writes

    await _execute_with_lock_retry(
        db,
        update(Media)
        .where(
            Media.rating_key == show.rating_key,
            Media.server_id == show.server_id,
        )
        .values(**updates, updated_at=now_ms()),
    )
    report.written += 1
    pending_writes += 1
    if pending_writes >= _COMMIT_BATCH_SIZE:
        await _commit_with_lock_retry(db)
        pending_writes = 0
    return pending_writes


async def _import_shows_for_account(
    db: AsyncSession,
    account: XtreamAccount,
    account_root: Path,
    server_id: str,
    overwrite: bool,
    dry_run: bool,
) -> ImportReport:
    """Mapping-driven import for shows.

    The mapping JSON contains episode entries (per-stream), so we walk it to
    derive the actual on-disk show folder for every series and read the
    tvshow.nfo there. Shows with no episode in the mapping fall back to a
    DB-driven path computed from `series_nfo_path(title, year=show.year)`.
    """
    report = ImportReport(
        account_id=account.id, media_type="show",
        overwrite=overwrite, dry_run=dry_run,
    )

    # Load all shows once (de-duped on rating_key — multi-(filter, sort_order)).
    shows_q = await _execute_with_lock_retry(
        db,
        select(Media).where(
            Media.type == "show", Media.server_id == server_id,
        ),
    )
    shows_by_rk: dict[str, Media] = {}
    for show in shows_q.scalars().all():
        if show.rating_key not in shows_by_rk:
            shows_by_rk[show.rating_key] = show
    logger.info(
        "NFO shows account=%s: %d unique shows in DB",
        account.id, len(shows_by_rk),
    )

    # ---------- Pass 1: mapping-driven ----------
    # mapping.path looks like "Series/<show_folder>/Season XX/<ep>.strm"
    # → show_folder is parts[1]; tvshow.nfo lives at <root>/Series/<show_folder>/tvshow.nfo.
    mapping = MappingStore(account_root)
    mapping.load()
    show_folder_to_episode_rks: dict[str, list[str]] = {}
    for source_id, entry in mapping._data.items():
        if not entry.path.startswith("Series/"):
            continue
        parts = Path(entry.path).parts
        if len(parts) < 2:
            continue
        show_folder_to_episode_rks.setdefault(parts[1], []).append(source_id)
    logger.info(
        "NFO shows account=%s: %d distinct show folders in mapping",
        account.id, len(show_folder_to_episode_rks),
    )

    # Resolve episode rating_keys → show rating_key (= grandparent_rating_key).
    # One batched SELECT per chunk of 500 keys to stay polite with SQLite.
    sample_eps: list[str] = [eps[0] for eps in show_folder_to_episode_rks.values()]
    ep_to_show_rk: dict[str, str] = {}
    chunk = 500
    for i in range(0, len(sample_eps), chunk):
        rks = sample_eps[i:i + chunk]
        result = await _execute_with_lock_retry(
            db,
            select(Media.rating_key, Media.grandparent_rating_key)
            .where(
                Media.server_id == server_id,
                Media.type == "episode",
                Media.rating_key.in_(rks),
            ),
        )
        for ep_rk, gp_rk in result.all():
            if gp_rk:
                ep_to_show_rk[ep_rk] = gp_rk

    pending_writes = 0
    handled_show_rks: set[str] = set()
    for show_folder, ep_rks in show_folder_to_episode_rks.items():
        # Pick the first episode whose grandparent we managed to resolve.
        show_rk = next(
            (ep_to_show_rk[ep_rk] for ep_rk in ep_rks if ep_rk in ep_to_show_rk),
            None,
        )
        if show_rk is None or show_rk not in shows_by_rk:
            # Episode points at a show that no longer exists in DB — skip.
            continue
        if show_rk in handled_show_rks:
            continue
        handled_show_rks.add(show_rk)
        show = shows_by_rk[show_rk]
        nfo_path = account_root / "Series" / show_folder / "tvshow.nfo"
        pending_writes = await _apply_show_nfo(
            db, show, nfo_path, label=show.title or show_folder,
            overwrite=overwrite, dry_run=dry_run,
            report=report, pending_writes=pending_writes,
        )

    # ---------- Pass 2: fallback DB-driven for shows not in mapping ----------
    # Some shows have no episodes mapped (recently added, or episodes deleted).
    # Compute the path the generator WOULD use, including year if present.
    fallback_count = 0
    for show_rk, show in shows_by_rk.items():
        if show_rk in handled_show_rks:
            continue
        nfo_rel = series_nfo_path(show.title or "", year=show.year)
        nfo_path = account_root / nfo_rel
        fallback_count += 1
        pending_writes = await _apply_show_nfo(
            db, show, nfo_path, label=show.title or show_rk,
            overwrite=overwrite, dry_run=dry_run,
            report=report, pending_writes=pending_writes,
        )
    if fallback_count:
        logger.info(
            "NFO shows account=%s: %d shows tried via fallback path (no episodes in mapping)",
            account.id, fallback_count,
        )

    if not dry_run and pending_writes:
        await _commit_with_lock_retry(db)
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

    if not dry_run:
        await _prepare_db_for_bulk_writes(db)

    accounts_q = await _execute_with_lock_retry(
        db,
        select(XtreamAccount).where(XtreamAccount.is_active == True),  # noqa: E712
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
