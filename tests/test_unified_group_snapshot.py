"""CR-P01 (true fix): the media_group snapshot + snapshot-backed unified list.

Guarantees:
  1. PARITY — the snapshot read path returns EXACTLY what the live aggregation
     path returns (same groups, same order, same members/best), including the
     split-identity convergence (imdb:// vs tmdb:// twins fold into one group).
  2. FAST PATH — when the snapshot exists, the read path does NOT fall back to
     the whole-catalog live aggregation.
  3. FALLBACK — an empty snapshot (fresh DB before the first build) transparently
     uses the live path, so browsing is correct before the first pipeline build.
  4. BUILDER — rebuild/rebuild_all populate both tables and are idempotent
     (re-running replaces, never duplicates).
  5. PAGINATION — snapshot paging (limit/offset) + total match the live path.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import func, select

from app.models.database import Media, MediaGroup, MediaGroupMember, XtreamAccount
from app.services import media_service as media_service_module
from app.services import unified_group_service
from app.services.media_service import media_service
from app.utils.server_id import build_server_id

# pytest-asyncio auto mode.


def _movie(account_id, rk, title, unif, added, page_offset, *, imdb=None, tmdb=None):
    return Media(
        rating_key=rk, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=1999, unification_id=unif,
        imdb_id=imdb, tmdb_id=tmdb, added_at=added, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


def _show(account_id, rk, title, unif, added, page_offset):
    return Media(
        rating_key=rk, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=title, type="show", year=2008, unification_id=unif,
        added_at=added, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


@pytest_asyncio.fixture(autouse=True)
def _clear_cache():
    """The live path caches grouped results; clear it so a live-vs-snapshot
    comparison in one test never serves a stale live entry."""
    media_service_module._unified_groups_cache.clear()
    yield
    media_service_module._unified_groups_cache.clear()


@pytest_asyncio.fixture
async def seeded(db_session):
    """3 movies: a split-identity pair (imdb://+tmdb:// share tmdb:10 -> one
    converged group) plus a standalone, and 2 shows."""
    db_session.add_all([
        XtreamAccount(
            id="a", label="Compte 1", base_url="http://a.example", port=80,
            username="u", password="p", is_active=True, created_at=0,
        ),
        _movie("a", "vod_1.mp4", "The Film", "imdb://tt0001", 200, 0,
               imdb="tt0001", tmdb="10"),
        _movie("a", "vod_2.mp4", "The Film VF", "tmdb://10", 100, 1, tmdb="10"),
        _movie("a", "vod_3.mp4", "Other", "tmdb://20", 300, 2, tmdb="20"),
        _show("a", "series_1", "Show A", "tmdb://1396", 500, 3),
        _show("a", "series_2", "Show B", "tmdb://1399", 400, 4),
    ])
    await db_session.commit()
    return db_session


def _summarize(groups):
    """Comparable shape: (key, best pk, sorted member pks) per group, in order."""
    return [
        (
            g.key,
            (g.best.server_id, g.best.rating_key),
            sorted((m.server_id, m.rating_key) for m in g.members),
        )
        for g in groups
    ]


# ─── builder ────────────────────────────────────────────────────────────────


async def test_rebuild_populates_snapshot(seeded):
    db = seeded
    n = await unified_group_service.rebuild(db, "movie")
    await db.commit()
    assert n == 2  # split pair converges -> 2 groups from 3 rows

    group_count = (await db.execute(
        select(func.count()).select_from(MediaGroup).where(MediaGroup.media_type == "movie")
    )).scalar()
    member_count = (await db.execute(
        select(func.count()).select_from(MediaGroupMember).where(MediaGroupMember.media_type == "movie")
    )).scalar()
    assert group_count == 2
    assert member_count == 3  # all 3 movie rows are members


async def test_rebuild_is_idempotent(seeded):
    db = seeded
    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    group_count = (await db.execute(
        select(func.count()).select_from(MediaGroup).where(MediaGroup.media_type == "movie")
    )).scalar()
    member_count = (await db.execute(
        select(func.count()).select_from(MediaGroupMember).where(MediaGroupMember.media_type == "movie")
    )).scalar()
    assert group_count == 2  # replaced, not duplicated
    assert member_count == 3


async def test_rebuild_all_covers_movies_and_shows(seeded, db_factory):
    counts = await unified_group_service.rebuild_all(db_factory)
    assert counts == {"movie": 2, "show": 2}


# ─── parity: snapshot path == live path ─────────────────────────────────────


async def test_snapshot_matches_live_movies(seeded):
    db = seeded
    # Live result first (snapshot empty -> fallback to live aggregation).
    live_groups, live_total = await media_service.get_unified_list(db, "movie", limit=50)

    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    media_service_module._unified_groups_cache.clear()

    snap_groups, snap_total = await media_service.get_unified_list(db, "movie", limit=50)

    assert snap_total == live_total == 2
    assert _summarize(snap_groups) == _summarize(live_groups)


async def test_snapshot_preserves_split_identity_convergence(seeded):
    """The imdb:// + tmdb:// twins (sharing tmdb:10) must be ONE group in the
    snapshot too — proving the builder runs _converge and the page re-aggregation
    keeps them merged."""
    db = seeded
    await unified_group_service.rebuild(db, "movie")
    await db.commit()

    groups, total = await media_service.get_unified_list(db, "movie", limit=50)
    assert total == 2
    by_key = {g.key: g for g in groups}
    assert "imdb://tt0001" in by_key
    converged = by_key["imdb://tt0001"]
    assert sorted((m.server_id, m.rating_key) for m in converged.members) == [
        (build_server_id("a"), "vod_1.mp4"),
        (build_server_id("a"), "vod_2.mp4"),
    ]
    assert "tmdb://10" not in by_key  # the twin key is absorbed, not a 2nd group


# ─── fast path / fallback ───────────────────────────────────────────────────


async def test_empty_snapshot_falls_back_to_live(seeded):
    """No media_group rows -> _unified_list_from_snapshot returns None and the
    live path runs (correct result on a fresh DB before the first build)."""
    db = seeded
    result = await media_service._unified_list_from_snapshot(db, "movie", 50, 0)
    assert result is None
    groups, total = await media_service.get_unified_list(db, "movie", limit=50)
    assert total == 2  # live path still works


async def test_populated_snapshot_does_not_run_live_aggregation(seeded, monkeypatch):
    """With the snapshot built, the read path must NOT call the whole-catalog
    live sort/aggregate helper."""
    db = seeded
    await unified_group_service.rebuild(db, "movie")
    await db.commit()

    def _boom(_rows):
        raise AssertionError("live whole-catalog aggregation ran despite a populated snapshot")

    monkeypatch.setattr(media_service_module, "_aggregate_and_sort_movies", _boom)

    groups, total = await media_service.get_unified_list(db, "movie", limit=50)
    assert total == 2  # served from the snapshot, live helper never called


async def test_filtered_query_uses_live_even_with_snapshot(seeded, monkeypatch):
    """A search/genre/year filter must bypass the snapshot (filtering changes
    group membership) and use the live path."""
    db = seeded
    await unified_group_service.rebuild(db, "movie")
    await db.commit()

    called = {"snapshot": False}
    real = media_service._unified_list_from_snapshot

    async def _spy(self, *a, **k):
        called["snapshot"] = True
        return await real(*a, **k)

    monkeypatch.setattr(
        media_service_module.MediaService, "_unified_list_from_snapshot", _spy,
    )
    # A year filter -> live path, snapshot helper must not be consulted.
    await media_service.get_unified_list(db, "movie", limit=50, year=1999)
    assert called["snapshot"] is False


# ─── pagination parity ──────────────────────────────────────────────────────


async def test_snapshot_pagination_matches_live(seeded):
    db = seeded
    live_p1, _ = await media_service.get_unified_list(db, "movie", limit=1, offset=0)
    live_p2, _ = await media_service.get_unified_list(db, "movie", limit=1, offset=1)

    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    media_service_module._unified_groups_cache.clear()

    snap_p1, total1 = await media_service.get_unified_list(db, "movie", limit=1, offset=0)
    snap_p2, total2 = await media_service.get_unified_list(db, "movie", limit=1, offset=1)

    assert total1 == total2 == 2
    assert [g.key for g in snap_p1] == [g.key for g in live_p1]
    assert [g.key for g in snap_p2] == [g.key for g in live_p2]
    # No overlap across pages, full coverage.
    assert {g.key for g in snap_p1} | {g.key for g in snap_p2} == {"imdb://tt0001", "tmdb://20"}


# ─── regressions: real Media identity (filter = category_id), not (sid, rk) ──


def _cat_row(account_id, rk, cat, unif, *, allowed=True, added=100, tmdb=None):
    """A media row for one Xtream category (filter = category_id). The same
    physical item under N categories = N rows sharing (server_id, rating_key)
    but differing on `filter`."""
    return Media(
        rating_key=rk, server_id=build_server_id(account_id),
        filter=cat, sort_order="default", library_section_id="xtream_vod",
        title="Dup Film", type="movie", year=1999, unification_id=unif,
        tmdb_id=tmdb, added_at=added, page_offset=0,
        is_in_allowed_categories=allowed, is_broken=False,
    )


async def test_multi_category_duplicate_builds_and_matches_live(db_session):
    """CR-P01 regression: an item listed under 2 synced categories = 2 media
    rows sharing (server_id, rating_key), same unification_id -> 2 members. The
    builder must store ONE member pointer (no media_group_member PK collision)
    and the read path must re-expand both variants -> byte-identical to live."""
    db = db_session
    db.add_all([
        XtreamAccount(
            id="a", label="C1", base_url="http://a", port=80,
            username="u", password="p", is_active=True, created_at=0,
        ),
        _cat_row("a", "vod_1.mp4", "cat1", "tmdb://10", tmdb="10"),
        _cat_row("a", "vod_1.mp4", "cat2", "tmdb://10", tmdb="10"),
        _movie("a", "vod_2.mp4", "Solo", "tmdb://20", 200, 5, tmdb="20"),
    ])
    await db.commit()

    live, live_total = await media_service.get_unified_list(db, "movie", limit=50)

    n = await unified_group_service.rebuild(db, "movie")  # must NOT raise
    await db.commit()
    assert n == 2

    member_count = (await db.execute(
        select(func.count()).select_from(MediaGroupMember).where(
            MediaGroupMember.group_key == "tmdb://10",
        )
    )).scalar()
    assert member_count == 1  # one pointer per (server_id, rating_key)

    media_service_module._unified_groups_cache.clear()
    snap, snap_total = await media_service.get_unified_list(db, "movie", limit=50)
    assert snap_total == live_total == 2
    assert _summarize(snap) == _summarize(live)  # both variants re-expanded


async def test_hydration_excludes_non_allowed_variant(db_session):
    """CR-P01 parity: an item with one allowed + one non-allowed category row.
    The snapshot points at the allowed one; hydration must NOT re-inflate the
    non-allowed twin (it re-applies type + is_in_allowed_categories, like live)."""
    db = db_session
    db.add_all([
        XtreamAccount(
            id="a", label="C1", base_url="http://a", port=80,
            username="u", password="p", is_active=True, created_at=0,
        ),
        _cat_row("a", "vod_1.mp4", "cat_ok", "tmdb://10", allowed=True, tmdb="10"),
        _cat_row("a", "vod_1.mp4", "cat_hidden", "tmdb://10", allowed=False, tmdb="10"),
    ])
    await db.commit()

    live, live_total = await media_service.get_unified_list(db, "movie", limit=50)
    assert live_total == 1
    assert len(live[0].members) == 1  # only the allowed row

    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    media_service_module._unified_groups_cache.clear()

    snap, snap_total = await media_service.get_unified_list(db, "movie", limit=50)
    assert snap_total == 1
    assert len(snap[0].members) == 1  # non-allowed twin NOT re-inflated
    assert _summarize(snap) == _summarize(live)


async def test_pass_b_title_absorption_parity(db_session):
    """Convergence Pass B: an unresolved `title_…` twin absorbed into an
    id-based group of the same canonical title+year must fold identically in the
    snapshot (its rows are stored under the winner key, so re-aggregation keeps
    them merged)."""
    from app.utils.unification import calculate_unification_id

    db = db_session
    tkey = calculate_unification_id("Movie Z", 2005)  # the title_ twin's key
    db.add_all([
        XtreamAccount(
            id="a", label="C1", base_url="http://a", port=80,
            username="u", password="p", is_active=True, created_at=0,
        ),
        Media(
            rating_key="vod_1.mp4", server_id=build_server_id("a"),
            filter="all", sort_order="default", library_section_id="xtream_vod",
            title="Movie Z", type="movie", year=2005, unification_id="tmdb://30",
            tmdb_id="30", added_at=100, page_offset=0,
            is_in_allowed_categories=True, is_broken=False,
        ),
        Media(
            rating_key="vod_2.mp4", server_id=build_server_id("a"),
            filter="all", sort_order="default", library_section_id="xtream_vod",
            title="Movie Z", type="movie", year=2005, unification_id=tkey,
            added_at=90, page_offset=1,
            is_in_allowed_categories=True, is_broken=False,
        ),
    ])
    await db.commit()

    live, live_total = await media_service.get_unified_list(db, "movie", limit=50)
    assert live_total == 1  # title twin absorbed into the id group

    await unified_group_service.rebuild(db, "movie")
    await db.commit()
    media_service_module._unified_groups_cache.clear()

    snap, snap_total = await media_service.get_unified_list(db, "movie", limit=50)
    assert snap_total == 1
    assert _summarize(snap) == _summarize(live)
