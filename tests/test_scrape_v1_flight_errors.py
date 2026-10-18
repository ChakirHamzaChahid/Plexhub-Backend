"""B1-11 — single-flight, deadline and error translation of the gateway (US7,
FR-032..FR-035, ADR 0009 D2.6/D2.7, CLAUDE.md pitfalls 21c/25c).

The provider keys are the canary `TESTKEY-LEAK-CANARY`: no log line, no
traceback and no response body may ever contain it.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest

from app.config import settings
from app.services import scrape_gateway_cache as cache
from app.services import scrape_gateway_service as gateway
from app.services import scrape_keys
from app.services.scrape_gateway_cache import counters
from app.services.tmdb_service import tmdb_service
from tests._scrape_v1_support import (
    BASE,
    CANARY,
    MASTER_HEADERS,
    OMDB,
    TMDB,
    all_log_text,
    load,
)

_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def _auto(gw_flights_cleanup):
    yield


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await _real_sleep(0.005)


def _short_deadline(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(scrape_keys, "request_timeout_seconds", lambda: seconds)


# --- Single-flight ------------------------------------------------------------------


async def test_twenty_concurrent_requests_make_one_provider_call(gw_client, gw_http, monkeypatch):
    monkeypatch.setattr(settings, "OMDB_API_KEY", "")
    raw = load("movie_603_fr.json")

    async def _slow(request):
        await _until(lambda: counters.coalesced >= 19)
        return httpx.Response(200, json=raw)

    route = gw_http.get(f"{TMDB}/movie/603").mock(side_effect=_slow)
    responses = await asyncio.gather(*(
        gw_client.get(f"{BASE}/movie/603", headers=MASTER_HEADERS) for _ in range(20)))
    assert [r.status_code for r in responses] == [200] * 20
    assert route.call_count == 1
    assert (counters.misses, counters.coalesced) == (1, 19)
    bodies = [r.json() for r in responses]
    assert all(b == bodies[0] for b in bodies) and bodies[0]["cache"] == "miss"


async def test_refresh_is_a_distinct_flight(gw_db, gw_providers, gw_http):
    gate = asyncio.Event()

    async def _slow(request):
        await gate.wait()
        return httpx.Response(200, json=load("movie_603_fr.json"))

    route = gw_http.get(f"{TMDB}/movie/603").mock(side_effect=_slow)
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    plain = asyncio.create_task(gateway.details("movie", 603, None, False))
    fresh = asyncio.create_task(gateway.details("movie", 603, None, True))
    await _until(lambda: len(gateway._in_flight) == 2)
    gate.set()
    await asyncio.gather(plain, fresh)
    assert route.call_count == 2


async def test_cancelled_client_does_not_cancel_the_flight(gw_db, gw_providers, gw_http):
    gate = asyncio.Event()

    async def _slow(request):
        await gate.wait()
        return httpx.Response(200, json=load("season_1396_1_fr.json"))

    gw_http.get(f"{TMDB}/tv/1396/season/1").mock(side_effect=_slow)
    request = asyncio.create_task(gateway.season(1396, 1, None, False))
    await _until(lambda: len(gateway._in_flight) == 1)
    (flight,) = gateway._in_flight.values()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not flight.cancelled()
    gate.set()
    await flight
    entry = await cache.get(scrape_keys.season_key(1396, 1, "fr-FR"), session_factory=gw_db)
    assert entry is not None and entry.result == "found"
    assert gateway._in_flight == {}                     # removed by its done-callback


async def test_done_callback_never_removes_a_newer_flight(gw_db, gw_providers):
    key = ("1|fr-FR|movie|1", False)
    gate = asyncio.Event()

    async def _work():
        await gate.wait()
        return "old"

    old, joined = gateway._join_or_start(key, _work)
    assert not joined
    newer = asyncio.get_running_loop().create_future()
    gateway._in_flight[key] = newer                     # a newer flight took the slot
    gate.set()
    await old
    await _real_sleep(0)
    assert gateway._in_flight[key] is newer
    gateway._in_flight.pop(key)


# --- Deadline and Retry-After (FR-034, X21) ----------------------------------------


async def test_deadline_gives_504_and_the_flight_still_fills_the_cache(
        gw_client, gw_db, monkeypatch):
    """The waiter leaves at its deadline (504); the shielded flight goes on and
    writes its result. The provider fake ignores the deadline on purpose: the
    real `_request_bounded` is bounded by the same deadline (B1-08), so only
    a provider answer landing right at the deadline outlives the waiter."""
    _short_deadline(monkeypatch, 0.2)
    gate = asyncio.Event()

    async def _slow(show_id, season, *, language, deadline):
        await gate.wait()
        return load("season_1396_1_fr.json")

    monkeypatch.setattr(tmdb_service, "fetch_season", _slow)
    started = time.monotonic()
    response = await gw_client.get(f"{BASE}/tv/1396/season/1", headers=MASTER_HEADERS)
    assert time.monotonic() - started < 3
    assert response.status_code == 504
    assert response.json() == {"detail": {"code": "upstream_timeout",
                                          "message": "The provider did not answer in time"}}
    assert counters.upstream_errors == 1
    (flight,) = gateway._in_flight.values()
    gate.set()
    await flight
    assert await cache.get(scrape_keys.season_key(1396, 1, "fr-FR"), session_factory=gw_db)
    hit = await gw_client.get(f"{BASE}/tv/1396/season/1", headers=MASTER_HEADERS)
    assert hit.json()["cache"] == "hit"


@pytest.mark.parametrize("header, expected", [("120", 120), ("100000", 300)])
async def test_long_retry_after_is_an_immediate_429(gw_client, gw_http, header, expected):
    gw_http.get(f"{TMDB}/movie/603").respond(429, headers={"Retry-After": header})
    started = time.monotonic()
    response = await gw_client.get(f"{BASE}/movie/603", headers=MASTER_HEADERS)
    assert time.monotonic() - started < 2
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(expected)
    detail = response.json()["detail"]
    assert detail["code"] == "rate_limited" and detail["retryAfterSeconds"] == expected


async def test_tmdb_429_after_its_retries(gw_client, gw_http, gw_no_sleep):
    route = gw_http.get(f"{TMDB}/movie/603").respond(429, headers={"Retry-After": "1"})
    response = await gw_client.get(f"{BASE}/movie/603", headers=MASTER_HEADERS)
    assert response.status_code == 429 and route.call_count == 4
    assert response.json()["detail"]["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


# --- Error translation (contract 4.6) ------------------------------------------------


@pytest.mark.parametrize("mock, status, code", [
    ({"status_code": 500}, 502, "upstream_error"),
    ({"status_code": 418}, 502, "upstream_error"),
    ({"status_code": 200, "content": b"<html>"}, 502, "upstream_error"),
    ({"status_code": 200, "json": {"no": "id"}}, 502, "upstream_error"),       # mapping error
    ({"status_code": 401}, 503, "provider_not_configured"),                   # X14
    ({"status_code": 503}, 503, "upstream_unavailable"),
])
async def test_provider_failures_are_translated(gw_client, gw_http, gw_no_sleep, mock, status, code):
    gw_http.get(f"{TMDB}/movie/603").respond(**mock)
    response = await gw_client.get(f"{BASE}/movie/603", headers=MASTER_HEADERS)
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert CANARY not in response.text and "themoviedb" not in response.text
    if code == "upstream_unavailable":
        assert response.headers["Retry-After"] == str(detail["retryAfterSeconds"])


async def test_unreachable_tmdb_is_503(gw_client, gw_http, gw_no_sleep):
    gw_http.get(f"{TMDB}/movie/603").mock(side_effect=httpx.ConnectError("boom"))
    response = await gw_client.get(f"{BASE}/movie/603", headers=MASTER_HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "upstream_unavailable"
    assert counters.upstream_errors == 1


@pytest.mark.parametrize("path", [
    "/match?type=movie&title=matrix", "/search?type=movie&query=matrix", "/movie/603",
    "/tv/1396", "/tv/1396/season/1", "/find?imdbId=tt0133093",
])
async def test_missing_tmdb_key_is_provider_not_configured(gw_client, gw_http, monkeypatch, path):
    monkeypatch.setattr(settings, "TMDB_API_KEY", "")
    route = gw_http.route().respond(200, json={})
    response = await gw_client.get(f"{BASE}{path}", headers=MASTER_HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_not_configured"
    assert route.call_count == 0


async def test_search_errors_are_translated_too(gw_client, gw_http, gw_no_sleep):
    gw_http.get(f"{TMDB}/search/movie").respond(500)
    response = await gw_client.get(f"{BASE}/search", params={"type": "movie", "query": "x"},
                                   headers=MASTER_HEADERS)
    assert response.status_code == 502 and response.json()["detail"]["code"] == "upstream_error"


# --- No key leak, even from an orphan flight (D2.7) ----------------------------------


async def test_orphan_failing_flight_leaks_no_key(gw_db, gw_providers, gw_http, gw_logs):
    """Every waiter is gone (cancelled client) when the provider fails: the
    flight does not raise (it returns its error), so `create_background_task`
    logs nothing at ERROR — and neither the outcome nor any log line carries
    the key or the URL."""
    gate = asyncio.Event()

    async def _slow_failure(request):
        assert CANARY in str(request.url)               # the key really is in the URL
        await gate.wait()
        return httpx.Response(500, text=f"echo {request.url}")

    gw_http.get(f"{TMDB}/movie/603").mock(side_effect=_slow_failure)
    request = asyncio.create_task(gateway.details("movie", 603, None, False))
    await _until(lambda: len(gateway._in_flight) == 1)
    (flight,) = gateway._in_flight.values()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    gate.set()
    outcome = await flight                              # the flight does not raise
    await _real_sleep(0.01)                             # let the done-callbacks run
    text = all_log_text(gw_logs)
    assert isinstance(outcome.error, gateway.GatewayError) and outcome.error.status == 502
    assert outcome.error.__context__ is None and outcome.error.__cause__ is None
    assert flight.exception() is None
    assert "Background task failed" not in text and "Traceback" not in text
    assert [r for r in gw_logs.records if r.levelno >= logging.ERROR] == []
    assert CANARY not in text and "api_key" not in text


async def test_error_bodies_never_carry_the_key(gw_client, gw_http, gw_logs):
    gw_http.get(f"{TMDB}/movie/603").mock(
        side_effect=lambda request: httpx.Response(500, text=f"echo {request.url}"))
    response = await gw_client.get(f"{BASE}/movie/603", headers=MASTER_HEADERS)
    await _real_sleep(0.01)
    assert response.status_code == 502
    assert CANARY not in response.text and CANARY not in all_log_text(gw_logs)


async def test_raw_exception_with_url_inside_a_flight_leaks_nothing(
        gw_client, gw_logs, monkeypatch):
    async def _boom(*args, **kwargs):
        raise httpx.HTTPStatusError(
            f"Server error for url https://api.themoviedb.org/3/movie/603?api_key={CANARY}",
            request=httpx.Request("GET", f"https://api.themoviedb.org/3/movie/603?api_key={CANARY}"),
            response=httpx.Response(500),
        )

    monkeypatch.setattr(tmdb_service, "fetch_movie", _boom)
    response = await gw_client.get(f"{BASE}/movie/603", headers=MASTER_HEADERS)
    await _real_sleep(0.01)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "upstream_error"
    text = all_log_text(gw_logs)
    assert CANARY not in text and CANARY not in response.text
    assert "HTTPStatusError" in text                    # the type is logged, not the text


async def test_master_key_never_appears_in_logs_or_bodies(gw_client, gw_http, gw_logs):
    gw_http.get(f"{TMDB}/movie/603").respond(500)
    response = await gw_client.get(f"{BASE}/movie/603", headers=MASTER_HEADERS)
    await _real_sleep(0.01)
    assert MASTER_HEADERS["X-API-Key"] not in all_log_text(gw_logs)
    assert MASTER_HEADERS["X-API-Key"] not in response.text
