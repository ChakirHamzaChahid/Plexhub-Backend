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
        # sanitize_for_filesystem returns "Unknown" for empty results
        assert sanitize_for_filesystem(":::") == "Unknown"

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

    def test_write_file_returns_created_on_first_write(self, tmp_path):
        storage = LocalStorage(tmp_path)
        status = storage.write_file("Films/Test/movie.nfo", "<movie/>")
        assert status == "created"

    def test_write_file_preserves_existing_by_default(self, tmp_path):
        """AUDIT-P6-006 default contract: an existing generated file (e.g.
        hand-enriched with Tiny Media Manager) is never rewritten unless the
        caller explicitly opts in with force=True."""
        storage = LocalStorage(tmp_path)
        storage.write_file("Films/Test/movie.nfo", "<movie><title>Original</title></movie>")
        status = storage.write_file("Films/Test/movie.nfo", "<movie><title>New</title></movie>")
        assert status == "unchanged"
        content = (tmp_path / "Films" / "Test" / "movie.nfo").read_text(encoding="utf-8")
        assert "Original" in content
        assert "New" not in content

    def test_write_file_force_rewrites_existing(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.write_file("Films/Test/movie.nfo", "<movie><title>Original</title></movie>")
        status = storage.write_file(
            "Films/Test/movie.nfo", "<movie><title>New</title></movie>", force=True,
        )
        assert status == "updated"
        content = (tmp_path / "Films" / "Test" / "movie.nfo").read_text(encoding="utf-8")
        assert "New" in content
        assert "Original" not in content

    def test_write_file_force_on_new_file_is_created_not_updated(self, tmp_path):
        storage = LocalStorage(tmp_path)
        status = storage.write_file("Films/Test/movie.nfo", "<movie/>", force=True)
        assert status == "created"

    def test_write_file_force_writes_atomically(self, tmp_path):
        """force=True must go through the same tempfile+os.replace path as any
        other write — never leave a partially-written file if interrupted."""
        storage = LocalStorage(tmp_path)
        storage.write_file("Films/Test/movie.nfo", "<movie><title>Original</title></movie>")
        storage.write_file(
            "Films/Test/movie.nfo", "<movie><title>New</title></movie>", force=True,
        )
        target = tmp_path / "Films" / "Test" / "movie.nfo"
        tmp = target.with_suffix(target.suffix + ".tmp")
        assert not tmp.exists()
        assert target.read_text(encoding="utf-8") == "<movie><title>New</title></movie>"

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


class TestImageErrorClassification:
    def test_classifies_http_4xx(self):
        import httpx
        from app.plex_generator.generator import _classify_image_error

        req = httpx.Request("GET", "http://x/")
        resp = httpx.Response(404, request=req)
        exc = httpx.HTTPStatusError("not found", request=req, response=resp)
        assert _classify_image_error(exc) == "http_4xx"

    def test_classifies_http_5xx(self):
        import httpx
        from app.plex_generator.generator import _classify_image_error

        req = httpx.Request("GET", "http://x/")
        resp = httpx.Response(503, request=req)
        exc = httpx.HTTPStatusError("server error", request=req, response=resp)
        assert _classify_image_error(exc) == "http_5xx"

    def test_classifies_connect_error(self):
        import httpx
        from app.plex_generator.generator import _classify_image_error

        assert _classify_image_error(httpx.ConnectError("dns fail")) == "connect_error"

    def test_classifies_timeout(self):
        import httpx
        from app.plex_generator.generator import _classify_image_error

        assert _classify_image_error(httpx.ConnectTimeout("slow")) == "timeout"

    def test_classifies_other(self):
        from app.plex_generator.generator import _classify_image_error

        assert _classify_image_error(ValueError("anything else")) == "other"


class TestNamingWithSuffix:
    def test_movie_folder_with_suffix(self):
        from app.plex_generator.naming import movie_folder
        assert movie_folder("Les Experts", 2000, suffix="HD") == "Les Experts (2000) (HD)"

    def test_movie_folder_with_fallback(self):
        from app.plex_generator.naming import movie_folder
        assert movie_folder("Foo", 2020, fallback_id="vod_42") == "Foo (2020) [vod_42]"

    def test_series_folder_canonical(self):
        from app.plex_generator.naming import series_folder
        assert series_folder("Les Experts", year=2000) == "Les Experts (2000)"

    def test_series_folder_with_suffix(self):
        from app.plex_generator.naming import series_folder
        assert series_folder("Les Experts", year=2000, suffix="US") == \
            "Les Experts (2000) (US)"

    def test_movie_path_with_suffix(self):
        from app.plex_generator.naming import movie_path
        assert movie_path("Les Experts", 2000, suffix="HD") == \
            "Films/Les Experts (2000) (HD)/Les Experts (2000) (HD).strm"

    def test_series_episode_path_with_suffix(self):
        from app.plex_generator.naming import series_episode_path
        assert series_episode_path("Les Experts", 1, 1, year=2000, suffix="US") == (
            "Series/Les Experts (2000) (US)/Season 01/"
            "Les Experts (2000) (US) S01E01.strm"
        )


class TestResolveCollisions:
    def _make_movie(self, source_id, title, year=None):
        # Lazy local import keeps the test file's top imports unchanged.
        from app.plex_generator.models import PlexMovie
        return PlexMovie(
            source_id=source_id, title=title, year=year, stream_url="http://x/",
        )

    def test_singleton_drops_suffix(self):
        from app.plex_generator.generator import _resolve_movie_names
        m = self._make_movie("vod_1", "Les Experts (2000) (US)")
        res = _resolve_movie_names([m])
        r = res["vod_1"]
        assert r.clean_title == "Les Experts"
        assert r.year == 2000
        assert r.suffix is None  # singleton -> Jellyfin clean
        assert r.fallback_id is None

    def test_two_with_distinct_suffix_keeps_both(self):
        from app.plex_generator.generator import _resolve_movie_names
        a = self._make_movie("vod_a", "Les Experts (2000) (US)")
        b = self._make_movie("vod_b", "Les Experts (2000) (HD)")
        res = _resolve_movie_names([a, b])
        assert res["vod_a"].suffix == "US"
        assert res["vod_a"].fallback_id is None
        assert res["vod_b"].suffix == "HD"
        assert res["vod_b"].fallback_id is None

    def test_collision_without_suffix_uses_fallback(self):
        from app.plex_generator.generator import _resolve_movie_names
        a = self._make_movie("vod_1.mkv", "Les Experts (2000)")
        b = self._make_movie("vod_2.mkv", "Les Experts (2000)")
        res = _resolve_movie_names([a, b])
        assert res["vod_1.mkv"].fallback_id == "vod_1"
        assert res["vod_2.mkv"].fallback_id == "vod_2"

    def test_three_same_suffix_breaks_tie_with_fallback(self):
        from app.plex_generator.generator import _resolve_movie_names
        a = self._make_movie("vod_a.mkv", "Les Experts (2000) (US)")
        b = self._make_movie("vod_b.mkv", "Les Experts (2000) (US)")
        c = self._make_movie("vod_c.mkv", "Les Experts (2000) (HD)")
        res = _resolve_movie_names([a, b, c])
        # a and b share suffix US -> need fallback
        assert res["vod_a.mkv"].suffix == "US"
        assert res["vod_a.mkv"].fallback_id == "vod_a"
        assert res["vod_b.mkv"].suffix == "US"
        assert res["vod_b.mkv"].fallback_id == "vod_b"
        # c is the only HD -> just suffix
        assert res["vod_c.mkv"].suffix == "HD"
        assert res["vod_c.mkv"].fallback_id is None


class TestAdultTagging:
    """Adult/XXX movies get the "[XXX] " tag on the generated folder, the .strm
    file names AND the movie.nfo <title> — not only in API responses."""

    def test_nfo_title_prefixed_for_adult_movie(self):
        movie = PlexMovie(
            source_id="vod_x.mp4", title="Naughty Film", is_adult=True,
            year=2020, stream_url="http://x",
        )
        xml = build_movie_nfo(movie)
        assert "<title>[XXX] Naughty Film</title>" in xml

    def test_nfo_title_not_prefixed_for_non_adult(self):
        movie = PlexMovie(
            source_id="vod_y.mp4", title="Family Film", year=2020, stream_url="http://x",
        )
        xml = build_movie_nfo(movie)
        assert "<title>Family Film</title>" in xml
        assert "[XXX]" not in xml

    def test_nfo_title_idempotent_when_already_prefixed(self):
        movie = PlexMovie(
            source_id="vod_z.mp4", title="[XXX] Already", is_adult=True,
            year=2020, stream_url="http://x",
        )
        xml = build_movie_nfo(movie)
        assert "<title>[XXX] Already</title>" in xml
        assert "[XXX] [XXX]" not in xml

    def test_resolve_names_prefixes_adult_title(self):
        from app.plex_generator.generator import _resolve_movie_names
        m = PlexMovie(
            source_id="vod_1", title="Naughty Film", is_adult=True,
            year=2020, stream_url="http://x",
        )
        res = _resolve_movie_names([m])
        assert res["vod_1"].clean_title == "[XXX] Naughty Film"
        assert res["vod_1"].year == 2020

    def test_generate_adult_movie_folder_and_file_tagged(self, tmp_path):
        movie = PlexMovie(
            source_id="vod_1.mp4", title="Naughty Film", is_adult=True,
            year=2020, stream_url="http://stream/adult",
        )
        source = MockSource(movies=[movie])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(source, storage, tmp_path, strm_only=False)
        report = asyncio.run(gen.generate())

        assert report.created == 1
        folder = tmp_path / "Films" / "[XXX] Naughty Film (2020)"
        assert (folder / "[XXX] Naughty Film (2020).strm").exists()
        nfo = folder / "movie.nfo"
        assert nfo.exists()
        assert "<title>[XXX] Naughty Film</title>" in nfo.read_text(encoding="utf-8")

    def test_generate_adult_multi_version_tagged(self, tmp_path):
        from app.plex_generator.models import PlexMovieVersion
        movie = PlexMovie(
            source_id="grp_1", title="Naughty Film", is_adult=True, year=2020,
            versions=[
                PlexMovieVersion(
                    source_id="vod_1.mp4", server_id="s1", label="VF",
                    stream_url="http://a",
                ),
                PlexMovieVersion(
                    source_id="vod_2.mp4", server_id="s2", label="VOSTFR",
                    stream_url="http://b",
                ),
            ],
        )
        source = MockSource(movies=[movie])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        report = asyncio.run(gen.generate())

        assert report.created == 2
        folder = tmp_path / "Films" / "[XXX] Naughty Film (2020)"
        assert (folder / "[XXX] Naughty Film (2020) - VF.strm").exists()
        assert (folder / "[XXX] Naughty Film (2020) - VOSTFR.strm").exists()

    def test_adult_and_non_adult_homonym_land_in_distinct_folders(self, tmp_path):
        adult = PlexMovie(
            source_id="vod_a.mp4", title="Twins", is_adult=True, year=2020,
            stream_url="http://adult",
        )
        clean = PlexMovie(
            source_id="vod_b.mp4", title="Twins", is_adult=False, year=2020,
            stream_url="http://clean",
        )
        source = MockSource(movies=[adult, clean])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)
        asyncio.run(gen.generate())

        assert (tmp_path / "Films" / "[XXX] Twins (2020)" / "[XXX] Twins (2020).strm").exists()
        assert (tmp_path / "Films" / "Twins (2020)" / "Twins (2020).strm").exists()


