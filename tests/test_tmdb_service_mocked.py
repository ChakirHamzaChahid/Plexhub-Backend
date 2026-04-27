"""Demo: mock TMDB HTTP via respx, test the service end-to-end.

Pattern for future worker tests (sync_worker, enrichment_worker).
"""
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


async def test_search_movie_returns_match(configured_tmdb, tmdb_mock):
    tmdb_mock.get("/3/search/movie").respond(
        200,
        json={
            "results": [
                {
                    "id": 12345,
                    "title": "The Matrix",
                    "release_date": "1999-03-31",
                    "popularity": 50.0,
                },
            ],
        },
    )
    match = await configured_tmdb.search_movie("The Matrix", 1999)
    assert match is not None
    assert match.tmdb_id == 12345
    assert match.title == "The Matrix"
    assert match.year == 1999
    assert match.confidence > 0.85


async def test_search_movie_caches_result(configured_tmdb, tmdb_mock):
    """A second call with the same (title, year) must hit cache, not the network."""
    route = tmdb_mock.get("/3/search/movie").respond(
        200,
        json={
            "results": [
                {"id": 99, "title": "Foo", "release_date": "2020-01-01", "popularity": 1.0},
            ],
        },
    )
    first = await configured_tmdb.search_movie("Foo", 2020)
    second = await configured_tmdb.search_movie("Foo", 2020)
    assert first == second
    assert route.call_count == 1  # cache hit, no second HTTP call


async def test_search_movie_caches_no_match(configured_tmdb, tmdb_mock):
    """Cached `None` must remain distinguishable from a miss — second call cached."""
    route = tmdb_mock.get("/3/search/movie").respond(200, json={"results": []})
    assert await configured_tmdb.search_movie("Nope", None) is None
    assert await configured_tmdb.search_movie("Nope", None) is None
    assert route.call_count == 1


async def test_search_movie_returns_none_when_not_configured(monkeypatch):
    """No API key → return None without hitting the network."""
    from app.services import tmdb_service as mod

    monkeypatch.setattr(mod.settings, "TMDB_API_KEY", "")
    svc = TMDBService()
    assert await svc.search_movie("anything", None) is None


async def test_429_with_retry_after(configured_tmdb, tmdb_mock, monkeypatch):
    """A 429 response should be retried after the Retry-After delay (mocked to 0)."""
    # Speed test up by stubbing asyncio.sleep
    from app.services import tmdb_service as mod

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    route = tmdb_mock.get("/3/search/movie")
    route.side_effect = [
        # First call: 429 with Retry-After
        __import__("httpx").Response(429, headers={"Retry-After": "1"}),
        # Second call: ok
        __import__("httpx").Response(
            200,
            json={"results": [
                {"id": 7, "title": "Bar", "release_date": "2010-05-05", "popularity": 1.0},
            ]},
        ),
    ]
    match = await configured_tmdb.search_movie("Bar", 2010)
    assert match is not None
    assert match.tmdb_id == 7
    assert route.call_count == 2
