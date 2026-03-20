import asyncio
import json
from pathlib import Path

import pytest

from app.plex_generator.models import (
    PlexMovie,
    PlexEpisode,
    PlexSeries,
    SyncReport,
)
from app.plex_generator.naming import (
    sanitize_for_filesystem,
    movie_path,
    movie_nfo_path,
    movie_poster_path,
    series_episode_path,
    series_nfo_path,
    series_poster_path,
)
from app.plex_generator.mapping import MappingStore
from app.plex_generator.storage import LocalStorage
from app.plex_generator.nfo_builder import build_movie_nfo, build_tvshow_nfo
from app.plex_generator.source import MediaSource
from app.plex_generator.generator import PlexLibraryGenerator


# ─── Naming Tests ───────────────────────────────────────────────


class TestSanitizeForFilesystem:
    def test_removes_invalid_chars(self):
        assert sanitize_for_filesystem('Film: The "Best" One?') == "Film The Best One"

    def test_replaces_backslash_and_pipe(self):
        assert sanitize_for_filesystem("A\\B|C") == "A B C"

    def test_strips_trailing_dots(self):
        assert sanitize_for_filesystem("Something...") == "Something"

    def test_collapses_spaces(self):
        assert sanitize_for_filesystem("A  :  B") == "A B"

    def test_empty_after_sanitize(self):
        assert sanitize_for_filesystem(":::") == ""

    def test_normal_title_unchanged(self):
        assert sanitize_for_filesystem("Inception") == "Inception"

    def test_unicode_preserved(self):
        assert sanitize_for_filesystem("Les Misérables") == "Les Misérables"


class TestMoviePath:
    def test_with_year(self):
        assert movie_path("Dune", 2021) == "Films/Dune (2021)/Dune (2021).strm"

    def test_without_year(self):
        assert movie_path("Unknown Movie", None) == "Films/Unknown Movie/Unknown Movie.strm"

    def test_special_chars_sanitized(self):
        result = movie_path('Star Wars: A New Hope', 1977)
        assert ":" not in result
        assert result == "Films/Star Wars A New Hope (1977)/Star Wars A New Hope (1977).strm"

    def test_nfo_path(self):
        assert movie_nfo_path("Dune", 2021) == "Films/Dune (2021)/movie.nfo"

    def test_poster_path(self):
        assert movie_poster_path("Dune", 2021) == "Films/Dune (2021)/poster.jpg"


class TestSeriesEpisodePath:
    def test_basic(self):
        result = series_episode_path("The Last of Us", 1, 1)
        assert result == "Series/The Last of Us/Season 01/The Last of Us S01E01.strm"

    def test_double_digit_season_episode(self):
        result = series_episode_path("Breaking Bad", 5, 16)
        assert result == "Series/Breaking Bad/Season 05/Breaking Bad S05E16.strm"

    def test_special_chars_in_title(self):
        result = series_episode_path("Grey's Anatomy", 1, 1)
        assert result == "Series/Grey's Anatomy/Season 01/Grey's Anatomy S01E01.strm"

    def test_nfo_path(self):
        assert series_nfo_path("The Last of Us") == "Series/The Last of Us/tvshow.nfo"

    def test_poster_path(self):
        assert series_poster_path("The Last of Us") == "Series/The Last of Us/poster.jpg"


# ─── Mapping Tests ──────────────────────────────────────────────


class TestMappingStore:
    def test_load_save_roundtrip(self, tmp_path):
        store = MappingStore(tmp_path)
        store.load()
        assert len(store) == 0

        store.set("vod_123.mp4", "Films/Test/Test.strm", "http://example.com/123.mp4")
        store.save()

        store2 = MappingStore(tmp_path)
        store2.load()
        assert len(store2) == 1
        entry = store2.get("vod_123.mp4")
        assert entry is not None
        assert entry.path == "Films/Test/Test.strm"
        assert entry.stream_url == "http://example.com/123.mp4"

    def test_remove(self, tmp_path):
        store = MappingStore(tmp_path)
        store.load()
        store.set("vod_1", "path1", "url1")
        store.set("vod_2", "path2", "url2")
        assert len(store) == 2

        store.remove("vod_1")
        assert len(store) == 1
        assert store.get("vod_1") is None
        assert store.get("vod_2") is not None

    def test_all_source_ids(self, tmp_path):
        store = MappingStore(tmp_path)
        store.load()
        store.set("a", "p1", "u1")
        store.set("b", "p2", "u2")
        assert store.all_source_ids() == {"a", "b"}

    def test_corrupted_file(self, tmp_path):
        (tmp_path / ".plex_mapping.json").write_text("not json", encoding="utf-8")
        store = MappingStore(tmp_path)
        store.load()
        assert len(store) == 0


