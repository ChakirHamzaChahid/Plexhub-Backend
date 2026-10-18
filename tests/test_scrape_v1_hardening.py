"""B1-11 post-review fixes: language allow-list (security M1), request body size
limit (security M2), `expiresAt` never in the past (code review M1), no ERROR
log for an expected flight failure (code review M2 / security L2), length of
the keys to invalidate (L4), 4 MiB response guard (L5).

PRD X31..X33, ADR 0009 D2.2/D2.7/D2.8/D2.10/D2.12.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from app import config as config_module
from app.api.body_limit import MAX_BODY_BYTES, ScrapeBodyLimitMiddleware
from app.config import settings
from app.services import scrape_gateway_cache as cache
from app.services import scrape_gateway_service as gateway
from app.services import scrape_keys
from app.services import scrape_mappers as mappers
from app.utils.time import now_ms
from tests._scrape_v1_support import (
    BASE,
    CANARY,
    MASTER_HEADERS,
    OMDB,
    TMDB,
    all_log_text,
    load,
    search_hit,
)

DAY = 86_400_000
TOO_LARGE = {"detail": {"code": "invalid_parameter", "message": "Request body too large"}}


@pytest.fixture(autouse=True)
def _auto(gw_flights_cleanup):
    yield


def _get(client, path, **params):
    return client.get(f"{BASE}{path}", params=params, headers=MASTER_HEADERS)


# --- 1. Language allow-list (SCRAPE_ALLOWED_LANGUAGES) ------------------------------


@pytest.mark.parametrize("raw, valid, invalid", [
    ("fr-FR,en-US", ("fr-FR", "en-US"), ()),
    (" fr-FR , de-DE ,", ("fr-FR", "de-DE"), ()),
    ("fr-FR,fr-FR,en-US", ("fr-FR", "en-US"), ()),
    ("fr-FR,fr,EN-us,xx_XX,de-DE\n", ("fr-FR",), ("fr", "EN-us", "xx_XX", "de-DE\n".strip())),
    ("", (), ()),
    (None, (), ()),
])
def test_parse_language_list(raw, valid, invalid):
    got_valid, got_invalid = config_module.parse_language_list(raw)
    if raw == "fr-FR,fr,EN-us,xx_XX,de-DE\n":
        # "de-DE\n" is stripped to a valid tag: the newline is whitespace.
        assert got_valid == ("fr-FR", "de-DE") and got_invalid == ("fr", "EN-us", "xx_XX")
    else:
        assert (got_valid, got_invalid) == (valid, invalid)


def test_allowed_languages_default_and_override(monkeypatch):
    assert scrape_keys.allowed_languages() == ("fr-FR", "en-US")
    monkeypatch.setattr(settings, "SCRAPE_ALLOWED_LANGUAGES", "de-DE, es-ES")
    assert scrape_keys.allowed_languages() == ("de-DE", "es-ES")


@pytest.mark.parametrize("raw", ["", "  ,, ", "fr,EN-us,xx_XX", None])
def test_empty_or_all_invalid_list_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setattr(settings, "SCRAPE_ALLOWED_LANGUAGES", raw)
    assert scrape_keys.allowed_languages() == ("fr-FR", "en-US")


def test_invalid_elements_are_warned_about_at_boot(monkeypatch, tmp_path, gw_logs):
    monkeypatch.setattr(config_module.Settings, "DATA_DIR", tmp_path / "d")
    monkeypatch.setattr(config_module.Settings, "LOG_DIR", tmp_path / "l")
    monkeypatch.setattr(config_module.Settings, "SCRAPE_ALLOWED_LANGUAGES", "fr-FR,bogus,EN-us")
    config_module.Settings()
    warnings = [r for r in gw_logs.records
                if r.levelno == logging.WARNING and "SCRAPE_ALLOWED_LANGUAGES" in r.getMessage()]
    assert len({id(r) for r in warnings}) == 1
    message = warnings[0].getMessage()
    assert "'bogus'" in message and "'EN-us'" in message and "fr-FR" not in message.split("(expected")[0]


@pytest.mark.parametrize("path, params", [
    ("/match", {"type": "movie", "title": "matrix", "language": "de-DE"}),
    ("/search", {"type": "movie", "query": "matrix", "language": "de-DE"}),
    ("/movie/603", {"language": "de-DE"}),
    ("/tv/1396", {"language": "de-DE"}),
    ("/tv/1396/season/1", {"language": "de-DE"}),
])
async def test_language_outside_the_list_is_a_400_and_never_reaches_a_provider(
        gw_client, gw_http, gw_db, path, params):
    route = gw_http.route().respond(200, json={})
    response = await _get(gw_client, path, **params)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_parameter"
    assert route.call_count == 0
    assert (await cache.stats(session_factory=gw_db)) == []          # no row created


async def test_listed_language_is_accepted(gw_client, gw_http, monkeypatch):
    monkeypatch.setattr(settings, "SCRAPE_ALLOWED_LANGUAGES", "de-DE")
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    response = await _get(gw_client, "/movie/603", language="de-DE")
    assert response.status_code == 200 and response.json()["language"] == "de-DE"
    # en-US is no longer listed (the override REPLACES the default list).
    assert (await _get(gw_client, "/movie/603", language="en-US")).status_code == 400


async def test_tmdb_language_is_always_accepted_even_when_not_listed(
        gw_client, gw_http, monkeypatch):
    monkeypatch.setattr(settings, "TMDB_LANGUAGE", "it-IT")
    assert "it-IT" not in scrape_keys.allowed_languages()
    movie = gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    absent = await _get(gw_client, "/movie/603")                      # default language
    explicit = await _get(gw_client, "/movie/603", language="it-IT")
    assert absent.status_code == 200 and explicit.status_code == 200
    assert absent.json()["language"] == "it-IT"
    assert movie.calls.last.request.url.params["language"] == "it-IT"


# --- 2. Body size limit on /api/scrape/v1/ only ---------------------------------------


def _scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "method": "POST", "path": path, "headers": headers}


async def _drive(middleware, scope, chunks: list[bytes]):
    """Run `middleware` on `scope`; returns (sent messages, chunks consumed)."""
    sent: list[dict] = []
    consumed = 0
    queue = list(chunks)

    async def receive():
        nonlocal consumed
        if not queue:
            return {"type": "http.disconnect"}
        consumed += 1
        body = queue.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return sent, consumed


def _status(sent) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def test_content_length_over_the_limit_is_refused_without_reading_the_body():
    inner_called = False

    async def inner(scope, receive, send):
        nonlocal inner_called
        inner_called = True

    middleware = ScrapeBodyLimitMiddleware(inner)
    headers = [(b"content-length", str(MAX_BODY_BYTES + 1).encode())]
    sent, consumed = await _drive(middleware, _scope(f"{BASE}/cache/purge", headers), [b"x" * 10])
    assert not inner_called and consumed == 0
    assert _status(sent) == 413
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    import json
    assert json.loads(body) == TOO_LARGE


async def test_pass_through_for_other_paths_and_non_http_scopes():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    middleware = ScrapeBodyLimitMiddleware(inner)
    huge = [(b"content-length", b"9999999")]
    await _drive(middleware, _scope("/api/accounts", huge), [b""])
    await _drive(middleware, _scope("/api/scrape/v1", huge), [b""])        # no trailing slash
    await middleware({"type": "lifespan"}, None, None)
    assert seen == ["http", "http", "lifespan"]


async def test_chunked_body_is_counted_and_cut_over_the_limit():
    async def inner(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = ScrapeBodyLimitMiddleware(inner)
    chunks = [b"x" * 10_000] * 10                       # 100 000 bytes, no Content-Length
    sent, consumed = await _drive(middleware, _scope(f"{BASE}/cache/purge", []), chunks)
    assert _status(sent) == 413
    assert consumed < len(chunks)                       # cut on the fly, not drained
    # The inner app's own answer is never forwarded after the cut.
    assert [m["type"] for m in sent].count("http.response.start") == 1


async def test_a_swallowing_inner_app_still_gets_a_413():
    """FastAPI turns an exception raised by `receive` into a 422: whatever the
    inner app answers once the limit was crossed, the client sees 413."""
    async def inner(scope, receive, send):
        try:
            while True:
                if not (await receive()).get("more_body"):
                    break
        except Exception:                                # noqa: BLE001
            await send({"type": "http.response.start", "status": 422, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

    middleware = ScrapeBodyLimitMiddleware(inner)
    sent, _ = await _drive(middleware, _scope(f"{BASE}/cache/purge", []), [b"x" * 40_000] * 3)
    assert _status(sent) == 413


async def test_a_body_exactly_at_the_limit_goes_through():
    async def inner(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ScrapeBodyLimitMiddleware(inner)
    headers = [(b"content-length", str(MAX_BODY_BYTES).encode())]
    chunks = [b"x" * (MAX_BODY_BYTES // 2)] * 2
    sent, _ = await _drive(middleware, _scope(f"{BASE}/cache/purge", headers), chunks)
    assert _status(sent) == 204
    sent, _ = await _drive(middleware, _scope(f"{BASE}/cache/purge", []), chunks)
    assert _status(sent) == 204


async def test_end_to_end_content_length(gw_client):
    big = b'{"keys": ["' + b"x" * (MAX_BODY_BYTES + 10) + b'"]}'
    response = await gw_client.post(f"{BASE}/cache/invalidate", content=big,
                                    headers={**MASTER_HEADERS, "Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json() == TOO_LARGE


async def test_end_to_end_chunked(gw_client):
    sent_chunks = 0

    async def body():
        nonlocal sent_chunks
        for _ in range(30):
            sent_chunks += 1
            yield b" " * 10_000

    response = await gw_client.post(
        f"{BASE}/cache/purge", content=body(),
        headers={**MASTER_HEADERS, "Content-Type": "application/json"})
    assert response.status_code == 413 and response.json() == TOO_LARGE
    assert sent_chunks < 30


async def test_a_normal_post_is_not_affected(gw_client):
    response = await gw_client.post(f"{BASE}/cache/purge", json={"limit": 10},
                                    headers=MASTER_HEADERS)
    assert response.status_code == 200


async def test_other_routes_are_not_limited(gw_client):
    big = b"x" * (MAX_BODY_BYTES * 2)
    response = await gw_client.post("/api/accounts", content=big,
                                    headers={"Content-Type": "application/json"})
    assert response.status_code != 413                  # 401: the guard answers, not the limit


def test_existing_middlewares_keep_their_relative_order():
    from app.api.csrf import AdminCsrfMiddleware
    from app.main import app
    from app.utils.request_context import RequestIdMiddleware

    classes = [m.cls for m in app.user_middleware]
    order = [c for c in classes if c in (
        AdminCsrfMiddleware, RequestIdMiddleware, ScrapeBodyLimitMiddleware)]
    assert order == [AdminCsrfMiddleware, RequestIdMiddleware, ScrapeBodyLimitMiddleware]
    names = [c.__name__ for c in classes]
    assert names.index("RequestIdMiddleware") < names.index("GZipMiddleware") < names.index("CORSMiddleware")


# --- 3. `expiresAt` is never in the past ----------------------------------------------


async def _put_old_movie(gw_db, age_days: int = 10) -> int:
    fetched_at = now_ms() - age_days * DAY
    details = mappers.map_movie(load("movie_603_fr.json"), language="fr-FR")
    await cache.put(scrape_keys.movie_key(603, "fr-FR"), "found", details, fetched_at,
                    session_factory=gw_db)
    return fetched_at


async def test_expires_at_is_floored_when_omdb_failed_on_an_old_hit(gw_client, gw_http, gw_db):
    fetched_at = await _put_old_movie(gw_db)
    gw_http.get(OMDB).respond(500)
    before = now_ms()
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["cache"] == "hit" and body["details"]["omdbStatus"] == "error"
    assert body["fetchedAt"] == fetched_at
    # fetchedAt + negative TTL (3 d) is 7 days ago: the response must not say so.
    assert body["expiresAt"] >= before
    assert body["expiresAt"] <= now_ms()


async def test_expires_at_is_floored_for_a_budget_exhausted_old_hit(
        gw_client, gw_http, gw_db, monkeypatch):
    await _put_old_movie(gw_db)
    monkeypatch.setattr(settings, "OMDB_DAILY_LIMIT", 0)
    before = now_ms()
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["details"]["omdbStatus"] == "budget_exhausted"
    assert body["expiresAt"] >= before


async def test_match_expires_at_is_floored_too(gw_client, gw_http, gw_db):
    fetched_at = await _put_old_movie(gw_db)
    key = scrape_keys.match_key("movie", "matrix", 1999, "fr-FR")
    await cache.put(key, "matched", {"confidence": 0.95, "tmdbId": 603}, fetched_at,
                    session_factory=gw_db)
    gw_http.get(OMDB).respond(500)
    before = now_ms()
    response = await _get(gw_client, "/match", type="movie", title="matrix", year=1999)
    body = response.json()
    assert body["cache"] == "hit" and body["result"] == "matched", response.text
    assert body["details"]["omdbStatus"] == "error"
    assert body["expiresAt"] >= before


async def test_a_fresh_entry_keeps_its_exact_expiry(gw_client, gw_http):
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["expiresAt"] - body["fetchedAt"] == 30 * DAY


# --- 4. An expected flight failure is not an ERROR log ---------------------------------


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("mock, status", [
    ({"status_code": 429, "headers": {"Retry-After": "7"}}, 429),
    ({"status_code": 503}, 503),
    ({"status_code": 500}, 502),
    ({"status_code": 401}, 503),
])
async def test_expected_flight_failures_log_no_error(
        gw_client, gw_http, gw_logs, gw_no_sleep, mock, status):
    gw_http.get(f"{TMDB}/movie/603").respond(**mock)
    response = await _get(gw_client, "/movie/603")
    await asyncio.sleep(0.01)                           # let the done-callbacks run
    assert response.status_code == status
    assert _errors(gw_logs) == []
    assert "Traceback" not in all_log_text(gw_logs) and CANARY not in all_log_text(gw_logs)


async def test_a_negative_result_logs_no_error_either(gw_client, gw_http, gw_logs):
    gw_http.get(f"{TMDB}/movie/999999").respond(404, json={"status_code": 34})
    assert (await _get(gw_client, "/movie/999999")).json()["result"] == "not_found"
    await asyncio.sleep(0.01)
    assert _errors(gw_logs) == []


async def test_a_deadline_timeout_logs_no_error(gw_client, gw_logs, monkeypatch):
    monkeypatch.setattr(scrape_keys, "request_timeout_seconds", lambda: 0.05)
    gate = asyncio.Event()

    async def _slow(show_id, season, *, language, deadline):
        await gate.wait()
        return load("season_1396_1_fr.json")

    from app.services.tmdb_service import tmdb_service
    monkeypatch.setattr(tmdb_service, "fetch_season", _slow)
    response = await gw_client.get(f"{BASE}/tv/1396/season/1", headers=MASTER_HEADERS)
    assert response.status_code == 504
    gate.set()
    await asyncio.gather(*gateway._in_flight.values())
    await asyncio.sleep(0.01)
    assert _errors(gw_logs) == []


async def test_each_waiter_raises_its_own_context_free_exception(gw_db, gw_providers, gw_http):
    gate = asyncio.Event()

    async def _failing(request):
        await gate.wait()
        return httpx.Response(503)

    gw_http.get(f"{TMDB}/movie/603").mock(side_effect=_failing)
    waiters = [asyncio.create_task(gateway.details("movie", 603, None, False)) for _ in range(3)]
    for _ in range(200):
        if gateway.counters.coalesced >= 2:
            break
        await asyncio.sleep(0.005)
    gate.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(r, gateway.GatewayError) and r.status == 503 for r in results)
    assert len({id(r) for r in results}) == 3                            # one object per waiter
    for err in results:
        assert err.__cause__ is None and err.__context__ is None


async def test_the_flight_returns_its_error_instead_of_raising(gw_db, gw_providers, gw_http):
    # Le fournisseur ne répond qu'une fois le vol observé : sans ce verrou, un
    # runner rapide termine le vol (500 non repris) avant que le test le voie.
    release = asyncio.Event()

    async def _boom(request):
        await release.wait()
        return httpx.Response(500)

    gw_http.get(f"{TMDB}/movie/603").mock(side_effect=_boom)
    task = asyncio.create_task(gateway.details("movie", 603, None, False))
    for _ in range(400):
        if gateway._in_flight:
            break
        await asyncio.sleep(0.005)
    (flight,) = gateway._in_flight.values()
    release.set()
    outcome = await flight                                # does not raise
    assert flight.exception() is None
    assert isinstance(outcome.error, gateway.GatewayError) and outcome.error.status == 502
    assert outcome.error.__cause__ is None and outcome.error.__context__ is None
    with pytest.raises(gateway.GatewayError):
        await task


async def test_an_unexpected_exception_is_still_logged_with_its_type_only(
        gw_client, gw_logs, monkeypatch):
    from app.services.tmdb_service import tmdb_service

    async def _bug(*args, **kwargs):
        raise KeyError(f"secret-{CANARY}")

    monkeypatch.setattr(tmdb_service, "fetch_movie", _bug)
    response = await _get(gw_client, "/movie/603")
    await asyncio.sleep(0.01)
    assert response.status_code == 502
    text = all_log_text(gw_logs)
    assert "KeyError" in text and CANARY not in text and "Traceback" not in text


# --- 5. Length of the keys to invalidate -----------------------------------------------


async def test_invalidate_key_length_is_bounded(gw_client):
    ok_length = await gw_client.post(f"{BASE}/cache/invalidate", json={"keys": ["x" * 1024]},
                                     headers=MASTER_HEADERS)
    assert ok_length.status_code == 400                   # well-sized but malformed key
    assert ok_length.json()["detail"]["code"] == "invalid_parameter"
    too_long = await gw_client.post(f"{BASE}/cache/invalidate", json={"keys": ["x" * 1025]},
                                    headers=MASTER_HEADERS)
    assert too_long.status_code == 422


# --- 6. 4 MiB guard ----------------------------------------------------------------------


def test_the_response_ceiling_is_four_mebibytes():
    assert gateway.MAX_RESPONSE_BYTES == 4 * 1024 * 1024


@pytest.mark.parametrize("path, mock_path, fixture, family_key", [
    ("/movie/603", f"{TMDB}/movie/603", "movie_603_fr.json", scrape_keys.movie_key(603, "fr-FR")),
    ("/tv/1396", f"{TMDB}/tv/1396", "tv_1396_fr.json", scrape_keys.show_key(1396, "fr-FR")),
    ("/tv/1396/season/1", f"{TMDB}/tv/1396/season/1", "season_1396_1_fr.json",
     scrape_keys.season_key(1396, 1, "fr-FR")),
])
async def test_an_oversized_response_is_a_502_and_nothing_is_cached(
        gw_client, gw_http, gw_db, monkeypatch, path, mock_path, fixture, family_key):
    gw_http.get(mock_path).respond(200, json=load(fixture))
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 1_000)
    response = await _get(gw_client, path)
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "upstream_error"
    assert await cache.get(family_key, session_factory=gw_db) is None
    assert (await cache.stats(session_factory=gw_db)) == []


async def test_an_oversized_match_details_caches_neither_entry(
        gw_client, gw_http, gw_db, monkeypatch):
    gw_http.get(f"{TMDB}/search/movie").respond(200, json={"results": [search_hit(603, "Matrix")]})
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 1_000)
    response = await _get(gw_client, "/match", type="movie", title="matrix", year=1999)
    assert response.status_code == 502
    assert (await cache.stats(session_factory=gw_db)) == []
