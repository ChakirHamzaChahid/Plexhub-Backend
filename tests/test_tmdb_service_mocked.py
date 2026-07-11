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


# ─── CR-F03: real-call budget accounting (not logical-item accounting) ──────


async def test_request_count_tracks_every_retry_not_one_per_call(
    configured_tmdb, tmdb_mock, monkeypatch,
):
    """A single logical `_request()` that needs 3 real HTTP attempts before
    succeeding (429, 503, then 200) must bump the real-call counter by 3, not
    by 1 — that decoupling (counting logical items/calls instead of real HTTP
    spend, including retries) is exactly CR-F03. Pre-fix, `_request` had no
    counter at all / callers counted 1 logical call regardless of retries."""
    from app.services import tmdb_service as mod

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    import httpx
    route = tmdb_mock.get("/3/search/movie")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(503),
        httpx.Response(200, json={"results": [_movie(7, "Bar", "2010-05-05")]}),
    ]
    assert configured_tmdb.get_request_count() == 0

    outcome = await configured_tmdb.search_movie("Bar", 2010)

    assert outcome.result == "matched"
    assert route.call_count == 3
    # The real-call counter must equal the actual HTTP attempts made (3),
    # not 1 (one logical `_request()`/search call).
    assert configured_tmdb.get_request_count() == 3


async def test_reset_request_count(configured_tmdb, tmdb_mock, monkeypatch):
    tmdb_mock.get("/3/search/movie").respond(
        200, json={"results": [_movie(1, "Foo", "2020-01-01")]},
    )
    await configured_tmdb.search_movie("Foo", 2020)
    assert configured_tmdb.get_request_count() > 0

    configured_tmdb.reset_request_count()
    assert configured_tmdb.get_request_count() == 0


async def test_enrichment_halts_on_real_call_budget_not_item_count(monkeypatch):
    """Reproduces CR-F03 end-to-end at the worker level: an item whose
    `_request` retries 3 times before matching must consume 3 units of the
    enrichment daily budget, not 1 — so a tiny real-call budget (3) must halt
    the worker after a single item even though 2 queue items are pending."""
    from app.workers import enrichment_worker as ew
    from app.config import settings
    from app.models.database import EnrichmentQueue
    from app.services.tmdb_service import TMDBService

    monkeypatch.setattr(settings, "ENRICHMENT_DAILY_LIMIT", 3)
    # One item per batch so the between-batch budget check actually takes
    # effect after item 1 exhausts the (tiny, test-only) budget — mirrors
    # real operation where BATCH_SIZE=200 is far larger than a typical queue.
    monkeypatch.setattr(ew, "BATCH_SIZE", 1)
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test_key")

    svc = TMDBService()

    async def fake_search_movie(title, year, *, summary=None, language=None):
        # Simulate `_request` burning 3 real HTTP attempts (retries) before
        # the FIRST item's search resolves — exhausting the tiny budget.
        svc.real_request_count += 3
        from app.services.tmdb_service import TMDBMatch, TMDBSearchOutcome
        return TMDBSearchOutcome(
            "matched",
            match=TMDBMatch(1, title, year, 0.97, title_score=1.0),
            best=TMDBMatch(1, title, year, 0.97),
        )

    async def fake_get_movie_details(tmdb_id):
        from app.services.tmdb_service import TMDBEnrichmentData
        return TMDBEnrichmentData(
            tmdb_id=tmdb_id, imdb_id=None, overview=None, poster_url=None,
            backdrop_url=None, vote_average=None, genres=None, year=None, cast=None,
        )

    monkeypatch.setattr(svc, "search_movie", fake_search_movie)
    monkeypatch.setattr(svc, "get_movie_details", fake_get_movie_details)
    monkeypatch.setattr(ew, "tmdb_service", svc)

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy import text, select
    from app.models.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ew, "async_session_factory", factory)

    async with factory() as s:
        s.add_all([
            EnrichmentQueue(
                rating_key="vod_1.mp4", server_id="xtream_a", media_type="movie",
                title="Foo", year=2020, status="pending", attempts=0, created_at=0,
            ),
            EnrichmentQueue(
                rating_key="vod_2.mp4", server_id="xtream_a", media_type="movie",
                title="Bar", year=2021, status="pending", attempts=1, created_at=1,
            ),
        ])
        await s.commit()

    await ew.run()

    async with factory() as s:
        rows = (await s.execute(select(EnrichmentQueue).order_by(EnrichmentQueue.created_at))).scalars().all()
        statuses = {r.rating_key: r.status for r in rows}

    # Budget (3 real calls) is exhausted entirely by the first item — the
    # worker must not even attempt the second (would blow the "real calls"
    # budget by another 3x if it counted items instead of real HTTP spend).
    assert statuses["vod_1.mp4"] == "done"
    assert statuses["vod_2.mp4"] == "pending"
    assert svc.get_request_count() == 3

    await engine.dispose()
