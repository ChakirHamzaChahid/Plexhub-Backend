"""Convergence + orphan-prune tests.

Two related fixes:
  1. aggregation_service merges rows that designate the SAME entity even when
     their derived `unification_id` strings diverge (imdb vs tmdb, or one twin
     left title-based) → one film/series = one group = one folder.
  2. The generator surfaces a human-meaningful disambiguation token (never
     `[imdb]`/`[tmdb]`/`[title_]`) and sweeps orphan title folders left behind
     when a title's versions move folder.
"""
import asyncio

import pytest

from app.models.database import Media
from app.plex_generator.generator import (
    PlexLibraryGenerator, _disambiguation_token,
)
from app.plex_generator.models import PlexMovie, PlexMovieVersion
from app.plex_generator.source import MediaSource
from app.plex_generator.storage import LocalStorage
from app.services.aggregation_service import aggregate_movies, aggregate_series
from app.utils.server_id import build_server_id
from app.utils.unification import calculate_unification_id


# ─── helpers ────────────────────────────────────────────────────────────


def _movie(rk, title, unif, imdb=None, tmdb=None, year=2008, account="a"):
    return Media(
        rating_key=rk, server_id=build_server_id(account), filter="all",
        sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=year, unification_id=unif,
        imdb_id=imdb, tmdb_id=tmdb, is_in_allowed_categories=True, is_broken=False,
    )


def _show(rk, title, unif, imdb=None, tmdb=None, year=2008, account="a"):
    return Media(
        rating_key=rk, server_id=build_server_id(account), filter="all",
        sort_order="default", library_section_id="xtream_series",
        title=title, type="show", year=year, unification_id=unif,
        imdb_id=imdb, tmdb_id=tmdb, is_in_allowed_categories=True, is_broken=False,
    )


def _ep(rk, show_rk, season, episode, account="a"):
    return Media(
        rating_key=rk, server_id=build_server_id(account), filter="all",
        sort_order="default", library_section_id="xtream_series",
        title=f"Episode {episode}", type="episode",
        grandparent_rating_key=show_rk, parent_index=season, index=episode,
        unification_id="", is_in_allowed_categories=True, is_broken=False,
    )


# ─── Pass A: merge by shared external id ────────────────────────────────


class TestMergeBySharedIds:
    def test_imdb_and_tmdb_rows_same_film_merge(self):
        # John Rambo: imdb+tmdb row, two tmdb-only rows — all share tmdb 7555.
        rows = [
            _movie("vod_1", "John Rambo", "imdb://tt0462499", imdb="tt0462499", tmdb="7555"),
            _movie("vod_2", "John Rambo", "tmdb://7555", tmdb="7555"),
            _movie("vod_3", "John Rambo", "tmdb://7555", tmdb="7555"),
        ]
        groups = aggregate_movies(rows)
        assert len(groups) == 1
        assert groups[0].key == "imdb://tt0462499"   # strongest representative
        assert len(groups[0].members) == 3

    def test_distinct_ids_not_merged(self):
        # Same (title, year) but genuinely different ids and no shared id → keep apart.
        rows = [
            _movie("vod_1", "Crash", "imdb://tt111", imdb="tt111", year=2004),
            _movie("vod_2", "Crash", "imdb://tt222", imdb="tt222", year=2004),
        ]
        assert len(aggregate_movies(rows)) == 2

    def test_zero_tmdb_id_is_not_a_join_token(self):
        # Bogus tmdb_id "0" must not merge unrelated films.
        rows = [
            _movie("vod_1", "Foo", "title_foo_2008", tmdb="0"),
            _movie("vod_2", "Bar", "title_bar_2008", tmdb="0"),
        ]
        assert len(aggregate_movies(rows)) == 2


# ─── Pass B: absorb unresolved title twin ───────────────────────────────


class TestAbsorbTitleGroups:
    def test_title_twin_absorbed_into_id_group(self):
        rows = [
            _movie("vod_1", "John Rambo", "imdb://tt0462499", imdb="tt0462499", tmdb="7555", year=2008),
            _movie("vod_2", "John Rambo", "title_john_rambo_2008", year=2008),
        ]
        groups = aggregate_movies(rows)
        assert len(groups) == 1
        assert groups[0].key == "imdb://tt0462499"
        assert len(groups[0].members) == 2

    def test_degenerate_title_not_absorbed(self):
        # Non-latin title normalizes to empty → must NOT false-merge.
        arabic = "تراب الماس"
        tkey = calculate_unification_id(arabic, 2018)
        rows = [
            _movie("vod_1", arabic, "tmdb://515224", tmdb="515224", year=2018),
            _movie("vod_2", arabic, tkey, year=2018),
        ]
        assert len(aggregate_movies(rows)) == 2


