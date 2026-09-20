"""ADR 0005 D3 (Wave W4) — `OMDbService.search_list` (`?s=` list search).

Mirrors `tests/test_omdb_service.py`'s respx fixture/style. The key
invariant beyond parsing: the OMDb API key never reaches a log record (it
rides on the URL as `apikey`, and `httpx.HTTPStatusError.__str__` embeds the
full request URL — hence the module-wide "never `str(exc)`" rule).
"""
from __future__ import annotations

import logging

import httpx
import pytest_asyncio

from app.services.omdb_service import OMDbService


@pytest_asyncio.fixture
async def configured_omdb(monkeypatch):
    from app.services import omdb_service as mod

    monkeypatch.setattr(mod.settings, "OMDB_API_KEY", "test_key")
    svc = OMDbService()
    try:
        yield svc
    finally:
        await svc.close()


async def _noop_sleep(_seconds):
    return None


def _search_payload(**overrides):
    base = {
        "Response": "True",
        "Search": [
            {
                "Title": "The Matrix", "Year": "1999", "imdbID": "tt0133093",
                "Type": "movie", "Poster": "https://m.media-amazon.com/images/m/matrix.jpg",
            },
            {
                "Title": "The Matrix Reloaded", "Year": "2003", "imdbID": "tt0234215",
                "Type": "movie", "Poster": "N/A",
            },
        ],
    }
    base.update(overrides)
    return base


async def test_search_list_parses_hits(configured_omdb, omdb_mock):
    route = omdb_mock.get("/").respond(200, json=_search_payload())
    hits = await configured_omdb.search_list("The Matrix", 1999, "movie")

    assert [h.imdb_id for h in hits] == ["tt0133093", "tt0234215"]
    assert hits[0].title == "The Matrix"
    assert hits[0].year == 1999
    assert hits[0].type == "movie"
    assert hits[0].poster_url == "https://m.media-amazon.com/images/m/matrix.jpg"
    # "N/A" poster -> None, not the literal sentinel.
    assert hits[1].poster_url is None

    params = route.calls[0].request.url.params
    assert params["s"] == "The Matrix"
    assert params["y"] == "1999"
    assert params["type"] == "movie"


async def test_search_list_maps_show_to_series_and_parses_year_range(
    configured_omdb, omdb_mock,
):
    route = omdb_mock.get("/").respond(200, json=_search_payload(Search=[
        {"Title": "Fargo", "Year": "2014–", "imdbID": "tt2802850", "Type": "series"},
    ]))
    hits = await configured_omdb.search_list("Fargo", None, "show")

    assert route.calls[0].request.url.params["type"] == "series"
    assert "y" not in route.calls[0].request.url.params
    assert hits[0].year == 2014  # only the leading 4 digits of a range
    assert hits[0].poster_url is None


async def test_search_list_drops_entries_without_an_id(configured_omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json=_search_payload(Search=[
        {"Title": "No id here", "Year": "2001", "Type": "movie"},
        {"Title": "Ok", "Year": "2001", "imdbID": "tt1", "Type": "movie"},
        "not-a-dict",
    ]))
    hits = await configured_omdb.search_list("x", None, "movie")
    assert [h.imdb_id for h in hits] == ["tt1"]


async def test_search_list_response_false_returns_empty(configured_omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json={"Response": "False", "Error": "Movie not found!"})
    assert await configured_omdb.search_list("nope", None, "movie") == []


async def test_search_list_blank_query_makes_no_request(configured_omdb, omdb_mock):
    route = omdb_mock.get("/").respond(200, json=_search_payload())
    assert await configured_omdb.search_list("", None, "movie") == []
    assert not route.called


async def test_search_list_unconfigured_makes_no_request(monkeypatch, omdb_mock):
    from app.services import omdb_service as mod

    monkeypatch.setattr(mod.settings, "OMDB_API_KEY", "")
    route = omdb_mock.get("/").respond(200, json=_search_payload())
    svc = OMDbService()
    try:
        assert await svc.search_list("The Matrix", None, "movie") == []
    finally:
        await svc.close()
    assert not route.called


async def test_search_list_respects_budget(configured_omdb, omdb_mock, monkeypatch):
    from app.services import omdb_service as mod

    monkeypatch.setattr(mod.settings, "OMDB_DAILY_LIMIT", 0)
    route = omdb_mock.get("/").respond(200, json=_search_payload())
    assert await configured_omdb.search_list("The Matrix", None, "movie") == []
    assert not route.called


async def test_search_list_http_error_is_empty_and_never_logs_the_key(
    configured_omdb, omdb_mock, caplog,
):
    omdb_mock.get("/").respond(401, json={"Error": "Invalid API key!"})
    with caplog.at_level(logging.DEBUG, logger="plexhub.omdb"):
        assert await configured_omdb.search_list("The Matrix", None, "movie") == []
    # Only this module's own records: httpx's own INFO line echoes the full
    # URL, which is exactly why `main.py` pins the `httpx` logger to WARNING
    # in production (CLAUDE.md §2 / piège 18f).
    blob = " ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("plexhub")
    )
    assert "test_key" not in blob
    assert "apikey" not in blob


async def test_search_list_transport_error_is_empty(
    configured_omdb, omdb_mock, caplog, monkeypatch,
):
    # A ConnectError exhausts the (1, 2, 4)s retry ladder — skip the waits.
    monkeypatch.setattr(
        "app.services.omdb_service.asyncio.sleep", _noop_sleep,
    )
    omdb_mock.get("/").mock(side_effect=httpx.ConnectError("boom"))
    with caplog.at_level(logging.DEBUG, logger="plexhub.omdb"):
        assert await configured_omdb.search_list("The Matrix", None, "movie") == []
    assert "test_key" not in " ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("plexhub")
    )


async def test_search_list_counts_real_attempts_in_tally(configured_omdb, omdb_mock):
    from app.services.omdb_service import count_requests

    omdb_mock.get("/").respond(200, json=_search_payload())
    with count_requests() as tally:
        await configured_omdb.search_list("The Matrix", None, "movie")
    assert tally.count == 1
