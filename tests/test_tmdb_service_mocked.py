"""Mock TMDB HTTP via respx; test search scoring (ScraperMatcher-style) + tie-break."""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.services.tmdb_service import TMDBService


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def configured_tmdb(monkeypatch):
    """A fresh TMDBService with an API key set, isolated from the global singleton."""
    from app.services import tmdb_service as mod

    monkeypatch.setattr(mod.settings, "TMDB_API_KEY", "test_key")
    monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")
    svc = TMDBService()
    try:
        yield svc
    finally:
        await svc.close()


def _movie(id_, title, date="", overview="", vote=0, original=None):
    r = {"id": id_, "title": title, "release_date": date, "vote_count": vote, "overview": overview}
    if original is not None:
        r["original_title"] = original
    return r


async def test_search_movie_returns_match(configured_tmdb, tmdb_mock):
    tmdb_mock.get("/3/search/movie").respond(
        200, json={"results": [_movie(12345, "The Matrix", "1999-03-31", vote=100)]},
    )
    outcome = await configured_tmdb.search_movie("The Matrix", 1999)
    assert outcome.result == "matched"
    assert outcome.match.tmdb_id == 12345
    assert outcome.match.year == 1999
    assert outcome.confidence > 0.85


async def test_search_movie_caches_result(configured_tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/search/movie").respond(
        200, json={"results": [_movie(99, "Foo", "2020-01-01")]},
    )
    first = await configured_tmdb.search_movie("Foo", 2020)
    second = await configured_tmdb.search_movie("Foo", 2020)
    assert first == second
    assert route.call_count == 1  # cache hit, no second HTTP call


async def test_search_movie_caches_no_match(configured_tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/search/movie").respond(200, json={"results": []})
    o1 = await configured_tmdb.search_movie("Nope", None)
    o2 = await configured_tmdb.search_movie("Nope", None)
    assert o1.result == "nomatch" and o1.match is None
    assert o1 == o2
    assert route.call_count == 1


async def test_search_movie_returns_nomatch_when_not_configured(monkeypatch):
    from app.services import tmdb_service as mod

    monkeypatch.setattr(mod.settings, "TMDB_API_KEY", "")
    svc = TMDBService()
    outcome = await svc.search_movie("anything", None)
    assert outcome.result == "nomatch"
    assert outcome.match is None


async def test_wrong_year_blocks_automatch(configured_tmdb, tmdb_mock):
    """Hairspray 1988 vs a 2007 candidate → year mismatch zeroes the year score."""
    tmdb_mock.get("/3/search/movie").respond(
        200, json={"results": [_movie(2, "Hairspray", "2007-07-20")]},
    )
    outcome = await configured_tmdb.search_movie("Hairspray", 1988)
    assert outcome.result == "nomatch"   # 0.7*1.0 + 0.3*0.0 = 0.70 < 0.85
    assert outcome.best is not None       # best score still recorded


async def test_matches_via_original_title(configured_tmdb, tmdb_mock):
    tmdb_mock.get("/3/search/movie").respond(
        200, json={"results": [
            _movie(5, "El Rey León", "1994-06-24", original="The Lion King", vote=500),
        ]},
    )
    outcome = await configured_tmdb.search_movie("The Lion King", 1994)
    assert outcome.result == "matched"
    assert outcome.match.tmdb_id == 5


async def test_token_set_handles_word_order(configured_tmdb, tmdb_mock):
    tmdb_mock.get("/3/search/movie").respond(
        200, json={"results": [_movie(8, "Spider-Man: No Way Home", "2021-12-15", vote=900)]},
    )
    outcome = await configured_tmdb.search_movie("No Way Home Spider Man", 2021)
    assert outcome.result == "matched"
    assert outcome.match.tmdb_id == 8


async def test_homonyms_are_ambiguous_without_summary(configured_tmdb, tmdb_mock):
    tmdb_mock.get("/3/search/movie").respond(
        200, json={"results": [
            _movie(1, "Crash", overview="A car-accident fetish subculture."),
            _movie(2, "Crash", overview="Racial tensions collide in Los Angeles."),
        ]},
    )
    outcome = await configured_tmdb.search_movie("Crash", None)
    assert outcome.result == "ambiguous"  # equal scores, margin < 0.05
    assert outcome.match is None


async def test_summary_breaks_the_tie(configured_tmdb, tmdb_mock):
    tmdb_mock.get("/3/search/movie").respond(
        200, json={"results": [
            _movie(1, "Crash", overview="A car-accident fetish subculture."),
            _movie(2, "Crash", overview="Racial tensions collide in Los Angeles over several days."),
        ]},
    )
    outcome = await configured_tmdb.search_movie(
        "Crash", None,
        summary="Racial tensions collide in Los Angeles among strangers.",
    )
    assert outcome.result == "matched"
    assert outcome.match.tmdb_id == 2   # summary picks the LA drama, not the fetish film


async def test_429_with_retry_after(configured_tmdb, tmdb_mock, monkeypatch):
    from app.services import tmdb_service as mod

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    import httpx
    route = tmdb_mock.get("/3/search/movie")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"results": [_movie(7, "Bar", "2010-05-05")]}),
    ]
    outcome = await configured_tmdb.search_movie("Bar", 2010)
    assert outcome.result == "matched"
    assert outcome.match.tmdb_id == 7
    assert route.call_count == 2
