"""B1-11 — `GET /find` (US4, FR-022, X19, X20, X24) and `GET /search` (US5, FR-020)."""
from __future__ import annotations

import pytest

from app.services import scrape_gateway_cache as cache
from app.services.scrape_gateway_cache import counters
from tests._scrape_v1_support import BASE, MASTER_HEADERS, TMDB, load, search_hit


@pytest.fixture(autouse=True)
def _auto(gw_flights_cleanup):
    yield


def _get(client, path, **params):
    return client.get(f"{BASE}{path}", params=params, headers=MASTER_HEADERS)


# --- /find --------------------------------------------------------------------------


async def test_find_by_imdb_id_miss_then_hit(gw_client, gw_http):
    route = gw_http.get(f"{TMDB}/find/tt0133093").respond(200, json=load("find_imdb_mixed.json"))
    body = (await _get(gw_client, "/find", imdbId="tt0133093")).json()
    assert set(body) == {"schemaVersion", "cacheKey", "language", "fetchedAt", "expiresAt",
                         "cache", "result", "matches"}
    assert body["cacheKey"] == "1|-|find|imdb|tt0133093" and body["language"] == "-"
    assert body["result"] == "found" and body["cache"] == "miss"
    matches = body["matches"]
    assert 1 <= len(matches) <= 5
    assert matches[0] == {"mediaType": "movie", "tmdbId": 603, "showTmdbId": None,
                          "seasonNumber": None, "episodeNumber": None}
    assert {m["mediaType"] for m in matches} <= {"movie", "show", "episode"}
    episode = next(m for m in matches if m["mediaType"] == "episode")
    assert (episode["showTmdbId"], episode["seasonNumber"], episode["episodeNumber"]) == (1396, 1, 1)
    params = route.calls.last.request.url.params
    assert params["external_source"] == "imdb_id" and "language" not in params

    hit = (await _get(gw_client, "/find", imdbId="tt0133093")).json()
    assert hit["cache"] == "hit" and hit["matches"] == matches and route.call_count == 1


async def test_find_by_tvdb_id_normalises_leading_zeros(gw_client, gw_http):
    route = gw_http.get(f"{TMDB}/find/81189").respond(200, json={"tv_results": [{"id": 1396}]})
    body = (await _get(gw_client, "/find", tvdbId="0081189")).json()
    assert body["cacheKey"] == "1|-|find|tvdb|81189"
    assert body["matches"] == [{"mediaType": "show", "tmdbId": 1396, "showTmdbId": None,
                                "seasonNumber": None, "episodeNumber": None}]
    assert route.calls.last.request.url.params["external_source"] == "tvdb_id"
    assert (await _get(gw_client, "/find", tvdbId="81189")).json()["cache"] == "hit"


async def test_find_without_any_result_is_a_cached_not_found(gw_client, gw_http):
    route = gw_http.get(f"{TMDB}/find/tt9999999").respond(200, json={
        "movie_results": [], "tv_results": [], "person_results": [{"id": 1}]})
    body = (await _get(gw_client, "/find", imdbId="tt9999999")).json()
    assert body["result"] == "not_found" and body["matches"] == []
    assert body["expiresAt"] - body["fetchedAt"] == 3 * 86_400_000
    assert (await _get(gw_client, "/find", imdbId="tt9999999")).json()["cache"] == "hit"
    assert route.call_count == 1


@pytest.mark.parametrize("params", [
    {},
    {"imdbId": "tt0133093", "tvdbId": "81189"},
    {"imdbId": "tt013"},
    {"imdbId": "TT0133093"},
    {"imdbId": "tt0133093x"},
    {"imdbId": "tt١٢٣٤٥٦٧"},
    {"tvdbId": "abc"},
    {"tvdbId": "0"},
    {"tvdbId": "1234567890123"},
    {"tvdbId": "+7"},
    {"tvdbId": "７"},
    {"tvdbId": ""},
])
async def test_find_parameter_errors_are_400(gw_client, gw_http, params):
    route = gw_http.route().respond(200, json={})
    response = await _get(gw_client, "/find", **params)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_parameter"
    assert route.call_count == 0


# --- /search -------------------------------------------------------------------------


async def test_search_found_is_never_persisted(gw_client, gw_http, gw_db):
    results = [search_hit(i, "Breaking Bad" if i % 2 else "Breaking", date="2008-01-20")
               for i in (12, 3, 7, 1, 9, 4, 11, 2, 8, 5, 10, 6)]
    route = gw_http.get(f"{TMDB}/search/tv").respond(200, json={"results": [
        {**r, "name": r["title"], "original_name": r["title"], "first_air_date": r["release_date"]}
        for r in results]})
    body = (await _get(gw_client, "/search", type="show", query="breaking bad")).json()
    assert body["cacheKey"] is None and body["cache"] == "miss"
    assert body["expiresAt"] == body["fetchedAt"]
    assert body["result"] == "found"
    cands = body["candidates"]
    assert 1 <= len(cands) <= 10
    assert [(c["score"], c["tmdbId"]) for c in cands] == sorted(
        ((c["score"], c["tmdbId"]) for c in cands), key=lambda t: (-t[0], t[1]))
    assert {c["mediaType"] for c in cands} == {"show"}
    assert await cache.stats(session_factory=gw_db) == []           # nothing in the table
    await _get(gw_client, "/search", type="show", query="breaking bad")
    assert route.call_count == 2                                    # never cached
    assert counters.misses == 2 and counters.hits == 0


async def test_search_without_result_is_not_found(gw_client, gw_http):
    gw_http.get(f"{TMDB}/search/movie").respond(200, json={"results": []})
    body = (await _get(gw_client, "/search", type="movie", query="zzqx")).json()
    assert body["result"] == "not_found" and body["candidates"] == []


async def test_search_sends_year_and_language(gw_client, gw_http):
    movie = gw_http.get(f"{TMDB}/search/movie").respond(200, json={"results": [search_hit(603, "Matrix")]})
    tv = gw_http.get(f"{TMDB}/search/tv").respond(200, json={"results": []})
    body = (await _get(gw_client, "/search", type="movie", query="matrix", year=1999,
                       language="en-US")).json()
    assert body["language"] == "en-US"
    params = movie.calls.last.request.url.params
    assert (params["year"], params["language"], params["query"]) == ("1999", "en-US", "matrix")
    await _get(gw_client, "/search", type="show", query="matrix", year=1999)
    assert tv.calls.last.request.url.params["first_air_date_year"] == "1999"
    assert tv.calls.last.request.url.params["language"] == "fr-FR"
    assert movie.call_count == 1                                     # one call, no fallback
