"""Parity + grid tests for app.utils.rating_blend (D-BLEND).

Two things get proven here:
1. `blend_rating` matches the exact grid from the design doc
   (`docs/plans/2026-07-20-omdb-rating-enrichment-design.md` § Locked product
   decisions), including the `<=0`/`None` "absent" edge cases.
2. `blend_display_rating_case` (SQL) agrees with `blend_rating` (Python) over
   the SAME grid when run as a bulk `recompute_display_rating_stmt()` UPDATE
   against a real (in-memory) SQLite engine — the SQL<->fn parity guarantee
   this module exists to provide.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.database import Media
from app.utils.rating_blend import blend_rating, recompute_display_rating_stmt

# NOTE: no module-level `pytestmark = pytest.mark.asyncio` here — this file
# mixes plain sync tests (the pure-function grid) with async DB tests, and
# `asyncio_mode = "auto"` (pyproject.toml) already picks up `async def`
# tests without a marker; forcing the marker onto sync tests emits a
# PytestWarning.


# ─── Pure function grid ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "imdb, tmdb, expected",
    [
        (7.0, 8.0, 7.5),      # both present -> average
        (7.0, None, 7.0),     # only imdb present
        (None, 8.0, 8.0),     # only tmdb present
        (None, None, None),   # none present
        (0.0, 0.0, None),     # both exactly 0 -> absent
        (-1.0, None, None),   # negative -> absent
        (0.0, 8.0, 8.0),      # imdb <=0 counts as absent -> tmdb wins
        (7.0, 0.0, 7.0),      # tmdb <=0 counts as absent -> imdb wins
    ],
)
def test_blend_rating_grid(imdb, tmdb, expected):
    assert blend_rating(imdb, tmdb) == expected


# ─── SQL <-> fn parity ─────────────────────────────────────────────────────

# Initial `display_rating` every grid row starts with — distinct from any
# blended value in the grid, so a wrongly no-op'd (or wrongly touched) row
# is unambiguous in assertions below.
_INITIAL_DISPLAY = 3.3

# (rating_key, type, imdb_rating, tmdb_rating) — covers the full
# `blend_rating` grid plus a non-movie/show control row (must stay
# untouched regardless of its imdb/tmdb values, since
# `recompute_display_rating_stmt` scopes to `type IN ('movie', 'show')`).
_GRID: list[tuple[str, str, float | None, float | None]] = [
    ("both", "movie", 7.0, 8.0),
    ("imdb_only", "movie", 7.0, None),
    ("tmdb_only", "show", None, 8.0),
    ("none", "movie", None, None),
    ("zero_zero", "show", 0.0, 0.0),
    ("negative", "movie", -1.0, None),
    ("imdb_zero", "show", 0.0, 8.0),
    ("tmdb_zero", "movie", 7.0, 0.0),
    ("wrong_type", "episode", 7.0, 8.0),
]


def _row(
    rating_key: str, type_: str, imdb: float | None, tmdb: float | None, page_offset: int
) -> Media:
    return Media(
        rating_key=rating_key,
        server_id="acct1",
        filter="all",
        sort_order="default",
        library_section_id="xtream_vod",
        # `uix_media_pagination` is unique on (server_id, library_section_id,
        # filter, sort_order, page_offset) — distinct per row so the grid
        # rows (which all share the other four columns) don't collide.
        page_offset=page_offset,
        title=rating_key,
        type=type_,
        imdb_rating=imdb,
        tmdb_rating=tmdb,
        display_rating=_INITIAL_DISPLAY,
    )


async def _seed_grid(db_session):
    for i, (rating_key, type_, imdb, tmdb) in enumerate(_GRID):
        db_session.add(_row(rating_key, type_, imdb, tmdb, page_offset=i))
    await db_session.commit()


async def _display_rating(db_session, rating_key: str) -> float:
    row = (await db_session.execute(
        select(Media).where(Media.rating_key == rating_key)
    )).scalars().one()
    return row.display_rating


async def test_recompute_display_rating_stmt_matches_blend_rating(db_session):
    await _seed_grid(db_session)

    await db_session.execute(recompute_display_rating_stmt())
    await db_session.commit()
    # Objects inserted above may still carry pre-update Python-side state
    # (fixture uses expire_on_commit=False) — force a DB re-read so the
    # assertions below see the actual persisted values, not stale cache.
    db_session.expire_all()

    for rating_key, type_, imdb, tmdb in _GRID:
        actual = await _display_rating(db_session, rating_key)

        if type_ not in ("movie", "show"):
            # Out of scope for the recompute -> always untouched.
            assert actual == _INITIAL_DISPLAY, rating_key
            continue

        expected = blend_rating(imdb, tmdb)
        if expected is None:
            # Both absent -> no-op, original value preserved.
            assert actual == _INITIAL_DISPLAY, rating_key
        else:
            assert actual == expected, rating_key


async def test_recompute_display_rating_stmt_is_idempotent(db_session):
    """A second run over already-recomputed rows must not change anything:
    the statement derives `display_rating` purely from `imdb_rating`/
    `tmdb_rating`, neither of which it mutates."""
    await _seed_grid(db_session)

    await db_session.execute(recompute_display_rating_stmt())
    await db_session.commit()
    db_session.expire_all()

    first_pass = {rk: await _display_rating(db_session, rk) for rk, *_ in _GRID}

    await db_session.execute(recompute_display_rating_stmt())
    await db_session.commit()
    db_session.expire_all()

    second_pass = {rk: await _display_rating(db_session, rk) for rk, *_ in _GRID}

    assert first_pass == second_pass
