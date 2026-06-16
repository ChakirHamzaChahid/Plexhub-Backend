"""Tests for cross-account / multi-version deduplication in Plex generation.

Goal: the same movie/series coming from several Xtream accounts (or as several
qualities/languages within one) collapses into a SINGLE library entry — one
folder, one NFO, one poster — with multiple playable .strm versions.
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.database import Media, XtreamAccount
from app.plex_generator.models import (
    PlexMovie, PlexMovieVersion,
    PlexEpisode, PlexEpisodeVersion,
    PlexSeries,
)
from app.plex_generator.naming import (
    movie_version_path,
    series_episode_version_path,
)
from app.plex_generator.storage import LocalStorage
from app.plex_generator.source import MediaSource, DatabaseSource
from app.plex_generator.generator import PlexLibraryGenerator
from app.utils.server_id import build_server_id


# ─── Naming ─────────────────────────────────────────────────────────────


class TestCanonicalTitle:
    def _row(self, title, year=None):
        return _movie_row("a", "vod_x.mp4", title, "", 0) if year is None else \
            Media(rating_key="vod_x.mp4", server_id="xtream_a", filter="all",
                  sort_order="default", library_section_id="xtream_vod",
                  title=title, type="movie", year=year, unification_id="")

    def test_strips_qualifier_keeps_year(self):
        from app.services.aggregation_service import canonical_title_year
        assert canonical_title_year(self._row("Terminator (1984) (VF)", 1984)) == ("Terminator", 1984)

    def test_year_from_title_when_column_missing(self):
        from app.services.aggregation_service import canonical_title_year
        row = Media(rating_key="vod_y.mp4", server_id="xtream_a", filter="all",
                    sort_order="default", library_section_id="xtream_vod",
                    title="Dune (2021) (HD)", type="movie", unification_id="")
        assert canonical_title_year(row) == ("Dune", 2021)

    def test_clean_title_unchanged(self):
        from app.services.aggregation_service import canonical_title_year
        assert canonical_title_year(self._row("Inception", 2010)) == ("Inception", 2010)


class TestVersionNaming:
    def test_movie_version_path(self):
        assert movie_version_path("Terminator", 1984, "VF · Compte 1") == (
            "Films/Terminator (1984)/Terminator (1984) - VF · Compte 1.strm"
        )

    def test_movie_version_path_sanitizes(self):
        # No Plex-only braces — portable to Jellyfin/Emby too.
        path = movie_version_path("Terminator", 1984, "HD <raw>")
        assert "{" not in path and "}" not in path
        assert path.endswith("Terminator (1984) - HD raw.strm")

    def test_series_episode_version_path(self):
        assert series_episode_version_path(
            "Breaking Bad", 1, 1, "Compte 2", year=2008,
        ) == (
            "Series/Breaking Bad (2008)/Season 01/"
            "Breaking Bad (2008) S01E01 - Compte 2.strm"
        )


# ─── Generator-level dedup (MockSource) ─────────────────────────────────


class MockSource(MediaSource):
    def __init__(self, movies=None, series=None):
        self._movies = movies or []
        self._series = series or []

    async def get_movies(self):
        return self._movies

    async def get_series(self):
        return self._series


def _terminator(versions):
    return PlexMovie(
        source_id="tmdb://218",
        title="Terminator (1984)",
        year=1984,
        genres="Action, Sci-Fi",
        summary="A cyborg is sent back in time.",
        tmdb_id=218,
        versions=versions,
    )


class TestMovieDedup:
    def _three_versions(self):
        return [
            PlexMovieVersion(source_id="vod_1.mp4", server_id="xtream_a",
                             label="VF · Compte 1", stream_url="http://a/1"),
            PlexMovieVersion(source_id="vod_2.mp4", server_id="xtream_a",
                             label="HD · Compte 1", stream_url="http://a/2"),
            PlexMovieVersion(source_id="vod_9.mp4", server_id="xtream_b",
                             label="VF · Compte 2", stream_url="http://b/9"),
        ]

    def test_single_folder_single_nfo_multiple_strm(self, tmp_path):
        source = MockSource(movies=[_terminator(self._three_versions())])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(source, storage, tmp_path, strm_only=False)
        report = asyncio.run(gen.generate())

        folder = tmp_path / "Films" / "Terminator (1984)"
        # Exactly ONE movie.nfo and ONE poster slot for the whole group.
        assert (folder / "movie.nfo").exists()
        nfos = list(folder.glob("*.nfo"))
        assert nfos == [folder / "movie.nfo"]
        # Three version .strm files (Plex+Jellyfin " - label"), one per source.
        strms = sorted(p.name for p in folder.glob("*.strm"))
        assert strms == [
            "Terminator (1984) - HD · Compte 1.strm",
            "Terminator (1984) - VF · Compte 1.strm",
            "Terminator (1984) - VF · Compte 2.strm",
        ]
        assert report.created == 3

    def test_removing_one_version_keeps_shared_nfo(self, tmp_path):
        storage = LocalStorage(tmp_path)
        gen1 = PlexLibraryGenerator(
            MockSource(movies=[_terminator(self._three_versions())]),
            storage, tmp_path, strm_only=False,
        )
        asyncio.run(gen1.generate())

        # Second run: account 2 dropped its copy.
        remaining = [v for v in self._three_versions() if v.server_id != "xtream_b"]
        gen2 = PlexLibraryGenerator(
            MockSource(movies=[_terminator(remaining)]),
            storage, tmp_path, strm_only=False,
        )
        r2 = asyncio.run(gen2.generate())

        folder = tmp_path / "Films" / "Terminator (1984)"
        assert r2.deleted == 1
        # Shared metadata survives because the folder still has live versions.
        assert (folder / "movie.nfo").exists()
        assert not (folder / "Terminator (1984) - VF · Compte 2.strm").exists()
        assert (folder / "Terminator (1984) - VF · Compte 1.strm").exists()

    def test_removing_all_versions_deletes_folder(self, tmp_path):
        storage = LocalStorage(tmp_path)
        gen1 = PlexLibraryGenerator(
            MockSource(movies=[_terminator(self._three_versions())]),
            storage, tmp_path, strm_only=False,
        )
        asyncio.run(gen1.generate())

        gen2 = PlexLibraryGenerator(MockSource(movies=[]), storage, tmp_path)
        r2 = asyncio.run(gen2.generate())

        assert r2.deleted == 3
        assert not (tmp_path / "Films" / "Terminator (1984)").exists()

    def test_distinct_groups_same_title_year_disambiguated(self, tmp_path):
        # Two genuinely different movies that collide on (title, year) but have
        # distinct unification ids must NOT be merged — folders stay separate.
        a = PlexMovie(
            source_id="tmdb://1", title="Crash (US)", year=2004,
            versions=[PlexMovieVersion(source_id="vod_1", server_id="xtream_a",
                                       label="C1", stream_url="http://a/1")],
        )
        b = PlexMovie(
            source_id="tmdb://2", title="Crash (HD)", year=2004,
            versions=[PlexMovieVersion(source_id="vod_2", server_id="xtream_b",
                                       label="C2", stream_url="http://b/2")],
        )
        gen = PlexLibraryGenerator(MockSource(movies=[a, b]), LocalStorage(tmp_path),
                                   tmp_path, strm_only=True)
        report = asyncio.run(gen.generate())
        assert report.created == 2
        films = {p.name for p in (tmp_path / "Films").iterdir()}
        # Folder-level disambiguation kicks in (suffix US / HD).
        assert films == {"Crash (2004) (US)", "Crash (2004) (HD)"}


class TestEpisodeDedup:
    def test_episode_versions_merge_into_one_slot(self, tmp_path):
        ep = PlexEpisode(
            source_id="tmdb://1396|S01E01",
            series_title="Breaking Bad", season_num=1, episode_num=1,
            title="Pilot", summary="Chemistry teacher.",
            versions=[
                PlexEpisodeVersion(source_id="ep_1.mkv", server_id="xtream_a",
                                   label="Compte 1", stream_url="http://a/e1"),
                PlexEpisodeVersion(source_id="ep_9.mkv", server_id="xtream_b",
                                   label="Compte 2", stream_url="http://b/e1"),
            ],
        )
        series = PlexSeries(source_id="tmdb://1396", title="Breaking Bad",
                            year=2008, episodes=[ep])
        gen = PlexLibraryGenerator(MockSource(series=[series]), LocalStorage(tmp_path),
                                   tmp_path, strm_only=False)
        report = asyncio.run(gen.generate())

        season = tmp_path / "Series" / "Breaking Bad (2008)" / "Season 01"
        strms = sorted(p.name for p in season.glob("*.strm"))
        assert strms == [
            "Breaking Bad (2008) S01E01 - Compte 1.strm",
            "Breaking Bad (2008) S01E01 - Compte 2.strm",
        ]
        # One tvshow.nfo at the series root.
        assert (tmp_path / "Series" / "Breaking Bad (2008)" / "tvshow.nfo").exists()
        assert report.created == 2


# ─── Source-level dedup (real DB, two accounts) ─────────────────────────


def _account(id_: str, label: str) -> XtreamAccount:
    return XtreamAccount(
        id=id_, label=label, base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


def _movie_row(account_id: str, rating_key: str, title: str, unif: str,
               page_offset: int = 0) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=1984, unification_id=unif,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
    )


class TestUnifiedDatabaseSource:
    @pytest_asyncio.fixture
    async def seeded_factory(self, db_engine, monkeypatch):
        factory = async_sessionmaker(db_engine, class_=AsyncSession,
                                     expire_on_commit=False)
        async with factory() as s:
            s.add_all([
                _account("a", "Compte 1"),
                _account("b", "Compte 2"),
                # Same film (tmdb://218) on both accounts, different titles/keys.
                _movie_row("a", "vod_1.mp4", "Terminator (1984) (VF)", "tmdb://218", 0),
                _movie_row("a", "vod_2.mp4", "Terminator (1984) (HD)", "tmdb://218", 1),
                _movie_row("b", "vod_9.mp4", "Terminator (1984) (VOSTFR)", "tmdb://218", 0),
                # A different, un-enriched film stays its own group.
                _movie_row("a", "vod_5.mp4", "Alien (1979)", "", 2),
            ])
            await s.commit()

        import app.plex_generator.source as source_mod
        monkeypatch.setattr(source_mod, "async_session_factory", factory)
        return factory

    @pytest.mark.asyncio
    async def test_groups_same_film_across_accounts(self, seeded_factory):
        movies = await DatabaseSource().get_movies()

        by_group = {m.source_id: m for m in movies}
        assert "tmdb://218" in by_group
        term = by_group["tmdb://218"]
        # Canonical title is cleaned (qualifier stripped), year preserved.
        assert term.title == "Terminator"
        assert term.year == 1984
        # Three versions merged under one group.
        assert len(term.versions) == 3
        urls = {v.stream_url for v in term.versions}
        assert urls == {
            "http://a.example/movie/u/p/1.mp4",
            "http://a.example/movie/u/p/2.mp4",
            "http://b.example/movie/u/p/9.mp4",
        }
        # Labels carry the qualifier + account label and are unique.
        labels = sorted(v.label for v in term.versions)
        assert labels == ["HD · Compte 1", "VF · Compte 1", "VOSTFR · Compte 2"]
        assert len(set(labels)) == 3

        # The un-enriched movie is a separate single-version group.
        alien = next(m for m in movies if m.title.startswith("Alien"))
        assert len(alien.versions) == 1

    @pytest.mark.asyncio
    async def test_restrict_to_one_account(self, seeded_factory):
        movies = await DatabaseSource(account_ids=["a"]).get_movies()
        term = next(m for m in movies if m.source_id == "tmdb://218")
        # Only account "a" sources are aggregated.
        assert len(term.versions) == 2
        assert all(v.server_id == "xtream_a" for v in term.versions)


# ─── API-level dedup (media_service) ────────────────────────────────────


def _show_row(account_id: str, rating_key: str, title: str, unif: str,
              page_offset: int = 0) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=title, type="show", year=2008, unification_id=unif,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
    )


def _episode_row(account_id: str, rating_key: str, show_rk: str,
                 season: int, episode: int, page_offset: int = 0) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=f"Episode {episode}", type="episode",
        grandparent_rating_key=show_rk, parent_index=season, index=episode,
        unification_id="", page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


class TestUnifiedApiService:
    @pytest.mark.asyncio
    async def test_unified_movies_group_and_versions(self, db_session):
        from app.services.media_service import media_service
        from app.api.media import _build_versions

        db_session.add_all([
            _account("a", "Compte 1"), _account("b", "Compte 2"),
            _movie_row("a", "vod_1.mp4", "Terminator (1984) (VF)", "tmdb://218", 0),
            _movie_row("b", "vod_9.mp4", "Terminator (1984) (HD)", "tmdb://218", 0),
            _movie_row("a", "vod_5.mp4", "Alien (1979)", "imdb://tt0078748", 1),
        ])
        await db_session.commit()

        groups, total = await media_service.get_unified_list(db_session, "movie")
        assert total == 2
        labels = await media_service.account_labels(db_session)
        by_key = {g.key: g for g in groups}
        # Canonical title cleaned for the unified card.
        from app.services.aggregation_service import canonical_title_year
        assert canonical_title_year(by_key["tmdb://218"].best) == ("Terminator", 1984)
        versions = _build_versions(by_key["tmdb://218"].members, labels)
        assert len(versions) == 2
        assert {v.server_id for v in versions} == {"xtream_a", "xtream_b"}
        assert sorted(v.label for v in versions) == ["HD · Compte 2", "VF · Compte 1"]

    @pytest.mark.asyncio
    async def test_unified_episodes_merge_across_accounts(self, db_session):
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"), _account("b", "Compte 2"),
            _show_row("a", "series_1", "Breaking Bad", "tmdb://1396", 0),
            _show_row("b", "series_9", "Breaking Bad", "tmdb://1396", 0),
            _episode_row("a", "ep_1.mkv", "series_1", 1, 1, 1),
            _episode_row("b", "ep_9.mkv", "series_9", 1, 1, 1),
            _episode_row("a", "ep_2.mkv", "series_1", 1, 2, 2),
        ])
        await db_session.commit()

        result = await media_service.get_unified_episodes(db_session, "tmdb://1396")
        assert result is not None
        shows, group = result
        assert len(shows) == 2
        slots = {(s.season, s.episode): s for s in group.slots}
        # S01E01 exists on both accounts -> 2 versions; S01E02 only on a -> 1.
        assert len(slots[(1, 1)].members) == 2
        assert len(slots[(1, 2)].members) == 1
