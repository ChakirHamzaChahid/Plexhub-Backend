"""Detect + correct tmdb_id/imdb_id inconsistencies that merge distinct titles.

Problem
-------
An enrichment write can leave a ``media`` row whose ``tmdb_id`` and ``imdb_id``
point at DIFFERENT real titles (the ``imdb_id`` was stamped with another
record's value). Because ``calculate_unification_id`` prefers ``imdb://…``, the
corrupt row joins the unified group of an unrelated film/series — two different
titles collapse into one generated Plex folder (mixed versions/episodes, a NFO
whose title belongs to one title and metadata to another). See
``docs/plans/2026-07-17-omdb-id-consistency-validator-design.md``.

Method (validated in production on 2026-07-17, generalized to films + series)
----------------------------------------------------------------------------
A *suspect group* = rows that share a ``unification_id`` but carry divergent
``tmdb_id`` values. The detection query runs on ``media`` DIRECTLY (the
``media_group`` snapshot carries no ids and is stale between pipeline runs).

Primary signal (internal, language-independent): reload each distinct
``tmdb_id`` of the group via TMDB (``get_movie_details`` / ``get_tv_details``)
and read its REAL ``imdb_id`` (from external_ids). A member whose own
``tmdb_id`` does NOT resolve to the group's imdb is suspect.

Fallback signal (merge-vs-split tie-break): OMDb queried by the group's imdb
gives an authoritative Title / Year / Runtime. OMDb often returns the ENGLISH
title even for francophone content, so a weak title similarity ALONE is never
conclusive — "different content" is only concluded when title AND duration
both diverge clearly.

Classification per member:
  * ``CONSISTENT``               — own tmdb resolves to the group imdb; nothing to do.
  * ``SAME_CONTENT_MISLABELED``  — same content, wrong tmdb_id → reassign to a
                                   CONSISTENT member's (tmdb_id, imdb_id).
  * ``DIFFERENT_CONTENT``        — genuinely different title merged by mistake →
                                   decouple to the member's OWN identity (its own
                                   tmdb_id + that tmdb's real imdb). NEVER a new
                                   title search.
  * ``UNCERTAIN``                — insufficient / contradictory signal; left
                                   untouched, reported for human review.

Usage
-----
    # Preview only (DEFAULT — writes NOTHING):
    python -m app.scripts.validate_id_consistency

    # Restrict scope / cap groups / dump a machine-readable report:
    python -m app.scripts.validate_id_consistency --media-type show --limit 50 --json report.json

    # Apply corrections (makes a timestamped online .backup of the DB first):
    python -m app.scripts.validate_id_consistency --apply

After ``--apply`` the script lists the unification_ids that CHANGED: the
generated-library folders for those groups must be cleared before the next
generation (``LocalStorage`` never overwrites an existing NFO/poster — see the
design doc §6 gotcha). Automating that deletion is a future increment.

NOT auto-run or registered anywhere — ``__main__``-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import func, select, update

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, OmdbScrapeCache
from app.services import (
    omdb_scrape_cache_service,
    unified_group_service,
)
from app.services.aggregation_service import canonical_title_year
from app.services.omdb_service import omdb_service
from app.services.tmdb_service import tmdb_service
from app.utils.db_retry import commit_with_retry
from app.utils.string_normalizer import clean_title, normalize_for_sorting
from app.utils.time import now_ms
from app.utils.unification import (
    calculate_history_group_key,
    calculate_unification_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("plexhub.validate_id")

# Parallel TMDB detail fetches (mirrors backfill_certifications).
_CONCURRENCY = 8
# Seconds to sleep between fan-out batches to avoid sustained 429 pressure.
_INTER_BATCH_SLEEP = 0.5

# Title similarity below this = "far apart"; at or above = "not far".
_TITLE_SIM_THRESHOLD = 0.40
# Runtime tolerance in minutes for the "same content" duration check.
_DURATION_TOLERANCE_MIN = 5

# Classification labels.
CONSISTENT = "CONSISTENT"
SAME_CONTENT_MISLABELED = "SAME_CONTENT_MISLABELED"
DIFFERENT_CONTENT = "DIFFERENT_CONTENT"
UNCERTAIN = "UNCERTAIN"
_ALL_CLASSES = (CONSISTENT, SAME_CONTENT_MISLABELED, DIFFERENT_CONTENT, UNCERTAIN)
_FIX_CLASSES = (SAME_CONTENT_MISLABELED, DIFFERENT_CONTENT)

# TMDBEnrichmentData attr -> Media column. Overwritten (not COALESCE) for the
# two fix classes, but only when the corrected identity actually provides a
# value (a None from TMDB never nulls existing data).
_RICH_MAP = (
    ("summary", "overview"),
    ("genres", "genres"),
    ("year", "year"),
    ("cast", "cast"),
    ("original_title", "original_title"),
    ("tagline", "tagline"),
    ("premiered", "premiered"),
    ("status", "status"),
    ("studio", "studio"),
    ("country", "country"),
    ("content_rating", "content_rating"),
    ("tvdb_id", "tvdb_id"),
    ("wikidata_id", "wikidata_id"),
    ("tmdb_rating", "tmdb_rating"),
    ("tmdb_votes", "tmdb_votes"),
    ("cast_json", "cast_json"),
    ("resolved_thumb_url", "poster_url"),
    ("resolved_art_url", "backdrop_url"),
    ("audience_rating", "vote_average"),
)


# ─── Report model ──────────────────────────────────────────────────────────


@dataclass
class MemberVerdict:
    media_type: str
    server_id: str
    rating_key: str
    title: str
    year: int | None
    old_tmdb_id: str | None
    old_imdb_id: str | None
    old_unification_id: str
    classification: str
    new_tmdb_id: str | None = None
    new_imdb_id: str | None = None
    new_unification_id: str | None = None


@dataclass
class Report:
    media_types: list[str]
    omdb_configured: bool
    applied: bool
    suspect_group_count: int = 0
    members_examined: int = 0
    tmdb_fetches: int = 0
    omdb_fetches: int = 0
    counts: dict[str, int] = field(default_factory=lambda: {c: 0 for c in _ALL_CLASSES})
    verdicts: list[MemberVerdict] = field(default_factory=list)
    rebuilt_types: list[str] = field(default_factory=list)
    changed_unification_ids: list[dict] = field(default_factory=list)


# ─── Small pure helpers ────────────────────────────────────────────────────


def _fmt_imdb(imdb_id: str | None) -> str | None:
    """Normalize to the canonical ``tt…`` form (None passes through)."""
    if not imdb_id:
        return None
    return imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"


def _imdb_eq(a: str | None, b: str | None) -> bool:
    fa, fb = _fmt_imdb(a), _fmt_imdb(b)
    return fa is not None and fa == fb


def _valid_tmdb(value) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    return s.isdigit() and s != "0"


def _norm_title(title: str | None) -> str:
    """NFKD + casefold + strip quality/year tags, reusing the house helpers
    (clean_title strips quality/year; normalize_for_sorting does NFKD +
    lowercase + punctuation strip) — no new normalization introduced."""
    clean, _ = clean_title(title or "")
    return normalize_for_sorting(clean)


def _title_similarity(a: str | None, b: str | None) -> float:
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _content_compare(row: Media, omdb) -> tuple[bool, bool]:
    """Return (same_content, different_content) from the OMDb fallback signal.

    Duration is language-independent and the strong signal; title is weak on
    its own (OMDb returns English titles). "same" needs the duration to match
    AND the title not to be wildly different; "different" needs BOTH the title
    and the duration to diverge clearly. Everything else stays undecided."""
    sim = _title_similarity(row.title, omdb.title)
    title_far = sim < _TITLE_SIM_THRESHOLD

    member_ms = row.duration
    omdb_min = omdb.runtime_minutes
    dur_present = bool(member_ms) and member_ms > 0 and omdb_min is not None
    if dur_present:
        member_min = round(member_ms / 60000)
        delta = abs(member_min - omdb_min)
        dur_close = delta <= _DURATION_TOLERANCE_MIN
        dur_far = delta > _DURATION_TOLERANCE_MIN
    else:
        dur_close = dur_far = False

    same = dur_close and not title_far
    different = title_far and dur_far
    return same, different


def _group_imdb(group_key: str, units: list[Media], own_imdb: dict[str, str | None]) -> str | None:
    """The reference imdb for a group. imdb-keyed groups take it from the key;
    tmdb://… / title_… groups use the imdb the plurality of members' tmdb_ids
    resolve to (the 'majority/consistent member')."""
    if group_key.startswith("imdb://"):
        return _fmt_imdb(group_key[len("imdb://"):])
    counter: Counter = Counter()
    for u in units:
        real = own_imdb.get(u.tmdb_id)
        if real:
            counter[_fmt_imdb(real)] += 1
    if not counter:
        return None
    top = counter.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        logger.warning(
            "Plurality tie on group %s (%d votes each) — arbitrarily picking %s",
            group_key, top[0][1], top[0][0],
        )
    return top[0][0]


def _classify(
    row: Media,
    real_imdb: str | None,
    group_imdb: str | None,
    consistent_source: tuple[str, str | None] | None,
    omdb_data,
) -> tuple[str, tuple]:
    """Return (classification, (new_tmdb, new_imdb)) for one member."""
    if group_imdb is not None and _imdb_eq(real_imdb, group_imdb):
        return CONSISTENT, (None, None)
    if omdb_data is None:
        # No fallback signal (OMDb unconfigured / not-found / budget) — can't tell.
        return UNCERTAIN, (None, None)
    same, different = _content_compare(row, omdb_data)
    if same:
        if consistent_source is None:
            return UNCERTAIN, (None, None)
        return SAME_CONTENT_MISLABELED, consistent_source
    if different:
        return DIFFERENT_CONTENT, (row.tmdb_id, real_imdb)
    return UNCERTAIN, (None, None)


def _dedup_units(rows: list[Media]) -> list[Media]:
    """One representative row per (server_id, rating_key) — a physical item
    can appear as N ``media`` rows differing only on ``filter`` (category)."""
    seen: set[tuple] = set()
    units: list[Media] = []
    for r in rows:
        pk = (r.server_id, r.rating_key)
        if pk in seen:
            continue
        seen.add(pk)
        units.append(r)
    return units


# ─── DB access ─────────────────────────────────────────────────────────────


async def _detect(db, media_type: str, limit: int | None) -> list[str]:
    """Suspect unification_ids for one media_type (COUNT(DISTINCT tmdb_id) > 1)."""
    stmt = (
        select(Media.unification_id)
        .where(
            Media.type == media_type,
            Media.is_in_allowed_categories.is_(True),
            Media.tmdb_id.isnot(None),
            Media.tmdb_id != "",
            Media.unification_id != "",
        )
        .group_by(Media.unification_id)
        .having(func.count(func.distinct(Media.tmdb_id)) > 1)
        .order_by(Media.unification_id)
    )
    uids = list((await db.execute(stmt)).scalars().all())
    if limit is not None:
        uids = uids[:limit]
    return uids


async def _load_members(db, media_type: str, uid: str) -> list[Media]:
    stmt = select(Media).where(
        Media.type == media_type,
        Media.unification_id == uid,
        Media.is_in_allowed_categories.is_(True),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _ensure_details(db, media_type, tmdb_ids, tmdb, details_cache, report):
    """Fetch TMDB details for each not-yet-cached tmdb_id (concurrency-bounded,
    batched with an inter-batch sleep). A dead/404 id caches to None."""
    todo = [t for t in tmdb_ids if (media_type, int(t)) not in details_cache]
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(t):
        async with sem:
            try:
                if media_type == "movie":
                    data = await tmdb.get_movie_details(int(t))
                else:
                    data = await tmdb.get_tv_details(int(t))
                return t, data
            except Exception as exc:  # dead tmdb_id / transport error
                logger.debug(
                    "TMDB details failed tmdb_id=%s type=%s: %s",
                    t, media_type, type(exc).__name__,
                )
                return t, None

    for i in range(0, len(todo), _CONCURRENCY):
        batch = todo[i:i + _CONCURRENCY]
        results = await asyncio.gather(*[_one(t) for t in batch])
        for t, data in results:
            details_cache[(media_type, int(t))] = data
            report.tmdb_fetches += 1
        if i + _CONCURRENCY < len(todo):
            await asyncio.sleep(_INTER_BATCH_SLEEP)


async def _omdb_lookup(db, imdb_id, omdb, daily_limit, run_cache, report, now):
    """OMDb data for a group imdb — cache-first, budget-aware. Returns None
    when there is no usable fallback signal (unconfigured / not-found /
    budget exhausted)."""
    if imdb_id in run_cache:
        return run_cache[imdb_id]

    cached = await omdb_scrape_cache_service.get(db, imdb_id, now)
    if cached is not None:
        run_cache[imdb_id] = cached
        return cached

    # A fresh 'not_found' row means "confirmed absent, don't re-query" — the
    # cache service's get() collapses that to None, so read the row directly.
    row = (await db.execute(
        select(OmdbScrapeCache).where(OmdbScrapeCache.imdb_id == imdb_id)
    )).scalars().first()
    if (
        row is not None
        and row.result == "not_found"
        and (now - row.fetched_at) <= omdb_scrape_cache_service.NEG_TTL_MS
    ):
        run_cache[imdb_id] = None
        return None

    if not getattr(omdb, "is_configured", False):
        run_cache[imdb_id] = None
        return None
    if omdb.get_request_count() >= daily_limit:
        logger.info("OMDb daily budget reached (%d) — skipping %s", daily_limit, imdb_id)
        run_cache[imdb_id] = None
        return None

    data = await omdb.get_by_imdb_id(imdb_id)
    report.omdb_fetches += 1
    await omdb_scrape_cache_service.put(
        db, imdb_id, "found" if data else "not_found", data, now
    )
    run_cache[imdb_id] = data
    return data


async def _apply_fix(db, row, media_type, new_tmdb, new_imdb, new_uid, new_hgk,
                     details_cache, omdb_data, group_imdb, now):
    """Write a correction. Identity fields overwrite unconditionally (they are
    WRONG, not absent); rich NFO fields overwrite only where the corrected
    identity provides them; imdb_rating/votes are COALESCE (fill-missing)."""
    values = {
        "tmdb_id": str(new_tmdb) if new_tmdb is not None else None,
        "imdb_id": _fmt_imdb(new_imdb),
        "unification_id": new_uid,
        "history_group_key": new_hgk,
        "updated_at": now,
    }

    det = details_cache.get((media_type, int(new_tmdb))) if _valid_tmdb(new_tmdb) else None
    if det is not None:
        for col, attr in _RICH_MAP:
            v = getattr(det, attr, None)
            if v is not None:
                values[col] = v

    # Accessory COALESCE: only when the corrected imdb IS the group imdb (so the
    # group's OMDb data genuinely describes this row) and the column is empty.
    final_imdb = _fmt_imdb(new_imdb)
    if omdb_data is not None and group_imdb is not None and final_imdb == group_imdb:
        if row.imdb_rating is None and omdb_data.imdb_rating is not None:
            values["imdb_rating"] = omdb_data.imdb_rating
        if row.imdb_votes is None and omdb_data.imdb_votes is not None:
            values["imdb_votes"] = omdb_data.imdb_votes

    await db.execute(
        update(Media)
        .where(Media.server_id == row.server_id, Media.rating_key == row.rating_key)
        .values(**values)
    )


# ─── Core (testable) ───────────────────────────────────────────────────────


async def run(
    db,
    *,
    media_type: str = "all",
    limit: int | None = None,
    apply: bool = False,
    tmdb=tmdb_service,
    omdb=omdb_service,
    rebuild=unified_group_service.rebuild,
    omdb_daily_limit: int | None = None,
    now: int | None = None,
) -> Report:
    """Detect + (optionally) correct tmdb_id/imdb_id inconsistencies.

    Operates entirely on the single ``db`` session (reads, writes, snapshot
    rebuild, commit). Service dependencies are injectable for tests. The DB
    backup and argparse live in ``main`` — this function never backs up.
    """
    now = now_ms() if now is None else now
    daily_limit = settings.OMDB_DAILY_LIMIT if omdb_daily_limit is None else omdb_daily_limit
    media_types = ["movie", "show"] if media_type == "all" else [media_type]

    report = Report(
        media_types=media_types,
        omdb_configured=bool(getattr(omdb, "is_configured", False)),
        applied=apply,
    )

    details_cache: dict[tuple[str, int], object] = {}
    omdb_run_cache: dict[str, object] = {}
    touched_types: set[str] = set()

    for mt in media_types:
        suspect_uids = await _detect(db, mt, limit)
        for uid in suspect_uids:
            rows = await _load_members(db, mt, uid)
            units = _dedup_units(rows)
            if len(units) < 2:
                continue
            report.suspect_group_count += 1

            distinct_tmdb = {u.tmdb_id for u in units if _valid_tmdb(u.tmdb_id)}
            await _ensure_details(db, mt, distinct_tmdb, tmdb, details_cache, report)
            own_imdb = {
                t: (details_cache[(mt, int(t))].imdb_id
                    if details_cache.get((mt, int(t))) is not None else None)
                for t in distinct_tmdb
            }

            group_imdb = _group_imdb(uid, units, own_imdb)

            # Source of truth for a reassignment = a CONSISTENT member.
            consistent_source: tuple[str, str | None] | None = None
            for u in units:
                real = own_imdb.get(u.tmdb_id)
                if group_imdb is not None and _imdb_eq(real, group_imdb):
                    consistent_source = (u.tmdb_id, u.imdb_id or _fmt_imdb(group_imdb))
                    break

            # OMDb fallback (once per group) only if some member is unconfirmed.
            need_omdb = any(
                not (group_imdb is not None and _imdb_eq(own_imdb.get(u.tmdb_id), group_imdb))
                for u in units
            )
            omdb_data = None
            if need_omdb and group_imdb is not None:
                omdb_data = await _omdb_lookup(
                    db, group_imdb, omdb, daily_limit, omdb_run_cache, report, now
                )

            for u in units:
                report.members_examined += 1
                real = own_imdb.get(u.tmdb_id)
                cls, (new_tmdb, new_imdb) = _classify(
                    u, real, group_imdb, consistent_source, omdb_data
                )

                new_uid = None
                new_hgk = None
                if cls in _FIX_CLASSES:
                    new_uid = calculate_unification_id(
                        u.title or "", u.year,
                        _fmt_imdb(new_imdb),
                        str(new_tmdb) if new_tmdb is not None else None,
                    )
                    if not new_uid:
                        cls = UNCERTAIN
                        new_tmdb = new_imdb = None
                    else:
                        new_hgk = calculate_history_group_key(
                            new_uid, u.rating_key, u.server_id
                        )

                report.counts[cls] += 1
                report.verdicts.append(MemberVerdict(
                    media_type=mt,
                    server_id=u.server_id,
                    rating_key=u.rating_key,
                    title=u.title or "",
                    year=u.year,
                    old_tmdb_id=u.tmdb_id,
                    old_imdb_id=u.imdb_id,
                    old_unification_id=uid,
                    classification=cls,
                    new_tmdb_id=str(new_tmdb) if new_tmdb is not None else None,
                    new_imdb_id=_fmt_imdb(new_imdb),
                    new_unification_id=new_uid,
                ))

                if cls in _FIX_CLASSES:
                    if new_uid != (uid or ""):
                        ctitle, cyear = canonical_title_year(u)
                        report.changed_unification_ids.append({
                            "old": uid, "new": new_uid,
                            "title": ctitle, "year": cyear,
                        })
                    if apply:
                        await _apply_fix(
                            db, u, mt, new_tmdb, new_imdb, new_uid, new_hgk,
                            details_cache, omdb_data, group_imdb, now,
                        )
                        touched_types.add(mt)

    if apply:
        for mt in sorted(touched_types):
            await rebuild(db, mt)
            report.rebuilt_types.append(mt)
        await commit_with_retry(db)

    return report


# ─── CLI ───────────────────────────────────────────────────────────────────


def _report_to_dict(report: Report) -> dict:
    d = asdict(report)
    # Trim CONSISTENT verdicts from the JSON payload — only actionable rows.
    d["verdicts"] = [
        v for v in d["verdicts"] if v["classification"] != CONSISTENT
    ]
    return d


def _print_report(report: Report) -> None:
    print("\n=== tmdb/imdb consistency validator ===")
    print(f"media types      : {', '.join(report.media_types)}")
    print(f"OMDb fallback    : {'configured' if report.omdb_configured else 'DISABLED (more UNCERTAIN)'}")
    print(f"mode             : {'APPLY (writing)' if report.applied else 'DRY-RUN (no writes)'}")
    print(f"suspect groups   : {report.suspect_group_count}")
    print(f"members examined : {report.members_examined}")
    print(f"TMDB fetches     : {report.tmdb_fetches}   OMDb fetches: {report.omdb_fetches}")
    print("classification   : " + "  ".join(
        f"{c}={report.counts[c]}" for c in _ALL_CLASSES
    ))

    actionable = [v for v in report.verdicts if v.classification != CONSISTENT]
    if actionable:
        print("\n--- members needing action ---")
        for v in actionable:
            line = (
                f"  [{v.classification:>22}] {v.title!r}({v.year}) "
                f"{v.server_id}:{v.rating_key} "
                f"tmdb {v.old_tmdb_id}->{v.new_tmdb_id} "
                f"imdb {v.old_imdb_id}->{v.new_imdb_id}"
            )
            print(line)

    if report.applied and report.changed_unification_ids:
        print("\n--- unification_ids CHANGED (clear these generated folders "
              "before the next library generation) ---")
        for c in report.changed_unification_ids:
            print(f"  {c['old']}  ->  {c['new']}   ({c['title']!r} {c['year']})")

    if not report.applied and (report.counts[SAME_CONTENT_MISLABELED]
                               or report.counts[DIFFERENT_CONTENT]):
        print("\nDRY-RUN — nothing written. Re-run with --apply to correct.")


def _backup(db_path: Path) -> Path:
    """Blocking online sqlite .backup — call via asyncio.to_thread (§9)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = db_path.with_name(f"{db_path.stem}.preidfix-{stamp}.db")
    src = sqlite3.connect(str(db_path))
    try:
        with sqlite3.connect(str(dest)) as bck:
            src.backup(bck)
    finally:
        src.close()
    return dest


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Detect and (with --apply) correct media rows whose tmdb_id/imdb_id "
            "point at different real titles, merging distinct titles into one "
            "unified group. Dry-run by default."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write corrections (default: dry-run, writes nothing). Makes a "
             "timestamped online .backup of the DB first.",
    )
    p.add_argument(
        "--media-type",
        choices=["movie", "show", "all"],
        default="all",
        help="Restrict to one media type (default: all).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap the number of suspect groups examined per media type.",
    )
    p.add_argument(
        "--json",
        dest="json_path",
        default=None,
        metavar="PATH",
        help="Also write the full report as JSON to PATH.",
    )
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> None:
    if args.apply:
        db_path = Path(settings.DB_PATH)
        if db_path.exists():
            backup = await asyncio.to_thread(_backup, db_path)
            logger.info("Backup written: %s", backup)

    async with async_session_factory() as db:
        report = await run(
            db,
            media_type=args.media_type,
            limit=args.limit,
            apply=args.apply,
        )

    _print_report(report)
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(_report_to_dict(report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def main(argv=None) -> None:
    args = _parse_args(argv)
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
