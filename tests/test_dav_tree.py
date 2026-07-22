"""Tests for app/dav/vfs.py (DavTree/DavTreeCache) and app/dav/tree_builder.py.

The tree-builder tests seed a real (in-memory) DB and monkeypatch
`app.plex_generator.source.async_session_factory`, mirroring
`tests/test_plex_dedup.py`'s `TestUnifiedDatabaseSource.seeded_factory`
pattern — `build_dav_tree()` goes through the exact same `DatabaseSource`
code path the `.strm` generator uses, so this is the most direct way to
prove tree/`.strm` parity (same aggregation, same DB state).
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.dav.tree_builder import build_dav_tree
from app.dav.vfs import DavTreeCache
from app.models.database import Media, XtreamAccount
from app.plex_generator.generator import resolve_movie_names, resolve_series_names
from app.plex_generator.naming import movie_path, movie_version_path, series_episode_path
from app.plex_generator.source import DatabaseSource
from app.utils.server_id import build_server_id

# ─── Fixture builders ────────────────────────────────────────────────────


def _account(id_: str, label: str) -> XtreamAccount:
    return XtreamAccount(
        id=id_, label=label, base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


def _movie_row(
    account_id: str, rating_key: str, title: str, unif: str = "",
    page_offset: int = 0, year: int | None = 2020,
    file_size: int | None = 5_000_000, is_adult: bool = False,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=year, unification_id=unif,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
        file_size=file_size, is_adult=is_adult,
    )


def _show_row(
    account_id: str, rating_key: str, title: str, unif: str = "",
    page_offset: int = 0, year: int | None = 2020,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=title, type="show", year=year, unification_id=unif,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
    )


def _episode_row(
    account_id: str, rating_key: str, show_rating_key: str, title: str,
    season: int, episode: int, page_offset: int = 0,
    file_size: int | None = 3_000_000,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=title, type="episode", grandparent_rating_key=show_rating_key,
        parent_index=season, index=episode, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False, file_size=file_size,
        unification_id="",
    )


@pytest_asyncio.fixture
async def seeded(db_engine, monkeypatch):
    """Seed the in-memory DB and point `DatabaseSource` at it (mirrors
    `tests/test_plex_dedup.py`'s `TestUnifiedDatabaseSource.seeded_factory`).
    Returns an async `seed(rows)` callable so each test controls its own
    fixture rows."""

    async def _seed(rows):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add_all(rows)
            await s.commit()

        import app.plex_generator.source as source_mod

        monkeypatch.setattr(source_mod, "async_session_factory", factory)
        return factory

    return _seed


@pytest.fixture(autouse=True)
def _default_dav_settings(monkeypatch):
    """Unbounded caps by default (individual tests override to exercise the
    cap explicitly) — the phase-1 defaults (25/5) would silently truncate
    fixtures unrelated to the cap tests themselves."""
    monkeypatch.setattr(settings, "DAV_MOVIE_LIMIT", 0)
    monkeypatch.setattr(settings, "DAV_SERIES_LIMIT", 0)


# ─── Extension swap ──────────────────────────────────────────────────────


class TestExtensionSwap:
    async def test_mp4_extension_swapped_from_rating_key(self, seeded):
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Dune", year=2021, file_size=100),
        ])
        tree = await build_dav_tree()
        entry = tree.lookup("Films/Dune (2021)/Dune (2021).mp4")
        assert entry is not None
        assert not entry.is_dir
        assert entry.size == 100
        assert entry.server_id == build_server_id("a")
        assert entry.rating_key == "vod_1.mp4"
        # The .strm variant must NOT be present.
        assert tree.lookup("Films/Dune (2021)/Dune (2021).strm") is None

    async def test_mkv_extension_for_episode(self, seeded):
        await seeded([
            _account("a", "Compte 1"),
            _show_row("a", "series_1", "Breaking Bad", year=2008, page_offset=0),
            _episode_row("a", "ep_1.mkv", "series_1", "Pilot", season=1, episode=1, page_offset=1),
        ])
        tree = await build_dav_tree()
        entry = tree.lookup("Series/Breaking Bad (2008)/Season 01/Breaking Bad (2008) S01E01.mkv")
        assert entry is not None

    async def test_default_ts_extension_when_rating_key_has_none(self, seeded):
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1", "NoExt", year=2019, file_size=100),
        ])
        tree = await build_dav_tree()
        entry = tree.lookup("Films/NoExt (2019)/NoExt (2019).ts")
        assert entry is not None


# ─── Deterministic cap ───────────────────────────────────────────────────


class TestDeterministicCap:
    async def test_same_subset_produced_on_two_builds(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_MOVIE_LIMIT", 2)
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Alpha", year=2001, file_size=1, page_offset=0),
            _movie_row("a", "vod_2.mp4", "Bravo", year=2002, file_size=1, page_offset=1),
            _movie_row("a", "vod_3.mp4", "Charlie", year=2003, file_size=1, page_offset=2),
        ])
        tree1 = await build_dav_tree()
        tree2 = await build_dav_tree()
        files1 = sorted(p for p, e in tree1.entries.items() if not e.is_dir)
        files2 = sorted(p for p, e in tree2.entries.items() if not e.is_dir)
        assert files1 == files2
        assert len(files1) == 2

    async def test_item_beyond_cap_is_absent(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_MOVIE_LIMIT", 1)
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Alpha", year=2001, file_size=1, page_offset=0),
            _movie_row("a", "vod_2.mp4", "Zeta", year=2002, file_size=1, page_offset=1),
        ])
        tree = await build_dav_tree()
        assert tree.lookup("Films/Alpha (2001)/Alpha (2001).mp4") is not None
        assert tree.lookup("Films/Zeta (2002)/Zeta (2002).mp4") is None

    async def test_zero_limit_means_unlimited(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_MOVIE_LIMIT", 0)
        await seeded([
            _account("a", "Compte 1"),
            *[
                _movie_row("a", f"vod_{i}.mp4", f"Movie {i:02d}", year=2000 + i,
                           file_size=1, page_offset=i)
                for i in range(5)
            ],
        ])
        tree = await build_dav_tree()
        files = [p for p, e in tree.entries.items() if not e.is_dir]
        assert len(files) == 5

    async def test_series_cap_truncates_shows(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_SERIES_LIMIT", 1)
        await seeded([
            _account("a", "Compte 1"),
            _show_row("a", "series_1", "Alpha Show", year=2001, page_offset=0),
            _episode_row("a", "ep_1.mkv", "series_1", "Pilot", season=1, episode=1, page_offset=1),
            _show_row("a", "series_2", "Zeta Show", year=2002, page_offset=2),
            _episode_row("a", "ep_2.mkv", "series_2", "Pilot", season=1, episode=1, page_offset=3),
        ])
        tree = await build_dav_tree()
        assert tree.lookup("Series/Alpha Show (2001)/Season 01/Alpha Show (2001) S01E01.mkv") is not None
        assert tree.lookup("Series/Zeta Show (2002)/Season 01/Zeta Show (2002) S01E01.mkv") is None


# ─── NULL-size exclusion ─────────────────────────────────────────────────


class TestNullSizeExclusion:
    async def test_version_without_known_size_excluded_by_default(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_REQUIRE_KNOWN_SIZE", True)
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "NoSize", year=2020, file_size=None),
        ])
        tree = await build_dav_tree()
        assert tree.lookup("Films/NoSize (2020)/NoSize (2020).mp4") is None
        # The whole group had zero eligible versions -> never published at all.
        assert tree.list_dir("Films") in (None, [])

    async def test_require_known_size_false_keeps_unknown_size_version(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_REQUIRE_KNOWN_SIZE", False)
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "NoSize", year=2020, file_size=None),
        ])
        tree = await build_dav_tree()
        entry = tree.lookup("Films/NoSize (2020)/NoSize (2020).mp4")
        assert entry is not None
        assert entry.size is None

    async def test_episode_without_known_size_excluded(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_REQUIRE_KNOWN_SIZE", True)
        await seeded([
            _account("a", "Compte 1"),
            _show_row("a", "series_1", "Breaking Bad", year=2008, page_offset=0),
            _episode_row("a", "ep_1.mkv", "series_1", "Pilot", season=1, episode=1,
                         file_size=None, page_offset=1),
        ])
        tree = await build_dav_tree()
        assert tree.lookup("Series/Breaking Bad (2008)/Season 01/Breaking Bad (2008) S01E01.mkv") is None
        # The show's only episode had zero eligible versions -> the show is dropped too.
        assert tree.list_dir("Series") in (None, [])


# ─── Adult exclusion ─────────────────────────────────────────────────────


class TestAdultExclusion:
    async def test_adult_movie_excluded_by_default(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_INCLUDE_ADULT", False)
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Adult Film", year=2020, is_adult=True, file_size=1),
        ])
        tree = await build_dav_tree()
        assert tree.list_dir("Films") in (None, [])

    async def test_adult_movie_included_when_enabled(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_INCLUDE_ADULT", True)
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Adult Film", year=2020, is_adult=True, file_size=1),
        ])
        tree = await build_dav_tree()
        entry = tree.lookup("Films/[XXX] Adult Film (2020)/[XXX] Adult Film (2020).mp4")
        assert entry is not None

    async def test_non_adult_movie_never_filtered(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_INCLUDE_ADULT", False)
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Family Film", year=2020, is_adult=False, file_size=1),
        ])
        tree = await build_dav_tree()
        assert tree.lookup("Films/Family Film (2020)/Family Film (2020).mp4") is not None


# ─── Single-version pick ─────────────────────────────────────────────────


class TestSingleVersionPick:
    async def test_picks_largest_known_size_version(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_SINGLE_VERSION", True)
        await seeded([
            _account("a", "Compte 1"), _account("b", "Compte 2"),
            _movie_row("a", "vod_1.mp4", "Shared (VF)", unif="tmdb://1",
                       year=2020, file_size=1_000),
            _movie_row("b", "vod_9.mp4", "Shared (HD)", unif="tmdb://1",
                       year=2020, file_size=5_000),
        ])
        tree = await build_dav_tree()
        # Single surviving version -> non-versioned path (no " - label" suffix).
        entry = tree.lookup("Films/Shared (2020)/Shared (2020).mp4")
        assert entry is not None
        assert entry.size == 5_000
        assert entry.rating_key == "vod_9.mp4"
        # The smaller-size version is not published under its own path.
        assert tree.lookup("Films/Shared (2020)/Shared (2020) - VF · Compte 1.mp4") is None
        # Only ONE file for this group.
        listing = tree.list_dir("Films/Shared (2020)")
        assert [n for n, e in listing if not e.is_dir] == ["Shared (2020).mp4"]

    async def test_multi_version_kept_when_disabled(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_SINGLE_VERSION", False)
        await seeded([
            _account("a", "Compte 1"), _account("b", "Compte 2"),
            _movie_row("a", "vod_1.mp4", "Shared (VF)", unif="tmdb://1",
                       year=2020, file_size=1_000),
            _movie_row("b", "vod_9.mp4", "Shared (HD)", unif="tmdb://1",
                       year=2020, file_size=5_000),
        ])
        tree = await build_dav_tree()
        assert tree.lookup("Films/Shared (2020)/Shared (2020) - VF · Compte 1.mp4") is not None
        assert tree.lookup("Films/Shared (2020)/Shared (2020) - HD · Compte 2.mp4") is not None
        assert tree.lookup("Films/Shared (2020)/Shared (2020).mp4") is None

    async def test_multi_version_episode_kept_when_disabled(self, seeded, monkeypatch):
        # Same version-selection logic applies to episode slots, not just
        # movie groups — a show with the same (season, episode) sourced from
        # two accounts keeps both files when DAV_SINGLE_VERSION is off.
        # Two accounts merge into one SeriesGroup via a shared unification_id;
        # each account's episode is matched to ITS OWN show row (server_id +
        # grandparent_rating_key), so both shows need distinct rating_keys.
        monkeypatch.setattr(settings, "DAV_SINGLE_VERSION", False)
        await seeded([
            _account("a", "Compte 1"), _account("b", "Compte 2"),
            _show_row("a", "series_1", "Breaking Bad", unif="tmdb://100", year=2008, page_offset=0),
            _show_row("b", "series_9", "Breaking Bad", unif="tmdb://100", year=2008, page_offset=0),
            _episode_row("a", "ep_1.mkv", "series_1", "Pilot (VF)", season=1, episode=1,
                         page_offset=1, file_size=1_000),
            _episode_row("b", "ep_9.mkv", "series_9", "Pilot (HD)", season=1, episode=1,
                         page_offset=1, file_size=5_000),
        ])
        tree = await build_dav_tree()
        listing = tree.list_dir("Series/Breaking Bad (2008)/Season 01")
        assert listing is not None
        names = sorted(n for n, e in listing if not e.is_dir)
        assert len(names) == 2
        assert all("Breaking Bad (2008) S01E01 - " in n for n in names)

    async def test_single_version_tie_break_is_deterministic(self, seeded, monkeypatch):
        # Equal file_size -> tie-break on source_id (rating_key), same on
        # every rebuild regardless of row insertion/iteration order.
        monkeypatch.setattr(settings, "DAV_SINGLE_VERSION", True)
        await seeded([
            _account("a", "Compte 1"), _account("b", "Compte 2"),
            _movie_row("a", "vod_9.mp4", "Tied (VF)", unif="tmdb://2", year=2020, file_size=1_000),
            _movie_row("b", "vod_1.mp4", "Tied (HD)", unif="tmdb://2", year=2020, file_size=1_000),
        ])
        tree1 = await build_dav_tree()
        tree2 = await build_dav_tree()
        entry1 = tree1.lookup("Films/Tied (2020)/Tied (2020).mp4")
        entry2 = tree2.lookup("Films/Tied (2020)/Tied (2020).mp4")
        assert entry1 is not None
        assert entry1.rating_key == entry2.rating_key == "vod_1.mp4"  # smaller source_id wins ties


# ─── Naming parity with the .strm generator ──────────────────────────────


class TestNamingParityWithGenerator:
    """`build_dav_tree()` must produce paths byte-identical to what the
    `.strm` generator (`generator.resolve_movie_names`/`resolve_series_names`
    + `naming.*`) would produce for the SAME DatabaseSource output — the DAV
    extension aside."""

    async def test_single_version_movie_path_matches_strm(self, seeded):
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Dune", year=2021, file_size=100),
        ])
        movies = await DatabaseSource().get_movies()
        names = resolve_movie_names(movies)
        movie = movies[0]
        name = names[movie.source_id]
        strm_path = movie_path(name.clean_title, name.year, suffix=name.suffix, fallback_id=name.fallback_id)
        expected_dav_path = strm_path[: -len(".strm")] + ".mp4"

        tree = await build_dav_tree()
        assert tree.lookup(expected_dav_path) is not None
        assert expected_dav_path == "Films/Dune (2021)/Dune (2021).mp4"

    async def test_multi_version_movie_path_matches_strm_with_label(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_SINGLE_VERSION", False)
        await seeded([
            _account("a", "Compte 1"), _account("b", "Compte 2"),
            _movie_row("a", "vod_1.mp4", "Shared (VF)", unif="tmdb://1", year=2020, file_size=1_000),
            _movie_row("b", "vod_9.mp4", "Shared (HD)", unif="tmdb://1", year=2020, file_size=5_000),
        ])
        movies = await DatabaseSource().get_movies()
        names = resolve_movie_names(movies)
        movie = next(m for m in movies if m.source_id == "tmdb://1")
        name = names[movie.source_id]

        tree = await build_dav_tree()
        assert len(movie.versions) == 2
        for v in movie.versions:
            strm_path = movie_version_path(
                name.clean_title, name.year, v.label or v.source_id,
                suffix=name.suffix, fallback_id=name.fallback_id,
            )
            expected_dav_path = strm_path[: -len(".strm")] + ".mp4"
            assert tree.lookup(expected_dav_path) is not None, expected_dav_path

    async def test_adult_movie_path_matches_strm_with_xxx_tag(self, seeded, monkeypatch):
        monkeypatch.setattr(settings, "DAV_INCLUDE_ADULT", True)
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Naughty Film", year=2020, is_adult=True, file_size=1),
        ])
        movies = await DatabaseSource().get_movies()
        names = resolve_movie_names(movies)
        movie = movies[0]
        name = names[movie.source_id]
        strm_path = movie_path(name.clean_title, name.year, suffix=name.suffix, fallback_id=name.fallback_id)
        assert "[XXX]" in strm_path
        expected_dav_path = strm_path[: -len(".strm")] + ".mp4"

        tree = await build_dav_tree()
        assert tree.lookup(expected_dav_path) is not None

    async def test_episode_path_matches_strm(self, seeded):
        await seeded([
            _account("a", "Compte 1"),
            _show_row("a", "series_1", "Breaking Bad", year=2008, page_offset=0),
            _episode_row("a", "ep_1.mkv", "series_1", "Pilot", season=1, episode=1,
                         file_size=100, page_offset=1),
        ])
        series_list = await DatabaseSource().get_series()
        names = resolve_series_names(series_list)
        series = series_list[0]
        name = names[series.source_id]
        ep = series.episodes[0]
        strm_path = series_episode_path(
            name.clean_title, ep.season_num, ep.episode_num,
            year=name.year, suffix=name.suffix, fallback_id=name.fallback_id,
        )
        expected_dav_path = strm_path[: -len(".strm")] + ".mkv"

        tree = await build_dav_tree()
        assert tree.lookup(expected_dav_path) is not None
        assert expected_dav_path == "Series/Breaking Bad (2008)/Season 01/Breaking Bad (2008) S01E01.mkv"


# ─── DavTree navigation helpers ──────────────────────────────────────────


class TestDavTreeNavigation:
    async def test_list_dir_root_lists_top_level_dirs(self, seeded):
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Dune", year=2021, file_size=100),
            _show_row("a", "series_1", "Breaking Bad", year=2008, page_offset=0),
            _episode_row("a", "ep_1.mkv", "series_1", "Pilot", season=1, episode=1, page_offset=1),
        ])
        tree = await build_dav_tree()
        listing = tree.list_dir("")
        assert listing is not None
        names = sorted(name for name, _entry in listing)
        assert names == ["Films", "Series"]
        for _name, entry in listing:
            assert entry.is_dir

    async def test_list_dir_unknown_path_returns_none(self, seeded):
        await seeded([_account("a", "Compte 1")])
        tree = await build_dav_tree()
        assert tree.list_dir("Nope") is None

    async def test_list_dir_on_a_file_returns_none(self, seeded):
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Dune", year=2021, file_size=100),
        ])
        tree = await build_dav_tree()
        assert tree.list_dir("Films/Dune (2021)/Dune (2021).mp4") is None

    async def test_lookup_unknown_path_returns_none(self, seeded):
        await seeded([_account("a", "Compte 1")])
        tree = await build_dav_tree()
        assert tree.lookup("nope/nope.mkv") is None

    async def test_top_level_children_are_sorted(self, seeded):
        await seeded([
            _account("a", "Compte 1"),
            _movie_row("a", "vod_1.mp4", "Zeta", year=2001, file_size=1, page_offset=0),
            _movie_row("a", "vod_2.mp4", "Alpha", year=2002, file_size=1, page_offset=1),
        ])
        tree = await build_dav_tree()
        listing = tree.list_dir("Films")
        names = [name for name, _entry in listing]
        assert names == sorted(names)

    async def test_children_are_sorted_even_when_insertion_order_differs(self, seeded, monkeypatch):
        # Version insertion order is (server_id, rating_key) ascending — pick
        # server ids so that order is the OPPOSITE of alphabetical label
        # order, so only the tree builder's final children.sort() pass (not
        # insertion order) can make the listing alphabetical.
        monkeypatch.setattr(settings, "DAV_SINGLE_VERSION", False)
        await seeded([
            _account("1", "Compte Un"),   # server_id "xtream_1" sorts FIRST
            _account("2", "Compte Deux"),  # server_id "xtream_2" sorts SECOND
            _movie_row("1", "vod_1.mp4", "Multi (Zulu Label)", unif="tmdb://9",
                       year=2020, file_size=1),
            _movie_row("2", "vod_2.mp4", "Multi (Alpha Label)", unif="tmdb://9",
                       year=2020, file_size=1),
        ])
        tree = await build_dav_tree()
        listing = tree.list_dir("Films/Multi (2020)")
        assert listing is not None
        names = [name for name, _entry in listing]
        assert len(names) == 2
        assert names == sorted(names)
        # Not a tautology: without the final sort, insertion order would
        # have been [Zulu, Alpha] — confirm Alpha really precedes Zulu.
        assert "Alpha" in names[0]
        assert "Zulu" in names[1]


# ─── DavTreeCache: TTL / invalidate / single-flight ──────────────────────


class TestDavTreeCache:
    async def test_get_builds_once_and_caches(self, monkeypatch):
        import app.dav.tree_builder as tree_builder_mod
        from app.dav.vfs import DavTree

        call_count = {"n": 0}

        async def fake_build():
            call_count["n"] += 1
            return DavTree()

        monkeypatch.setattr(tree_builder_mod, "build_dav_tree", fake_build)
        cache = DavTreeCache()

        t1 = await cache.get()
        t2 = await cache.get()

        assert t1 is t2
        assert call_count["n"] == 1

    async def test_invalidate_forces_rebuild(self, monkeypatch):
        import app.dav.tree_builder as tree_builder_mod
        from app.dav.vfs import DavTree

        call_count = {"n": 0}

        async def fake_build():
            call_count["n"] += 1
            return DavTree()

        monkeypatch.setattr(tree_builder_mod, "build_dav_tree", fake_build)
        cache = DavTreeCache()

        await cache.get()
        cache.invalidate()
        await cache.get()

        assert call_count["n"] == 2

    async def test_ttl_expiry_triggers_rebuild(self, monkeypatch):
        import app.dav.tree_builder as tree_builder_mod
        from app.dav.vfs import DavTree

        monkeypatch.setattr(settings, "DAV_TREE_TTL_MINUTES", 1)
        call_count = {"n": 0}

        async def fake_build():
            call_count["n"] += 1
            return DavTree()

        monkeypatch.setattr(tree_builder_mod, "build_dav_tree", fake_build)
        cache = DavTreeCache()

        await cache.get()
        assert call_count["n"] == 1

        # Simulate the TTL having elapsed (2 min > 1 min TTL) without sleeping.
        cache._tree.built_at -= 120
        await cache.get()

        assert call_count["n"] == 2

    async def test_no_rebuild_before_ttl_expiry(self, monkeypatch):
        import app.dav.tree_builder as tree_builder_mod
        from app.dav.vfs import DavTree

        monkeypatch.setattr(settings, "DAV_TREE_TTL_MINUTES", 60)
        call_count = {"n": 0}

        async def fake_build():
            call_count["n"] += 1
            return DavTree()

        monkeypatch.setattr(tree_builder_mod, "build_dav_tree", fake_build)
        cache = DavTreeCache()

        await cache.get()
        await cache.get()

        assert call_count["n"] == 1

    async def test_zero_ttl_never_expires_until_invalidated(self, monkeypatch):
        import app.dav.tree_builder as tree_builder_mod
        from app.dav.vfs import DavTree

        monkeypatch.setattr(settings, "DAV_TREE_TTL_MINUTES", 0)
        call_count = {"n": 0}

        async def fake_build():
            call_count["n"] += 1
            return DavTree()

        monkeypatch.setattr(tree_builder_mod, "build_dav_tree", fake_build)
        cache = DavTreeCache()

        await cache.get()
        cache._tree.built_at -= 10_000_000
        await cache.get()

        assert call_count["n"] == 1

    async def test_single_flight_concurrent_get_builds_once(self, monkeypatch):
        import app.dav.tree_builder as tree_builder_mod
        from app.dav.vfs import DavTree

        call_count = {"n": 0}

        async def slow_build():
            call_count["n"] += 1
            await asyncio.sleep(0.05)
            return DavTree()

        monkeypatch.setattr(tree_builder_mod, "build_dav_tree", slow_build)
        cache = DavTreeCache()

        results = await asyncio.gather(*(cache.get() for _ in range(5)))

        assert call_count["n"] == 1
        assert all(r is results[0] for r in results)