# ─── Series convergence ─────────────────────────────────────────────────


class TestSeriesConvergence:
    def test_two_listings_same_show_merge_seasons(self):
        shows = [
            _show("series_1", "Breaking Bad", "imdb://tt0903747", imdb="tt0903747", tmdb="1396"),
            _show("series_9", "Breaking Bad", "tmdb://1396", tmdb="1396"),
        ]
        eps = [_ep("ep_1", "series_1", 1, 1), _ep("ep_9", "series_9", 2, 1)]
        groups = aggregate_series(shows, eps)
        assert len(groups) == 1
        assert {(s.season, s.episode) for s in groups[0].slots} == {(1, 1), (2, 1)}


# ─── Disambiguation token ───────────────────────────────────────────────


class TestDisambiguationToken:
    def test_token_strips_scheme(self):
        assert _disambiguation_token("imdb://tt1234567") == "tt1234567"
        assert _disambiguation_token("tmdb://577922") == "tmdb577922"
        assert _disambiguation_token("title_alex_2017") == "alex_2017"


class _MockSource(MediaSource):
    def __init__(self, movies=None, series=None):
        self._movies, self._series = movies or [], series or []

    async def get_movies(self):
        return self._movies

    async def get_series(self):
        return self._series


def _plex_movie(source_id, title, year):
    return PlexMovie(
        source_id=source_id, title=title, year=year,
        versions=[PlexMovieVersion(
            source_id=f"v_{source_id}", server_id="xtream_a",
            label="L", stream_url=f"http://a/{source_id}",
        )],
    )


class TestHomonymFolders:
    def test_distinct_ids_no_suffix_get_id_token_folders(self, tmp_path):
        # Two real homonyms (no qualifier in title) must land in distinct,
        # non-colliding folders — never both `[imdb]`.
        a = _plex_movie("imdb://tt111", "Crash", 2004)
        b = _plex_movie("imdb://tt222", "Crash", 2004)
        gen = PlexLibraryGenerator(_MockSource(movies=[a, b]),
                                   LocalStorage(tmp_path), tmp_path, strm_only=True)
        report = asyncio.run(gen.generate())
        assert report.created == 2
        films = {p.name for p in (tmp_path / "Films").iterdir()}
        assert films == {"Crash (2004) [tt111]", "Crash (2004) [tt222]"}


# ─── Orphan-dir prune ───────────────────────────────────────────────────


class TestPruneOrphanDirs:
    def test_orphan_folder_pruned(self, tmp_path):
        storage = LocalStorage(tmp_path)
        # An orphan: generated metadata but no playable .strm (episodes moved away).
        ghost = tmp_path / "Series" / "Ghost (1990)"
        ghost.mkdir(parents=True)
        (ghost / "tvshow.nfo").write_text("<tvshow/>", encoding="utf-8")
        (ghost / "poster.jpg").write_bytes(b"x")

        gen = PlexLibraryGenerator(
            _MockSource(movies=[_plex_movie("tmdb://1", "Live", 2020)]),
            storage, tmp_path, strm_only=True,
        )
        report = asyncio.run(gen.generate())

        assert report.pruned == 1
        assert not ghost.exists()
        assert (tmp_path / "Films" / "Live (2020)").exists()  # live folder kept

    def test_folder_with_unexpected_file_kept(self, tmp_path):
        storage = LocalStorage(tmp_path)
        keep = tmp_path / "Films" / "Keepme (2000)"
        keep.mkdir(parents=True)
        (keep / "movie.nfo").write_text("<movie/>", encoding="utf-8")
        (keep / "user-notes.txt").write_text("mine", encoding="utf-8")

        gen = PlexLibraryGenerator(_MockSource(movies=[]), storage, tmp_path, strm_only=True)
        report = asyncio.run(gen.generate())

        assert report.pruned == 0
        assert keep.exists()  # unexpected file → never deleted
