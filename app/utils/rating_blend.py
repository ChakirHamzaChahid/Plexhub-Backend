"""D-BLEND — single source of truth for the blended `Media.display_rating`.

Locked product decision (see
`docs/plans/2026-07-20-omdb-rating-enrichment-design.md` § Locked product
decisions): `display_rating` becomes `blend(imdb_rating, tmdb_rating)`.
A value `<= 0` (or `NULL`) counts as ABSENT — mirrors the Android
`blendRating` "ignore <= 0" rule. Both present -> average; exactly one
present -> that one; none present -> unchanged (`None` in Python, a no-op
in SQL).

This module ships BOTH a pure Python function (`blend_rating`) and a
SQLAlchemy `case()` mirror (`blend_display_rating_case`) so the exact same
rule can run in-process (per-item enrichment) and as a bulk `UPDATE`
(`recompute_display_rating_stmt`, self-healing recompute). A parity test
(`tests/test_rating_blend.py`) proves the two agree over the full value
grid, including `NULL`/`0`/negative edge cases.
"""
from __future__ import annotations

from sqlalchemy import and_, case, update
from sqlalchemy.sql.elements import ColumnElement

from app.models.database import Media


def blend_rating(imdb: float | None, tmdb: float | None) -> float | None:
    """D-BLEND. A value <= 0 OR None counts as ABSENT (mirrors the Android
    `blendRating` "ignore <= 0"). Both present -> (imdb + tmdb) / 2; exactly
    one present -> that one; none present -> None (caller must leave
    `display_rating` untouched)."""
    imdb_present = imdb is not None and imdb > 0
    tmdb_present = tmdb is not None and tmdb > 0
    if imdb_present and tmdb_present:
        return (imdb + tmdb) / 2
    if imdb_present:
        return imdb
    if tmdb_present:
        return tmdb
    return None


def blend_display_rating_case(
    imdb_expr: ColumnElement, tmdb_expr: ColumnElement, current_expr: ColumnElement
) -> ColumnElement:
    """SQLAlchemy `case()` mirroring `blend_rating` over two column/bindparam
    expressions. Returns `current_expr` (no-op) when BOTH are absent
    (<=0 / NULL) — same "leave display_rating untouched" contract as the
    Python `None` return.

    NULL-safety: SQL `col > 0` evaluates to NULL (not False) when `col` IS
    NULL, so "present" is built explicitly as `col IS NOT NULL AND col > 0`
    rather than relying on `col > 0` alone.
    """
    imdb_present = and_(imdb_expr.isnot(None), imdb_expr > 0)
    tmdb_present = and_(tmdb_expr.isnot(None), tmdb_expr > 0)
    return case(
        (and_(imdb_present, tmdb_present), (imdb_expr + tmdb_expr) / 2.0),
        (imdb_present, imdb_expr),
        (tmdb_present, tmdb_expr),
        else_=current_expr,
    )


def recompute_display_rating_stmt():
    """SQL-only, no network: heals `display_rating` from the durable
    `imdb_rating`/`tmdb_rating` columns (self-healing recompute — see design
    doc "Risks: content_hash-flip clobber"). Scoped to `movie`/`show` rows
    that have at least one usable rating, so rows where both are absent
    (a true no-op per `blend_display_rating_case`) are skipped entirely."""
    return (
        update(Media)
        .where(
            Media.type.in_(("movie", "show")),
            (Media.imdb_rating > 0) | (Media.tmdb_rating > 0),
        )
        .values(
            display_rating=blend_display_rating_case(
                Media.imdb_rating, Media.tmdb_rating, Media.display_rating
            )
        )
    )