# ─── NFO Builder Tests ──────────────────────────────────────────


class TestNfoBuilder:
    def test_movie_nfo_basic(self):
        movie = PlexMovie(
            source_id="vod_1",
            title="Dune",
            year=2021,
            stream_url="http://test",
            genres="Sci-Fi, Adventure",
            summary="A desert planet story",
            imdb_id="tt1160419",
            tmdb_id=438631,
        )
        xml = build_movie_nfo(movie)
        assert "<title>Dune</title>" in xml
        assert "<year>2021</year>" in xml
        assert "<plot>A desert planet story</plot>" in xml
        assert "<genre>Sci-Fi</genre>" in xml
        assert "<genre>Adventure</genre>" in xml
        assert 'type="imdb"' in xml
        assert "tt1160419" in xml
        assert 'type="tmdb"' in xml
        assert "438631" in xml

    def test_tvshow_nfo_basic(self):
        series = PlexSeries(
            source_id="series_1",
            title="Breaking Bad",
            year=2008,
            genres="Drama, Crime",
            summary="A teacher turns to crime",
        )
        xml = build_tvshow_nfo(series)
        assert "<title>Breaking Bad</title>" in xml
        assert "<year>2008</year>" in xml

    def test_movie_nfo_minimal(self):
        movie = PlexMovie(
            source_id="vod_2",
            title="Unknown",
            stream_url="http://test",
        )
        xml = build_movie_nfo(movie)
        assert "<title>Unknown</title>" in xml
        assert "<year>" not in xml


# ─── Storage Tests ──────────────────────────────────────────────


