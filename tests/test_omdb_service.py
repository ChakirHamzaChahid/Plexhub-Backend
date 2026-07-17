"""Mock OMDb HTTP via respx; test field parsing, retries, and request-count
budgeting. Mirrors tests/test_tmdb_service_mocked.py's fixture/style."""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio

from app.services.omdb_service import OMDbService


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def configured_omdb(monkeypatch):
    """A fresh OMDbService with an API key set, isolated from the global singleton."""
    from app.services import omdb_service as mod

    monkeypatch.setattr(mod.settings, "OMDB_API_KEY", "test_key")
    svc = OMDbService()
    try:
        yield svc
    finally:
        await svc.close()


def _payload(**overrides):
    base = {
        "Response": "True",
        "Title": "The Matrix",
        "Year": "1999",
        "Runtime": "136 min",
        "Genre": "Action, Sci-Fi",
        "Director": "Lana Wachowski, Lilly Wachowski",
        "Actors": "Keanu Reeves, Laurence Fishburne",
        "Plot": "A computer hacker learns the truth about reality.",
        "imdbRating": "8.7",
        "imdbVotes": "2,000,000",
        "Type": "movie",
    }
    base.update(overrides)
    return base


async def test_get_by_imdb_id_parses_all_fields(configured_omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json=_payload())
    data = await configured_omdb.get_by_imdb_id("tt0133093")
    assert data is not None
    assert data.title == "The Matrix"
    assert data.year == "1999"
    assert data.runtime_minutes == 136
    assert data.genre == "Action, Sci-Fi"
    assert data.director == "Lana Wachowski, Lilly Wachowski"
    assert data.actors == "Keanu Reeves, Laurence Fishburne"
    assert data.plot == "A computer hacker learns the truth about reality."
    assert data.imdb_rating == 8.7
    assert data.imdb_votes == 2000000
    assert data.type == "movie"


async def test_get_by_imdb_id_handles_n_a_fields(configured_omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json=_payload(
        Runtime="N/A", Genre="N/A", Director="N/A", Actors="N/A",
        Plot="N/A", imdbRating="N/A", imdbVotes="N/A",
    ))
    data = await configured_omdb.get_by_imdb_id("tt0133093")
    assert data is not None
    assert data.runtime_minutes is None
    assert data.genre is None
    assert data.director is None
    assert data.actors is None
    assert data.plot is None
    assert data.imdb_rating is None
    assert data.imdb_votes is None
    # Title/Year/Type are never "N/A" in practice on a Response:"True" body,
    # so they pass through raw (no N/A-stripping needed for those fields).
    assert data.title == "The Matrix"


async def test_get_by_imdb_id_not_found_returns_none(configured_omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json={"Response": "False", "Error": "Incorrect IMDb ID."})
    data = await configured_omdb.get_by_imdb_id("tt0000000")
    assert data is None


async def test_get_by_imdb_id_returns_none_when_not_configured(monkeypatch):
    from app.services import omdb_service as mod

    monkeypatch.setattr(mod.settings, "OMDB_API_KEY", "")
    svc = OMDbService()
    data = await svc.get_by_imdb_id("tt0133093")
    assert data is None


async def test_get_by_imdb_id_blank_id_short_circuits(configured_omdb):
    data = await configured_omdb.get_by_imdb_id("")
    assert data is None
    assert configured_omdb.get_request_count() == 0


async def test_unconfigured_makes_zero_http_calls(monkeypatch, omdb_mock):
    from app.services import omdb_service as mod

    monkeypatch.setattr(mod.settings, "OMDB_API_KEY", "")
    svc = OMDbService()
    route = omdb_mock.get("/").respond(200, json=_payload())
    data = await svc.get_by_imdb_id("tt0133093")
    assert data is None
    assert route.call_count == 0


async def test_429_with_retry_after(configured_omdb, omdb_mock, monkeypatch):
    from app.services import omdb_service as mod

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    import httpx
    route = omdb_mock.get("/")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json=_payload()),
    ]
    data = await configured_omdb.get_by_imdb_id("tt0133093")
    assert data is not None
    assert data.title == "The Matrix"
    assert route.call_count == 2


async def test_5xx_retried_then_succeeds(configured_omdb, omdb_mock, monkeypatch):
    from app.services import omdb_service as mod

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    import httpx
    route = omdb_mock.get("/")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=_payload()),
    ]
    data = await configured_omdb.get_by_imdb_id("tt0133093")
    assert data is not None
    assert route.call_count == 2


async def test_hard_404_returns_none_without_retry(configured_omdb, omdb_mock):
    """A 404 is NOT in the retryable 5xx set (502/503/504) — mirrors
    `tmdb_service._request`, which re-raises immediately (`else: raise`) for
    any HTTPStatusError outside that set, regardless of remaining retry
    budget. `get_by_imdb_id` then catches it and returns None (graceful,
    matching `tmdb_service.find_by_imdb_id`'s broad catch-and-return-None
    semantics rather than `search_movie`'s propagate-on-error semantics)."""
    route = omdb_mock.get("/").respond(404)
    data = await configured_omdb.get_by_imdb_id("tt0133093")
    assert data is None
    assert route.call_count == 1


async def test_request_count_tracks_every_retry_not_one_per_call(
    configured_omdb, omdb_mock, monkeypatch,
):
    """Mirrors CR-F03 accounting on the TMDB side: every real HTTP attempt
    (retries included) bumps the counter, not one increment per logical
    `get_by_imdb_id()` call."""
    from app.services import omdb_service as mod

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    import httpx
    route = omdb_mock.get("/")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(503),
        httpx.Response(200, json=_payload()),
    ]
    assert configured_omdb.get_request_count() == 0

    data = await configured_omdb.get_by_imdb_id("tt0133093")

    assert data is not None
    assert route.call_count == 3
    assert configured_omdb.get_request_count() == 3


async def test_reset_request_count(configured_omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json=_payload())
    await configured_omdb.get_by_imdb_id("tt0133093")
    assert configured_omdb.get_request_count() > 0

    configured_omdb.reset_request_count()
    assert configured_omdb.get_request_count() == 0


async def test_api_key_never_leaks_on_hard_http_error(configured_omdb, omdb_mock, caplog):
    """A hard HTTP failure must never leak `apikey=test_key` via any log
    record. `httpx.HTTPStatusError.__str__` embeds the full request URL
    (including the query string, which carries `apikey`) — the service must
    log exception type / status code only, never `str(exc)` directly."""
    omdb_mock.get("/").respond(401, json={"Response": "False", "Error": "Invalid API key!"})
    with caplog.at_level(logging.WARNING, logger="plexhub.omdb"):
        data = await configured_omdb.get_by_imdb_id("tt0133093")
    assert data is None
    assert caplog.records  # sanity: the failure path did log something
    for record in caplog.records:
        assert "test_key" not in record.getMessage()
