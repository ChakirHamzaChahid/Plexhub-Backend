"""ADR 0005 D2 (W0) — `TMDBService.search_candidates` / `get_match_extras`
+ the `count_requests()` ContextVar tally, all mocked via respx."""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from app.services import tmdb_service as tmdb_mod
from app.services.tmdb_service import TMDBService, count_requests


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def configured_tmdb(monkeypatch):
    monkeypatch.setattr(tmdb_mod.settings, "TMDB_API_KEY", "test_key")
    monkeypatch.setattr(tmdb_mod.settings, "TMDB_LANGUAGE", "en-US")
    svc = TMDBService()
    try:
        yield svc
    finally:
        await svc.close()


def _movie(id_, title, date="", overview="", vote=0, poster=None, original=None):
    r = {"id": id_, "title": title, "release_date": date, "vote_count": vote, "overview": overview}
    if poster is not None:
        r["poster_path"] = poster
    if original is not None:
        r["original_title"] = original
    return r


class TestSearchCandidates:
    async def test_unconfigured_returns_empty(self, monkeypatch, tmdb_mock):
        monkeypatch.setattr(tmdb_mod.settings, "TMDB_API_KEY", "")
        svc = TMDBService()
        result = await svc.search_candidates("movie", "The Matrix", 1999)
        assert result.verdict.result == "nomatch"
        assert result.candidates == []
        await svc.close()

    async def test_movie_search_one_call_scored_sorted(self, configured_tmdb, tmdb_mock):
        route = tmdb_mock.get("/3/search/movie").respond(200, json={"results": [
            _movie(1, "The Matrix", "1999-03-31", vote=1000, poster="/m1.jpg"),
            _movie(2, "The Matrix Reloaded", "2003-05-15", vote=800, poster="/m2.jpg"),
        ]})
        result = await configured_tmdb.search_candidates("movie", "The Matrix", 1999)
        assert route.call_count == 1
        assert result.verdict.result == "matched"
        assert len(result.candidates) == 2
        assert result.candidates[0].tmdb_id == 1
        assert result.candidates[0].poster_path == "/m1.jpg"
        assert result.candidates[0].kind == "movie"
        # confidence desc.
        assert result.candidates[0].confidence >= result.candidates[1].confidence

    async def test_tv_search_uses_first_air_date_year_param(self, configured_tmdb, tmdb_mock):
        route = tmdb_mock.get("/3/search/tv").respond(200, json={"results": [
            {"id": 42, "name": "Breaking Bad", "first_air_date": "2008-01-20", "vote_count": 500, "overview": "..."},
        ]})
        result = await configured_tmdb.search_candidates("tv", "Breaking Bad", 2008)
        req = route.calls.last.request
        assert "first_air_date_year=2008" in str(req.url)
        assert result.candidates[0].kind == "tv"

    async def test_no_year_omits_year_param(self, configured_tmdb, tmdb_mock):
        route = tmdb_mock.get("/3/search/movie").respond(200, json={"results": []})
        await configured_tmdb.search_candidates("movie", "Some Title", None)
        req = route.calls.last.request
        assert "year=" not in str(req.url)

    async def test_no_search_cache_always_fresh(self, configured_tmdb, tmdb_mock):
        """Unlike search_movie/search_tv, search_candidates never serves from
        `_search_cache` — every call is a fresh HTTP request."""
        route = tmdb_mock.get("/3/search/movie").respond(200, json={"results": [
            _movie(1, "Foo", "2020-01-01"),
        ]})
        await configured_tmdb.search_candidates("movie", "Foo", 2020)
        await configured_tmdb.search_candidates("movie", "Foo", 2020)
        assert route.call_count == 2