class TestLocalStorage:
    def test_write_and_read_strm(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.write_strm("Films/Test/Test.strm", "http://example.com/stream")
        content = storage.read_strm("Films/Test/Test.strm")
        assert content == "http://example.com/stream"

    def test_write_file(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.write_file("Films/Test/movie.nfo", "<movie><title>X</title></movie>")
        assert (tmp_path / "Films" / "Test" / "movie.nfo").exists()

    def test_delete_file(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.write_strm("Films/Test/Test.strm", "http://x")
        assert (tmp_path / "Films" / "Test" / "Test.strm").exists()
        storage.delete_file("Films/Test/Test.strm")
        assert not (tmp_path / "Films" / "Test" / "Test.strm").exists()

    def test_cleanup_empty_dirs(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.write_strm("Films/Test/Test.strm", "http://x")
        storage.delete_file("Films/Test/Test.strm")
        storage.cleanup_empty_dirs("Films/Test/Test.strm")
        assert not (tmp_path / "Films" / "Test").exists()
        # Films/ itself is now empty and should be cleaned
        assert not (tmp_path / "Films").exists()

    def test_read_nonexistent_strm(self, tmp_path):
        storage = LocalStorage(tmp_path)
        assert storage.read_strm("nope.strm") is None


# ─── Generator Tests (with MockSource) ──────────────────────────


class MockSource(MediaSource):
    def __init__(self, movies=None, series=None):
        self._movies = movies or []
        self._series = series or []

    async def get_movies(self):
        return self._movies

    async def get_series(self):
        return self._series


def _make_movie(sid="vod_1.mp4", title="Test Movie", year=2023, url="http://stream/1"):
    return PlexMovie(source_id=sid, title=title, year=year, stream_url=url)


def _make_series(sid="series_1", title="Test Series", episodes=None):
    eps = episodes or [
        PlexEpisode(
            source_id="ep_1.mkv", series_title=title,
            season_num=1, episode_num=1, stream_url="http://stream/ep1",
        ),
        PlexEpisode(
            source_id="ep_2.mkv", series_title=title,
            season_num=1, episode_num=2, stream_url="http://stream/ep2",
        ),
    ]
    return PlexSeries(source_id=sid, title=title, episodes=eps)


class TestGenerator:
    def test_create_movies(self, tmp_path):
        source = MockSource(movies=[_make_movie()])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        report = asyncio.run(gen.generate())

        assert report.created == 1
        assert report.unchanged == 0
        strm = tmp_path / "Films" / "Test Movie (2023)" / "Test Movie (2023).strm"
        assert strm.exists()
        assert strm.read_text(encoding="utf-8").strip() == "http://stream/1"

    def test_idempotent(self, tmp_path):
        source = MockSource(movies=[_make_movie()])
        storage = LocalStorage(tmp_path)

        gen1 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        r1 = asyncio.run(gen1.generate())
        assert r1.created == 1

        gen2 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        r2 = asyncio.run(gen2.generate())
        assert r2.created == 0
        assert r2.unchanged == 1

    def test_url_update(self, tmp_path):
        movie = _make_movie(url="http://old")
        source = MockSource(movies=[movie])
        storage = LocalStorage(tmp_path)

        gen1 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        asyncio.run(gen1.generate())

        movie_updated = _make_movie(url="http://new")
        source2 = MockSource(movies=[movie_updated])
        gen2 = PlexLibraryGenerator(source2, storage, tmp_path, strm_only=True)
        r2 = asyncio.run(gen2.generate())

        assert r2.updated == 1
        strm = tmp_path / "Films" / "Test Movie (2023)" / "Test Movie (2023).strm"
        assert strm.read_text(encoding="utf-8").strip() == "http://new"

    def test_delete_stale(self, tmp_path):
        source = MockSource(movies=[_make_movie()])
        storage = LocalStorage(tmp_path)
        gen1 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        asyncio.run(gen1.generate())

        # Second run with empty source -> should delete
        source2 = MockSource(movies=[])
        gen2 = PlexLibraryGenerator(source2, storage, tmp_path, strm_only=True)
        r2 = asyncio.run(gen2.generate())

        assert r2.deleted == 1
        assert not (tmp_path / "Films" / "Test Movie (2023)").exists()

    def test_series_episodes(self, tmp_path):
        source = MockSource(series=[_make_series()])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        report = asyncio.run(gen.generate())

        assert report.created == 2
        ep1 = tmp_path / "Series" / "Test Series" / "Season 01" / "Test Series S01E01.strm"
        ep2 = tmp_path / "Series" / "Test Series" / "Season 01" / "Test Series S01E02.strm"
        assert ep1.exists()
        assert ep2.exists()
        assert ep1.read_text(encoding="utf-8").strip() == "http://stream/ep1"

    def test_movie_with_metadata(self, tmp_path):
        movie = PlexMovie(
            source_id="vod_1.mp4",
            title="Dune",
            year=2021,
            stream_url="http://stream/1",
            genres="Sci-Fi",
            summary="Epic",
            imdb_id="tt1160419",
        )
        source = MockSource(movies=[movie])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(source, storage, tmp_path, strm_only=False)
        report = asyncio.run(gen.generate())

        assert report.created == 1
        nfo = tmp_path / "Films" / "Dune (2021)" / "movie.nfo"
        assert nfo.exists()
        content = nfo.read_text(encoding="utf-8")
        assert "<title>Dune</title>" in content

    def test_move_on_title_change(self, tmp_path):
        movie = _make_movie(sid="vod_1.mp4", title="Old Title", year=2023)
        source = MockSource(movies=[movie])
        storage = LocalStorage(tmp_path)
        gen1 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        asyncio.run(gen1.generate())

        assert (tmp_path / "Films" / "Old Title (2023)" / "Old Title (2023).strm").exists()

        movie_renamed = _make_movie(sid="vod_1.mp4", title="New Title", year=2023)
        source2 = MockSource(movies=[movie_renamed])
        gen2 = PlexLibraryGenerator(source2, storage, tmp_path, strm_only=True)
        r2 = asyncio.run(gen2.generate())

        assert r2.updated == 1
        assert not (tmp_path / "Films" / "Old Title (2023)").exists()
        assert (tmp_path / "Films" / "New Title (2023)" / "New Title (2023).strm").exists()