class TestGenerateOffloadsBlockingIO:
    """CR-C01 guard: generate()'s fsync/rmtree-heavy write phase must run off
    the event loop (asyncio.to_thread), not inline, or a concurrent coroutine
    (e.g. the /api/health handler) stalls for the whole write phase."""

    def test_event_loop_stays_responsive_during_generate(self, tmp_path):
        import time

        class SlowStorage(LocalStorage):
            """Same as LocalStorage but each write sleeps briefly to simulate
            the fsync cost of a large library, so a blocked loop is measurable."""

            def write_strm(self, rel_path: str, url: str) -> None:
                time.sleep(0.02)
                super().write_strm(rel_path, url)

        movies = [
            _make_movie(sid=f"vod_{i}.mp4", title=f"Movie {i}", year=2020)
            for i in range(20)
        ]
        source = MockSource(movies=movies)
        storage = SlowStorage(tmp_path)
        gen = PlexLibraryGenerator(source, storage, tmp_path, strm_only=True)

        async def _run():
            ticks = {"n": 0}
            stop = asyncio.Event()

            async def ticker():
                while not stop.is_set():
                    ticks["n"] += 1
                    await asyncio.sleep(0.01)

            ticker_task = asyncio.create_task(ticker())
            report = await gen.generate()
            stop.set()
            await ticker_task
            return report, ticks["n"]

        report, tick_count = asyncio.run(_run())

        assert report.created == 20
        # 20 movies * 0.02s = ~0.4s of blocking work. If it ran inline on the
        # loop, the ticker would get essentially 0 ticks during that window.
        # Offloaded via asyncio.to_thread, the loop keeps ticking concurrently.
        assert tick_count >= 10