class TestGetMatchExtras:
    async def test_unconfigured_returns_none(self, monkeypatch):
        monkeypatch.setattr(tmdb_mod.settings, "TMDB_API_KEY", "")
        svc = TMDBService()
        assert await svc.get_match_extras(603, "movie") is None
        await svc.close()

    async def test_movie_extras_parsed(self, configured_tmdb, tmdb_mock):
        route = tmdb_mock.get("/3/movie/603").respond(200, json={
            "id": 603,
            "title": "The Matrix",
            "original_title": "The Matrix",
            "release_date": "1999-03-31",
            "overview": "A computer hacker learns...",
            "poster_path": "/main.jpg",
            "external_ids": {"imdb_id": "tt0133093"},
            "images": {"posters": [
                {"file_path": "/main.jpg"},   # dup of main poster -> deduped
                {"file_path": "/alt1.jpg"},
                {"file_path": "/alt2.jpg"},
            ]},
        })
        extras = await configured_tmdb.get_match_extras(603, "movie")
        assert extras is not None
        assert extras.tmdb_id == 603
        assert extras.kind == "movie"
        assert extras.imdb_id == "tt0133093"
        assert extras.year == 1999
        assert extras.poster_urls == [
            "https://image.tmdb.org/t/p/w185/main.jpg",
            "https://image.tmdb.org/t/p/w185/alt1.jpg",
            "https://image.tmdb.org/t/p/w185/alt2.jpg",
        ]
        req = route.calls.last.request
        assert "append_to_response=external_ids%2Cimages" in str(req.url)
        # api_key IS expected here — this is the metadata client, not the
        # dedicated image-fetch client (ADR 0005 D5/F2, W3 concern).
        assert "api_key=test_key" in str(req.url)

    async def test_imdb_id_gets_tt_prefix_when_missing(self, configured_tmdb, tmdb_mock):
        tmdb_mock.get("/3/tv/42").respond(200, json={
            "id": 42, "name": "Show", "first_air_date": "2010-01-01",
            "external_ids": {"imdb_id": "0111161"},
        })
        extras = await configured_tmdb.get_match_extras(42, "tv")
        assert extras.imdb_id == "tt0111161"

    async def test_404_returns_none(self, configured_tmdb, tmdb_mock):
        tmdb_mock.get("/3/movie/999999").respond(404, json={"status_message": "not found"})
        assert await configured_tmdb.get_match_extras(999999, "movie") is None

    async def test_poster_urls_capped_and_deduped(self, configured_tmdb, tmdb_mock):
        posters = [{"file_path": f"/p{i}.jpg"} for i in range(15)]
        tmdb_mock.get("/3/movie/1").respond(200, json={
            "id": 1, "title": "T", "release_date": "",
            "images": {"posters": posters},
        })
        extras = await configured_tmdb.get_match_extras(1, "movie")
        assert len(extras.poster_urls) == 10


class TestCountRequestsTally:
    async def test_counts_search_and_details_calls(self, configured_tmdb, tmdb_mock):
        tmdb_mock.get("/3/search/movie").respond(200, json={"results": []})
        tmdb_mock.get("/3/movie/1").respond(200, json={"id": 1, "title": "T", "release_date": ""})

        with count_requests() as tally:
            assert tally.count == 0
            await configured_tmdb.search_movie("Foo", None)
            await configured_tmdb.get_movie_details(1)
        assert tally.count == 2
        # Never resets/reads the global run counter.
        assert configured_tmdb.real_request_count >= 2

    async def test_zero_outside_context(self, configured_tmdb, tmdb_mock):
        tmdb_mock.get("/3/search/movie").respond(200, json={"results": []})
        await configured_tmdb.search_movie("Foo", None)
        # No `with count_requests():` active -> nothing crashes, nothing to read.
        assert tmdb_mod._request_tally.get() is None

    async def test_counts_retries_within_one_logical_call(self, configured_tmdb, tmdb_mock, monkeypatch):
        # Force zero sleep so the retry loop runs fast.
        async def _no_sleep(*_a, **_kw):
            return None

        monkeypatch.setattr(tmdb_mod.asyncio, "sleep", _no_sleep)
        route = tmdb_mock.get("/3/search/movie")
        route.side_effect = [
            __import__("httpx").Response(503, json={}),
            __import__("httpx").Response(200, json={"results": []}),
        ]
        with count_requests() as tally:
            await configured_tmdb.search_movie("Foo", None)
        # Initial attempt (503, retried) + successful retry = 2 real attempts.
        assert tally.count == 2
        assert route.call_count == 2

    async def test_shared_across_gathered_tasks(self, configured_tmdb, tmdb_mock):
        route = tmdb_mock.get("/3/search/movie").respond(200, json={"results": []})

        async def _one(title):
            # Distinct titles -> each call is a genuine cache miss (the
            # search cache is keyed on (kind, title, year, lang)), so all
            # three really hit the network.
            await configured_tmdb.search_movie(title, None)

        with count_requests() as tally:
            # `ContextVar` is copied into each task at creation time, so all
            # three share the SAME RequestTally instance.
            await asyncio.gather(_one("Title A"), _one("Title B"), _one("Title C"))
        assert tally.count == 3
        assert route.call_count == 3

    async def test_nested_inner_wins_not_aggregating(self, configured_tmdb, tmdb_mock):
        tmdb_mock.get("/3/search/movie").respond(200, json={"results": []})
        with count_requests() as outer:
            await configured_tmdb.search_movie("Outer call", None)
            with count_requests() as inner:
                await configured_tmdb.search_movie("Inner call", None)
            assert inner.count == 1
            # Outer resumes counting for calls made after the inner block exits.
            await configured_tmdb.search_movie("Outer call 2", None)
        assert outer.count == 2  # the "Inner call" attempt did NOT count on outer
