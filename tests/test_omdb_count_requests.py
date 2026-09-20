"""ADR 0005 D2 (W0) — `omdb_service.count_requests()` mirrors
`tmdb_service.count_requests()`: independent ContextVar tally, never
touches `real_request_count`/`OMDB_DAILY_LIMIT`."""
from __future__ import annotations

import pytest_asyncio

from app.services.omdb_service import OMDbService, count_requests


@pytest_asyncio.fixture
async def configured_omdb(monkeypatch):
    from app.services import omdb_service as mod

    monkeypatch.setattr(mod.settings, "OMDB_API_KEY", "test_key")
    svc = OMDbService()
    try:
        yield svc
    finally:
        await svc.close()


async def test_counts_real_calls(configured_omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json={"Response": "False", "Error": "Movie not found!"})
    with count_requests() as tally:
        assert tally.count == 0
        await configured_omdb.get_by_imdb_id("tt0000001")
    assert tally.count == 1
    assert configured_omdb.real_request_count >= 1


async def test_zero_outside_context(configured_omdb, omdb_mock):
    from app.services import omdb_service as mod

    omdb_mock.get("/").respond(200, json={"Response": "False"})
    await configured_omdb.get_by_imdb_id("tt0000001")
    assert mod._request_tally.get() is None


async def test_independent_from_tmdb_tally(configured_omdb, omdb_mock):
    """The OMDb tally is a SEPARATE ContextVar from tmdb_service's — a
    `with tmdb_service.count_requests():` block must not see OMDb calls,
    and vice versa."""
    from app.services import tmdb_service as tmdb_mod

    omdb_mock.get("/").respond(200, json={"Response": "False"})
    with tmdb_mod.count_requests() as tmdb_tally:
        with count_requests() as omdb_tally:
            await configured_omdb.get_by_imdb_id("tt0000001")
        assert omdb_tally.count == 1
    assert tmdb_tally.count == 0
