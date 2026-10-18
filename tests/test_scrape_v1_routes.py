"""B1-11 — routes of the gateway: per-route guards (structural test), auth
matrix, per-key rate limit, 400 vs 422, metric, OpenAPI, boot audit
(PRD FR-004, FR-015, FR-015b, FR-033, FR-036; ADR 0008 D2.4, ADR 0009 D2.8)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from prometheus_client import REGISTRY
from starlette.requests import Request

from app.api import scrape_v1
from app.api.deps import verify_scrape_admin_scope, verify_scrape_scope
from app.api.route_audit import _dependency_calls, assert_api_routes_authenticated
from app.config import settings
from app.utils.metrics import SCRAPE_GATEWAY_OUTCOMES, SCRAPE_GATEWAY_ROUTES
from tests._scrape_v1_support import BASE, MASTER, MASTER_HEADERS, TMDB, load, make_key

READ_ROUTES = {
    ("GET", f"{BASE}/match"): "match",
    ("GET", f"{BASE}/search"): "search",
    ("GET", f"{BASE}/movie/{{tmdbId}}"): "movie",
    ("GET", f"{BASE}/tv/{{tmdbId}}"): "tv",
    ("GET", f"{BASE}/tv/{{tmdbId}}/season/{{seasonNumber}}"): "season",
    ("GET", f"{BASE}/find"): "find",
    ("GET", f"{BASE}/cache/stats"): "cache_stats",
}
ADMIN_ROUTES = {
    ("POST", f"{BASE}/cache/invalidate"): "cache_invalidate",
    ("POST", f"{BASE}/cache/purge"): "cache_purge",
}


@pytest.fixture(autouse=True)
def _auto(gw_flights_cleanup):
    yield


def _gateway_routes() -> dict[tuple[str, str], APIRoute]:
    from app.main import app

    out = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(BASE):
            for method in route.methods:
                out[(method, route.path)] = route
    return out


# --- Structure (FR-015, ADR 0008 D2.4) -------------------------------------------------


def test_the_router_has_no_router_level_dependency():
    assert scrape_v1.router.dependencies == []
    assert scrape_v1.router.prefix == "/api/scrape/v1"


def test_exactly_nine_routes_with_the_expected_guard_each():
    routes = _gateway_routes()
    assert set(routes) == set(READ_ROUTES) | set(ADMIN_ROUTES)
    for key, route in routes.items():
        direct = [d.call for d in route.dependant.dependencies]
        expected = verify_scrape_admin_scope if key in ADMIN_ROUTES else verify_scrape_scope
        # The guard FIRST, then the limiter (it reads request.state.principal).
        assert direct[:2] == [expected, scrape_v1.enforce_scrape_rate_limit], key
        calls = _dependency_calls(route)
        other = verify_scrape_scope if expected is verify_scrape_admin_scope else verify_scrape_admin_scope
        assert other not in calls, f"{key}: both scope guards stacked"


def test_route_audit_accepts_the_gateway():
    from app.main import app

    assert_api_routes_authenticated(app)


def test_metric_route_names_match_the_routes():
    assert set(SCRAPE_GATEWAY_ROUTES) == set(READ_ROUTES.values()) | set(ADMIN_ROUTES.values())


# --- Auth matrix (US1-AC3/AC4/AC6, FR-004) ----------------------------------------------

# Requests that never reach a provider: data routes with an invalid language
# (400 = the guard let it through), admin routes with a harmless body.
PROBES = {
    "match": ("GET", f"{BASE}/match?type=movie&title=x&language=bad", None),
    "search": ("GET", f"{BASE}/search?type=movie&query=x&language=bad", None),
    "movie": ("GET", f"{BASE}/movie/603?language=bad", None),
    "tv": ("GET", f"{BASE}/tv/1396?language=bad", None),
    "season": ("GET", f"{BASE}/tv/1396/season/1?language=bad", None),
    "find": ("GET", f"{BASE}/find", None),
    "cache_stats": ("GET", f"{BASE}/cache/stats", None),
    "cache_invalidate": ("POST", f"{BASE}/cache/invalidate", {"keys": ["1|fr-FR|movie|603"]}),
    "cache_purge": ("POST", f"{BASE}/cache/purge", {"limit": 1}),
}
ADMIN_NAMES = set(ADMIN_ROUTES.values())


async def _call(client, name, key):
    method, url, body = PROBES[name]
    headers = {} if key is None else {"X-API-Key": key}
    return await client.request(method, url, json=body, headers=headers)


@pytest.mark.parametrize("who", ["master", "scrape", "admin", "both", "none", "unknown",
                                 "revoked", "expired", "bogus", "missing"])
async def test_auth_matrix(gw_client, gw_db, who):
    key = {
        "master": MASTER,
        "scrape": await make_key(gw_db, scopes="scrape"),
        "admin": await make_key(gw_db, scopes="scrape.admin"),
        "both": await make_key(gw_db, scopes="scrape scrape.admin"),
        "none": await make_key(gw_db),
        "unknown": await make_key(gw_db, scopes="zz-unknown"),
        "revoked": await make_key(gw_db, scopes="scrape scrape.admin", revoked=True),
        "expired": await make_key(gw_db, scopes="scrape scrape.admin", expired=True),
        "bogus": "phk_not-a-real-key",
        "missing": None,
    }[who]
    for name in PROBES:
        response = await _call(gw_client, name, key)
        admin = name in ADMIN_NAMES
        allowed = who in ("master", "both") or (who == "scrape" and not admin) or (
            who == "admin" and admin)
        if who in ("revoked", "expired", "bogus", "missing"):
            assert response.status_code == 401, (who, name)
            assert response.json()["detail"]["code"] == "invalid_api_key"
        elif allowed:
            assert response.status_code in (200, 400), (who, name, response.text)
        else:
            assert response.status_code == 403, (who, name)
            assert response.json()["detail"]["code"] == "insufficient_scope"


async def test_scrape_key_is_refused_on_existing_routes(gw_client, gw_db):
    key = await make_key(gw_db, scopes="scrape")
    response = await gw_client.get("/api/media/movies", headers={"X-API-Key": key})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"


# --- Per-key rate limit (FR-033, D2.8) ----------------------------------------------------


async def test_limit_of_one_gives_429_on_the_second_request(gw_client, gw_db, monkeypatch):
    monkeypatch.setattr(settings, "SCRAPE_RATE_LIMIT_PER_MINUTE", 1)
    key = await make_key(gw_db, scopes="scrape")
    other = await make_key(gw_db, scopes="scrape")
    first = await gw_client.get(f"{BASE}/cache/stats", headers={"X-API-Key": key})
    assert first.status_code == 200
    second = await gw_client.get(f"{BASE}/cache/stats", headers={"X-API-Key": key})
    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail["code"] == "rate_limited"
    assert 1 <= detail["retryAfterSeconds"] <= 60
    assert second.headers["Retry-After"] == str(detail["retryAfterSeconds"])
    # Another key has its own bucket; the master key too.
    assert (await gw_client.get(f"{BASE}/cache/stats", headers={"X-API-Key": other})).status_code == 200
    assert (await gw_client.get(f"{BASE}/cache/stats", headers=MASTER_HEADERS)).status_code == 200
    assert (await gw_client.get(f"{BASE}/cache/stats", headers=MASTER_HEADERS)).status_code == 429


async def test_limiter_counts_400_and_422_and_runs_after_the_guard(gw_client, monkeypatch):
    monkeypatch.setattr(settings, "SCRAPE_RATE_LIMIT_PER_MINUTE", 2)
    assert (await gw_client.get(f"{BASE}/find", headers=MASTER_HEADERS)).status_code == 400
    assert (await gw_client.get(f"{BASE}/movie/0", headers=MASTER_HEADERS)).status_code == 422
    assert (await gw_client.get(f"{BASE}/cache/stats", headers=MASTER_HEADERS)).status_code == 429
    # No key: the guard answers first (401), the limiter is never reached.
    assert (await gw_client.get(f"{BASE}/cache/stats")).status_code == 401


async def test_zero_disables_the_limiter(gw_client, monkeypatch):
    monkeypatch.setattr(settings, "SCRAPE_RATE_LIMIT_PER_MINUTE", 0)
    for _ in range(5):
        assert (await gw_client.get(f"{BASE}/cache/stats", headers=MASTER_HEADERS)).status_code == 200


async def test_limiter_refuses_a_request_without_principal():
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    with pytest.raises(HTTPException) as caught:
        await scrape_v1.enforce_scrape_rate_limit(request)
    assert caught.value.status_code == 401
    assert caught.value.detail["code"] == "invalid_api_key"


# --- 400 vs 422 (FR-015b) -------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    f"{BASE}/match?type=film&title=x",
    f"{BASE}/match?title=x",
    f"{BASE}/match?type=movie&title=",
    f"{BASE}/match?type=movie&title=" + "x" * 301,
    f"{BASE}/match?type=movie&title=x&year=1849",
    f"{BASE}/match?type=movie&title=x&year=2201",
    f"{BASE}/match?type=movie&title=x&year=abc",
    f"{BASE}/search?type=movie&query=",
    f"{BASE}/search?type=movie&query=" + "x" * 301,
    f"{BASE}/search?type=movies&query=x",
    f"{BASE}/movie/0",
    f"{BASE}/movie/1000000000",
    f"{BASE}/movie/abc",
    f"{BASE}/tv/0",
    f"{BASE}/tv/1396/season/1000",
    f"{BASE}/tv/1396/season/-1",
    f"{BASE}/match?type=movie&title=x&refresh=maybe",
])
async def test_type_and_bound_errors_are_native_422(gw_client, gw_http, url):
    route = gw_http.route().respond(200, json={})
    response = await gw_client.get(url, headers=MASTER_HEADERS)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)
    assert route.call_count == 0


@pytest.mark.parametrize("url", [
    f"{BASE}/match?type=movie&title=x&language=fr",
    f"{BASE}/match?type=movie&title=%21%21%21",
    f"{BASE}/movie/603?language=en",
    f"{BASE}/tv/1396/season/0?language=EN-us",
    f"{BASE}/search?type=movie&query=x&language=xx-xx",
    f"{BASE}/find",
    f"{BASE}/find?imdbId=tt0133093&tvdbId=81189",
    f"{BASE}/find?imdbId=tt1",
    f"{BASE}/find?tvdbId=x1",
])
async def test_semantic_errors_are_400_invalid_parameter(gw_client, gw_http, url):
    route = gw_http.route().respond(200, json={})
    response = await gw_client.get(url, headers=MASTER_HEADERS)
    assert response.status_code == 400, response.text
    assert set(response.json()["detail"]) == {"code", "message"}
    assert response.json()["detail"]["code"] == "invalid_parameter"
    assert route.call_count == 0


# --- Metric (FR-036, D2.12) -------------------------------------------------------------------


def _sample(route: str, outcome: str) -> float | None:
    return REGISTRY.get_sample_value(
        "plexhub_scrape_gateway_requests_total", {"route": route, "outcome": outcome})


def test_metric_is_zero_initialised_for_every_route_and_outcome():
    for route in SCRAPE_GATEWAY_ROUTES:
        for outcome in SCRAPE_GATEWAY_OUTCOMES:
            assert _sample(route, outcome) is not None, (route, outcome)


async def test_metric_counts_hit_miss_and_error(gw_client, gw_http):
    gw_http.get(f"{TMDB}/tv/1396/season/1").respond(200, json=load("season_1396_1_fr.json"))
    before = {o: _sample("season", o) for o in SCRAPE_GATEWAY_OUTCOMES}
    await gw_client.get(f"{BASE}/tv/1396/season/1", headers=MASTER_HEADERS)
    await gw_client.get(f"{BASE}/tv/1396/season/1", headers=MASTER_HEADERS)
    await gw_client.get(f"{BASE}/tv/1396/season/1?language=bad", headers=MASTER_HEADERS)
    after = {o: _sample("season", o) for o in SCRAPE_GATEWAY_OUTCOMES}
    assert {o: after[o] - before[o] for o in SCRAPE_GATEWAY_OUTCOMES} == {
        "hit": 1, "miss": 1, "coalesced": 0, "error": 1}
    stats_before = _sample("cache_stats", "hit")
    await gw_client.get(f"{BASE}/cache/stats", headers=MASTER_HEADERS)
    assert _sample("cache_stats", "hit") - stats_before == 1


# --- OpenAPI (FR-016, FR-025, FR-028b) ---------------------------------------------------------


def test_openapi_has_the_nine_routes_and_the_schemas():
    from app.main import app

    spec = app.openapi()
    paths = spec["paths"]
    for method, path in list(READ_ROUTES) + list(ADMIN_ROUTES):
        assert method.lower() in paths[path], (method, path)
    schemas = spec["components"]["schemas"]
    for name in ("MatchResponse", "ScrapeSearchResponse", "MovieResponse", "ShowResponse",
                 "SeasonResponse", "FindResponse", "MovieDetails", "ShowDetails",
                 "SeasonDetails", "EpisodeDetails", "Candidate", "Rating", "Images",
                 "CacheStatsResponse", "InvalidateRequest", "InvalidateResponse",
                 "PurgeRequest", "PurgeResponse", "ErrorResponse", "ErrorDetail"):
        assert name in schemas, name
    def params(path: str) -> set[str]:
        return {p["name"] for p in paths[path]["get"]["parameters"] if p["in"] != "header"}

    match = paths[f"{BASE}/match"]["get"]
    assert params(f"{BASE}/match") == {"type", "title", "year", "language", "refresh"}
    assert params(f"{BASE}/find") == {"imdbId", "tvdbId"}
    assert params(f"{BASE}/movie/{{tmdbId}}") == {"tmdbId", "language", "refresh"}
    assert params(f"{BASE}/tv/{{tmdbId}}/season/{{seasonNumber}}") == {
        "tmdbId", "seasonNumber", "language", "refresh"}
    assert "429" in match["responses"] and "504" in match["responses"]
    assert "schemaVersion" in schemas["MatchResponse"]["properties"]


def test_no_openapi_component_is_renamed_by_a_name_clash():
    """A gateway model named like an existing one would make FastAPI rename
    BOTH components (`app__api__ai__SearchResponse`), changing the existing
    contract of `/api/ai` (B1-11)."""
    from app.main import app

    names = set(app.openapi()["components"]["schemas"])
    assert not [n for n in names if n.startswith("app__")]
    assert "SearchResponse" in names                 # the /api/ai component, unchanged
    assert "ScrapeSearchResponse" in names
