"""One-shot DB maintenance: propagate an external id from a RESOLVED row to its
UNRESOLVED twin so duplicate movies/shows collapse into one unified entry.

Problem it solves
-----------------
The same title is often present twice in the catalog: one row resolved by the
scraper (has imdb_id/tmdb_id → unification_id `imdb://…`/`tmdb://…`) and one row
the scraper missed (no id → `title_…`). They never dedup because the generator /
API group by `unification_id`. `import-nfo` can't fix the un-scraped twin (it has
no id-bearing NFO).

This script links them **safely**:
  * Matching is EXACT on (normalized clean title, year) — NEVER fuzzy. Fuzzy
    `token_set_ratio` is unsafe here (short generic titles like "Des"/"Jane" are
    word-subsets of many unrelated French titles → catastrophic false merges).
  * It only ever WRITES to rows that have NO external id (a true twin). Resolved
    rows are never modified, so a wrong guess cannot overwrite good data.
  * For each matched unresolved row it copies imdb_id/tmdb_id from the resolved
    twin and recomputes unification_id + history_group_key with the SAME helpers
    the rest of the backend uses (so it groups identically afterwards).

Usage
-----
    python -m app.scripts.dedup_resolved_twins                 # DRY-RUN (no writes)
    python -m app.scripts.dedup_resolved_twins --apply         # write changes
    python -m app.scripts.dedup_resolved_twins --types movie   # restrict type
    python -m app.scripts.dedup_resolved_twins --db /data/plexhub.db

`--apply` makes a timestamped sqlite backup (online .backup API) next to the DB
first. Run with the backend STOPPED (or at least no sync in progress).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from app.utils.string_normalizer import clean_title, normalize_for_sorting
from app.utils.unification import (
    calculate_history_group_key,
    calculate_unification_id,
)


def _norm_key(title: str) -> tuple[str, int | None]:
    """(normalized clean title, year) — the safe exact-match key."""
    clean, year = clean_title(title or "")
    return normalize_for_sorting(clean), year


def _is_resolved(imdb, tmdb) -> bool:
    if imdb:
        return True
    t = str(tmdb).strip() if tmdb is not None else ""
    return t.isdigit() and t != "0"


def _backup(db_path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = db_path.with_name(f"{db_path.stem}.prededup-{stamp}.db")
    src = sqlite3.connect(str(db_path))
    try:
        with sqlite3.connect(str(dest)) as bck:
            src.backup(bck)
    finally:
        src.close()
    return dest


def _plan_for_type(con: sqlite3.Connection, media_type: str) -> list[dict]:
    rows = con.execute(
        "SELECT rating_key, server_id, title, year, imdb_id, tmdb_id, unification_id "
        "FROM media WHERE type = ?",
        (media_type,),
    ).fetchall()

    resolved_by_key: dict[tuple[str, int | None], tuple] = {}
    resolved_years_by_norm: dict[str, set] = {}
    unresolved: list[tuple] = []

    for rk, sid, title, year, imdb, tmdb, uid in rows:
        norm, parsed_year = _norm_key(title)
        y = year if year is not None else parsed_year
        if not norm:
            continue
        if _is_resolved(imdb, tmdb):
            # Prefer an imdb-bearing twin as the canonical source (matches the
            # imdb>tmdb priority in calculate_unification_id).
            cur = resolved_by_key.get((norm, y))
            if cur is None or (imdb and not cur[4]):
                resolved_by_key[(norm, y)] = (rk, sid, title, y, imdb, tmdb, uid)
            resolved_years_by_norm.setdefault(norm, set()).add(y)
        else:
            unresolved.append((rk, sid, title, y, norm))

    plan: list[dict] = []
    for rk, sid, title, y, norm in unresolved:
        match = None
        confidence = ""
        if y is not None and (norm, y) in resolved_by_key:
            match = resolved_by_key[(norm, y)]
            confidence = "exact-year"
        elif y is None:
            years = resolved_years_by_norm.get(norm, set())
            if len(years) == 1:
                match = resolved_by_key[(norm, next(iter(years)))]
                confidence = "no-year-unique"
        if match is None:
            continue
        _, _, src_title, _, imdb, tmdb, _ = match
        new_uid = calculate_unification_id(title, y, imdb, str(tmdb) if tmdb else None)
        if not new_uid:
            continue
        plan.append({
            "rating_key": rk, "server_id": sid, "title": title, "year": y,
            "src_title": src_title, "imdb_id": imdb, "tmdb_id": str(tmdb) if tmdb else None,
            "unification_id": new_uid,
            "history_group_key": calculate_history_group_key(new_uid, rk, sid),
            "confidence": confidence,
        })
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="path to plexhub.db (default: settings.DB_PATH)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--types", default="movie,show", help="comma list: movie,show")
    args = ap.parse_args(argv)

    if args.db:
        db_path = Path(args.db)
    else:
        from app.config import settings
        db_path = Path(settings.DB_PATH)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    con = sqlite3.connect(str(db_path))
    try:
        full_plan: list[dict] = []
        for t in types:
            plan = _plan_for_type(con, t)
            full_plan.extend(plan)
            print(f"\n=== {t}: {len(plan)} unresolved rows would be linked to a resolved twin ===")
            for p in plan[:25]:
                print(f"  [{p['confidence']:>14}] {p['title']!r}({p['year']}) "
                      f"-> {p['src_title']!r} => {p['unification_id']}")
            if len(plan) > 25:
                print(f"  … +{len(plan) - 25} more")

        if not args.apply:
            print(f"\nDRY-RUN — {len(full_plan)} change(s) NOT written. Re-run with --apply.")
            return 0

        if full_plan:
            backup = _backup(db_path)
            print(f"\nBackup written: {backup}")
            now = int(time.time() * 1000)
            with con:
                con.executemany(
                    "UPDATE media SET imdb_id=?, tmdb_id=?, unification_id=?, "
                    "history_group_key=?, updated_at=? "
                    "WHERE rating_key=? AND server_id=? "
                    "AND (imdb_id IS NULL OR imdb_id='') "
                    "AND (tmdb_id IS NULL OR tmdb_id='' OR tmdb_id='0')",
                    [(p["imdb_id"], p["tmdb_id"], p["unification_id"],
                      p["history_group_key"], now, p["rating_key"], p["server_id"])
                     for p in full_plan],
                )
        print(f"\nAPPLIED — {len(full_plan)} row(s) linked to their resolved twin.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
