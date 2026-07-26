"""S2.4 (AUDIT-P3-002, visibility only) — `plexhub_unified_path_total{path}`.

`media_service.get_unified_list` must increment exactly one of the 3
zero-initialised series declared in `app/utils/metrics.py` (S1.4) on every
call, so a production dashboard can distinguish:
  - "snapshot"      — the fast, intended path (precomputed media_group).
  - "live_filtered" — EXPECTED live aggregation: the request itself carries a
    filter the snapshot can't serve (search/genre/year).
  - "live_fallback" — ANOMALOUS live aggregation: an unfiltered request that
    *should* have hit the snapshot fell through because it is empty/never
    built for this media_type — the silent-degradation scenario the finding
    is about.

See `docs/plans/2026-07-26-refacto-audit-v1-plan.md` §VAGUE 2 S2.4.
"""
from __future__ import annotations

import pytest_asyncio

from app.models.database import Media, XtreamAccount
from app.services import media_service as media_service_module
from app.services import unified_group_service
from app.services.media_service import media_service
from app.utils import metrics
from app.utils.server_id import build_server_id

# pytest-asyncio auto mode.


def _movie(account_id, rk, title, unif, added, page_offset, *, tmdb=None):
    return Media(
        rating_key=rk, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=1999, unification_id=unif,
        tmdb_id=tmdb, added_at=added, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


@pytest_asyncio.fixture(autouse=True)
def _clear_cache():
    """The live path caches grouped results; clear it so one test's live call
    never serves another test's cached entry."""
    media_service_module._unified_groups_cache.clear()
    yield
    media_service_module._unified_groups_cache.clear()


@pytest_asyncio.fixture
async def seeded(db_session):
    db_session.add_all([
        XtreamAccount(
            id="a", label="Compte 1", base_url="http://a.example", port=80,
            username="u", password="p", is_active=True, created_at=0,
        ),
        _movie("a", "vod_1.mp4", "Film Un", "tmdb://10", 200, 0, tmdb="10"),
        _movie("a", "vod_2.mp4", "Film Deux", "tmdb://20", 100, 1, tmdb="20"),
    ])
    await db_session.commit()
    return db_session


def _count(path: str) -> float:
    return metrics.unified_path_total.labels(path=path)._value.get()


async def test_snapshot_hit_increments_snapshot_only(seeded):
    """Snapshot built + unfiltered query -> exactly the "snapshot" series moves."""
    db = seeded
    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    media_service_module._unified_groups_cache.clear()

    before = {p: _count(p) for p in metrics._UNIFIED_PATHS}
    groups, total = await media_service.get_unified_list(db, "movie", limit=50)
    after = {p: _count(p) for p in metrics._UNIFIED_PATHS}

    assert total == 2
    assert after["snapshot"] == before["snapshot"] + 1
    assert after["live_filtered"] == before["live_filtered"]
    assert after["live_fallback"] == before["live_fallback"]


async def test_filtered_query_increments_live_filtered_only(seeded):
    """A search/genre/year filter is EXPECTED to bypass the snapshot — this
    must NOT be counted as the anomalous fallback."""
    db = seeded
    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    media_service_module._unified_groups_cache.clear()

    before = {p: _count(p) for p in metrics._UNIFIED_PATHS}
    await media_service.get_unified_list(db, "movie", limit=50, year=1999)
    after = {p: _count(p) for p in metrics._UNIFIED_PATHS}

    assert after["live_filtered"] == before["live_filtered"] + 1
    assert after["snapshot"] == before["snapshot"]
    assert after["live_fallback"] == before["live_fallback"]


async def test_empty_snapshot_increments_live_fallback_only(seeded):
    """No media_group rows for this media_type (fresh DB / failed rebuild) —
    an unfiltered query falls through to live aggregation: the ANOMALOUS case
    that must be distinguishable from live_filtered."""
    db = seeded
    # No unified_group_service.rebuild(...) call: snapshot stays empty.

    before = {p: _count(p) for p in metrics._UNIFIED_PATHS}
    groups, total = await media_service.get_unified_list(db, "movie", limit=50)
    after = {p: _count(p) for p in metrics._UNIFIED_PATHS}

    assert total == 2  # live path still returns correct data
    assert after["live_fallback"] == before["live_fallback"] + 1
    assert after["snapshot"] == before["snapshot"]
    assert after["live_filtered"] == before["live_filtered"]


async def test_search_query_also_counts_as_live_filtered(seeded):
    """genre/search share the same "expected" bucket as year — not a 3rd path."""
    db = seeded
    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    media_service_module._unified_groups_cache.clear()

    before = _count("live_filtered")
    await media_service.get_unified_list(db, "movie", limit=50, search="Film")
    assert _count("live_filtered") == before + 1