class TestForceRefreshMetadata:
    """AUDIT-P6-006: opt-in refresh policy for generated NFO files.

    `write_file`/`download_image` preserve any existing generated file
    forever, so a re-enrichment (new OMDb notes, ids corrected by
    validate_id_consistency, blended display_rating...) never reaches disk
    until an operator manually deletes the NFO. These tests pin: (1) the
    default stays byte-for-byte the historical "preserve" behaviour, (2) the
    opt-in `force_refresh_metadata=True` flag actually refreshes NFO content,
    (3) images are NEVER refreshed by this flag under any circumstance, and
    (4) the `[XXX]` adult tag survives a refresh (piège 15)."""

    def _movie(self, summary: str, is_adult: bool = False, poster_url: str | None = None):
        return PlexMovie(
            source_id="vod_1.mp4", title="Dune", year=2021,
            stream_url="http://stream/1", summary=summary, is_adult=is_adult,
            poster_url=poster_url,
        )

    def test_default_does_not_rewrite_existing_nfo(self, tmp_path):
        source = MockSource(movies=[self._movie("Original summary")])
        storage = LocalStorage(tmp_path)
        gen1 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=False)
        asyncio.run(gen1.generate())

        nfo_path = tmp_path / "Films" / "Dune (2021)" / "movie.nfo"
        assert "Original summary" in nfo_path.read_text(encoding="utf-8")

        # Re-enrichment changes the summary in the source, but a plain re-run
        # (force_refresh_metadata defaults False) must leave the NFO exactly
        # as it was — this is the "strictly unchanged by default" contract.
        source2 = MockSource(movies=[self._movie("New enriched summary")])
        gen2 = PlexLibraryGenerator(source2, storage, tmp_path, strm_only=False)
        report2 = asyncio.run(gen2.generate())

        content = nfo_path.read_text(encoding="utf-8")
        assert "Original summary" in content
        assert "New enriched summary" not in content
        # NFO writes stay invisible to the report by default, exactly as
        # before this feature existed — only the (unchanged) .strm counts.
        assert report2.updated == 0
        assert report2.created == 0
        assert report2.unchanged == 1

    def test_force_refresh_rewrites_nfo_content(self, tmp_path):
        source = MockSource(movies=[self._movie("Original summary")])
        storage = LocalStorage(tmp_path)
        gen1 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=False)
        asyncio.run(gen1.generate())

        nfo_path = tmp_path / "Films" / "Dune (2021)" / "movie.nfo"
        assert "Original summary" in nfo_path.read_text(encoding="utf-8")

        source2 = MockSource(movies=[self._movie("New enriched summary")])
        gen2 = PlexLibraryGenerator(
            source2, storage, tmp_path, strm_only=False, force_refresh_metadata=True,
        )
        report2 = asyncio.run(gen2.generate())

        content = nfo_path.read_text(encoding="utf-8")
        assert "New enriched summary" in content
        assert "Original summary" not in content
        # The refresh must be honestly reflected as `updated`, never silently
        # folded into `unchanged` — that's the whole point of this finding.
        assert report2.updated == 1

    def test_force_refresh_new_file_counts_as_created(self, tmp_path):
        source = MockSource(movies=[self._movie("Only summary")])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(
            source, storage, tmp_path, strm_only=False, force_refresh_metadata=True,
        )
        report = asyncio.run(gen.generate())

        # First-ever run: nothing pre-existed, so the .nfo counts as created,
        # same bucket the .strm creation lands in.
        assert report.created == 2  # 1 .strm + 1 .nfo

    def test_force_refresh_never_touches_poster_image(self, tmp_path):
        poster_rel = movie_poster_path("Dune", 2021)
        poster_full = tmp_path / poster_rel
        poster_full.parent.mkdir(parents=True, exist_ok=True)
        sentinel = b"\xff\xd8\xff-hand-retouched-by-tinyMediaManager"
        poster_full.write_bytes(sentinel)

        source = MockSource(movies=[
            self._movie("Some summary", poster_url="http://example.invalid/poster.jpg"),
        ])
        storage = LocalStorage(tmp_path)
        gen = PlexLibraryGenerator(
            source, storage, tmp_path, strm_only=False, force_refresh_metadata=True,
        )
        asyncio.run(gen.generate())

        # download_image/submit_image_download take no `force` parameter at
        # all — an existing poster short-circuits before any network call,
        # regardless of force_refresh_metadata. No network access is even
        # attempted here (poster.jpg already existed), so this test needs no
        # HTTP mocking; if the guard regressed, this write would attempt a
        # real request to example.invalid and fail loudly instead of silently
        # preserving the sentinel bytes.
        assert poster_full.read_bytes() == sentinel

    def test_force_refresh_preserves_adult_title_prefix(self, tmp_path):
        """Piège §9-15: a refreshed NFO must keep re-applying the [XXX] tag —
        it's re-derived from `is_adult` on every build_movie_nfo() call, not
        cached from the first write, so a refresh can't silently drop it."""
        source = MockSource(movies=[self._movie("Original", is_adult=True)])
        storage = LocalStorage(tmp_path)
        gen1 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=False)
        asyncio.run(gen1.generate())

        nfo_path = tmp_path / "Films" / "[XXX] Dune (2021)" / "movie.nfo"
        assert "<title>[XXX] Dune</title>" in nfo_path.read_text(encoding="utf-8")

        source2 = MockSource(movies=[self._movie("Refreshed", is_adult=True)])
        gen2 = PlexLibraryGenerator(
            source2, storage, tmp_path, strm_only=False, force_refresh_metadata=True,
        )
        asyncio.run(gen2.generate())

        content = nfo_path.read_text(encoding="utf-8")
        assert "<title>[XXX] Dune</title>" in content
        assert "Refreshed" in content

    def test_force_refresh_series_and_episode_nfo_rewritten(self, tmp_path):
        series = _make_series()
        source = MockSource(series=[series])
        storage = LocalStorage(tmp_path)
        gen1 = PlexLibraryGenerator(source, storage, tmp_path, strm_only=False)
        asyncio.run(gen1.generate())

        tvshow_path = tmp_path / "Series" / "Test Series" / "tvshow.nfo"
        ep1_nfo = (
            tmp_path / "Series" / "Test Series" / "Season 01"
            / "Test Series S01E01.nfo"
        )
        assert tvshow_path.exists()
        assert ep1_nfo.exists()
        original_tvshow = tvshow_path.read_text(encoding="utf-8")
        original_ep1 = ep1_nfo.read_text(encoding="utf-8")

        # Rebuild with an episode whose summary changed.
        changed_series = _make_series(episodes=[
            PlexEpisode(
                source_id="ep_1.mkv", series_title="Test Series",
                season_num=1, episode_num=1, stream_url="http://stream/ep1",
                summary="Changed episode summary",
            ),
            PlexEpisode(
                source_id="ep_2.mkv", series_title="Test Series",
                season_num=1, episode_num=2, stream_url="http://stream/ep2",
            ),
        ])
        source2 = MockSource(series=[changed_series])
        gen2 = PlexLibraryGenerator(
            source2, storage, tmp_path, strm_only=False, force_refresh_metadata=True,
        )
        report2 = asyncio.run(gen2.generate())

        assert ep1_nfo.read_text(encoding="utf-8") != original_ep1
        assert "Changed episode summary" in ep1_nfo.read_text(encoding="utf-8")
        # tvshow.nfo content is identical here (nothing series-level changed)
        # but force=True still rewrites it — content stays correct either way.
        assert tvshow_path.read_text(encoding="utf-8") == original_tvshow
        # tvshow.nfo + 2 episode NFOs, all pre-existing -> all rewritten and
        # honestly counted as "updated" (both .strm URLs are unchanged, so no
        # .strm contributes to this count).
        assert report2.updated == 3
