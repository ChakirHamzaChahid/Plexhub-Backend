"""B1-11 — `GET /api/scrape/v1/match` (US2, PRD FR-007, FR-016..FR-019, FR-024, X17).

TMDB/OMDb are mocked with respx (canary keys), the cache is a real file-backed
SQLite. The worker's score and thresholds decide the verdict.
"""
from __future__ import annotations

import pytest

from app.services import scrape_gateway_cache as cache
from app.services import scrape_keys
from app.services.scrape_gateway_cache import counters
from tests._scrape_v1_support import BASE, MASTER_HEADERS, OMDB, TMDB, load, search_hit

DAY = 86_400_000


@pytest.fixture(autouse=True)
def _auto(gw_flights_cleanup):
    yield


def _match(client, **params):
    params.setdefault("type", "movie")
    return client.get(f"{BASE}/match", params=params, headers=MASTER_HEADERS)


def _mock_matrix(http, *, results=None):
    search = http.get(f"{TMDB}/search/movie").respond(
        200, json={"results": results if results is not None else [search_hit(603, "Matrix")]},
    )
    movie = http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    omdb = http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    return search, movie, omdb


ENVELOPE_KEYS = {"schemaVersion", "cacheKey", "language", "fetchedAt", "expiresAt", "cache", "result"}


async def test_matched_miss_then_hit_without_any_provider_call(gw_client, gw_http):
    search, movie, omdb = _mock_matrix(gw_http)

    first = await _match(gw_client, title="matrix", year=1999)
    assert first.status_code == 200, first.text
    body = first.json()
    assert ENVELOPE_KEYS | {"confidence", "tmdbId", "mediaType", "candidates", "details"} == set(body)
    assert body["schemaVersion"] == 1
    assert body["cacheKey"] == "1|fr-FR|movie|matrix|1999"
    assert body["language"] == "fr-FR"
    assert body["cache"] == "miss"
    assert body["result"] == "matched"
    assert body["tmdbId"] == 603 and body["mediaType"] == "movie"
    assert body["candidates"] == []
    assert 0 < body["confidence"] <= 1
    assert body["expiresAt"] - body["fetchedAt"] == 30 * DAY
    details = body["details"]
    assert details["tmdbId"] == 603 and details["omdbStatus"] == "ok"
    assert [r["source"] for r in details["ratings"]] == ["tmdb", "imdb", "metacritic", "rottenTomatoes"]
    # Year search matched: no second search without the year.
    assert (search.call_count, movie.call_count, omdb.call_count) == (1, 1, 1)

    second = await _match(gw_client, title="matrix", year=1999)
    assert second.status_code == 200
    hit = second.json()
    assert hit["cache"] == "hit"
    assert {**hit, "cache": "miss"} == body          # a hit renders exactly the miss
    assert (search.call_count, movie.call_count, omdb.call_count) == (1, 1, 1)
    assert (counters.hits, counters.misses) == (1, 1)


async def test_first_compute_writes_match_and_details_with_the_same_dates(gw_client, gw_http, gw_db):
    _mock_matrix(gw_http)
    body = (await _match(gw_client, title="matrix", year=1999)).json()

    match_entry = await cache.get(
        scrape_keys.match_key("movie", "matrix", 1999, "fr-FR"), session_factory=gw_db)
    movie_entry = await cache.get(scrape_keys.movie_key(603, "fr-FR"), session_factory=gw_db)
    assert match_entry is not None and movie_entry is not None
    # X17 / FR-018: the match entry only stores {confidence, tmdbId}.
    assert set(match_entry.payload) == {"confidence", "tmdbId"}
    assert match_entry.payload["tmdbId"] == 603
    assert (match_entry.fetched_at, match_entry.expires_at) == (
        movie_entry.fetched_at, movie_entry.expires_at)
    assert body["fetchedAt"] == match_entry.fetched_at
    # The cached details carry neither the OMDb ratings nor omdbStatus (D2.4).
    assert "omdbStatus" not in movie_entry.payload
    assert [r["source"] for r in movie_entry.payload["ratings"]] == ["tmdb"]


async def test_language_is_sent_to_every_tmdb_call_and_is_part_of_the_key(gw_client, gw_http):
    search, movie, _ = _mock_matrix(gw_http)
    await _match(gw_client, title="matrix", year=1999)
    assert search.calls.last.request.url.params["language"] == "fr-FR"
    assert movie.calls.last.request.url.params["language"] == "fr-FR"
    assert movie.calls.last.request.url.params["include_image_language"] == "fr,en,null"

    en = (await _match(gw_client, title="matrix", year=1999, language="en-US")).json()
    assert en["cacheKey"] == "1|en-US|movie|matrix|1999" and en["language"] == "en-US"
    assert en["cache"] == "miss"
    assert search.calls.last.request.url.params["language"] == "en-US"
    assert movie.calls.last.request.url.params["language"] == "en-US"
    assert movie.calls.last.request.url.params["include_image_language"] == "en,null"
    assert (search.call_count, movie.call_count) == (2, 2)


async def test_ambiguous_candidates_are_stored_bounded_sorted_and_replayed(gw_client, gw_http, gw_db):
    ids = [9012, 604, 9007, 9003, 9011, 9001, 9010, 9002, 9009, 9004, 9008, 9005]
    search = gw_http.get(f"{TMDB}/search/movie").respond(
        200, json={"results": [search_hit(i, "Matrix") for i in ids]})
    movie = gw_http.get(f"{TMDB}/movie/{604}").respond(200, json={"id": 604})

    body = (await _match(gw_client, title="matrix", year=1999)).json()
    assert body["result"] == "ambiguous"
    assert body["tmdbId"] is None and body["details"] is None
    cands = body["candidates"]
    assert 1 <= len(cands) <= 10
    assert [(c["score"], c["tmdbId"]) for c in cands] == sorted(
        ((c["score"], c["tmdbId"]) for c in cands), key=lambda t: (-t[0], t[1]))
    assert body["expiresAt"] - body["fetchedAt"] == 3 * DAY       # negative TTL
    assert movie.call_count == 0
    assert search.call_count == 2                                # with the year, then without

    entry = await cache.get(
        scrape_keys.match_key("movie", "matrix", 1999, "fr-FR"), session_factory=gw_db)
    assert entry.result == "ambiguous" and entry.payload["candidates"] == cands

    hit = (await _match(gw_client, title="matrix", year=1999)).json()
    assert hit["cache"] == "hit" and hit["candidates"] == cands
    assert search.call_count == 2


async def test_nomatch_is_cached_with_the_negative_ttl(gw_client, gw_http):
    search = gw_http.get(f"{TMDB}/search/movie").respond(200, json={"results": []})
    body = (await _match(gw_client, title="zzqx unknown", year=2001)).json()
    assert body["result"] == "nomatch"
    assert body["confidence"] is None and body["tmdbId"] is None
    assert body["candidates"] == [] and body["details"] is None
    assert body["expiresAt"] - body["fetchedAt"] == 3 * DAY
    assert search.call_count == 2

    hit = (await _match(gw_client, title="zzqx unknown", year=2001)).json()
    assert hit["cache"] == "hit" and hit["result"] == "nomatch"
    assert search.call_count == 2


async def test_missing_year_gives_an_empty_year_segment_and_a_single_search(gw_client, gw_http):
    search = gw_http.get(f"{TMDB}/search/movie").respond(200, json={"results": []})
    body = (await _match(gw_client, title="matrix")).json()
    assert body["cacheKey"] == "1|fr-FR|movie|matrix|"
    assert search.call_count == 1
    assert "year" not in search.calls.last.request.url.params


async def test_show_match_uses_the_tv_endpoints(gw_client, gw_http):
    search = gw_http.get(f"{TMDB}/search/tv").respond(200, json={"results": [
        {"id": 1396, "name": "Breaking Bad", "original_name": "Breaking Bad",
         "first_air_date": "2008-01-20", "vote_count": 10}]})
    tv = gw_http.get(f"{TMDB}/tv/1396").respond(200, json=load("tv_1396_fr.json"))
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0903747_partial.json"))
    body = (await gw_client.get(f"{BASE}/match", headers=MASTER_HEADERS, params={
        "type": "show", "title": "breaking bad", "year": 2008})).json()
    assert body["cacheKey"] == "1|fr-FR|show|breaking bad|2008"
    assert body["result"] == "matched" and body["mediaType"] == "show"
    assert body["details"]["tmdbId"] == 1396 and "seasons" in body["details"]
    assert search.calls.last.request.url.params["first_air_date_year"] == "2008"
    assert tv.call_count == 1


async def test_x17_invalidated_details_are_refetched_in_the_same_flight(gw_client, gw_http, gw_db):
    search, movie, omdb = _mock_matrix(gw_http)
    first = (await _match(gw_client, title="matrix", year=1999)).json()
    assert await cache.invalidate_family("movie", 603, session_factory=gw_db) == 1

    again = await _match(gw_client, title="matrix", year=1999)
    body = again.json()
    assert body["cache"] == "miss" and body["result"] == "matched"
    assert search.call_count == 1                # the match entry was still fresh
    assert movie.call_count == 2                 # details re-fetched
    assert omdb.call_count == 1                  # omdb entry still fresh
    assert body["fetchedAt"] == first["fetchedAt"]
    restored = await cache.get(scrape_keys.movie_key(603, "fr-FR"), session_factory=gw_db)
    assert restored is not None
    match_entry = await cache.get(
        scrape_keys.match_key("movie", "matrix", 1999, "fr-FR"), session_factory=gw_db)
    assert body["expiresAt"] == min(match_entry.expires_at, restored.expires_at)

    hit = (await _match(gw_client, title="matrix", year=1999)).json()
    assert hit["cache"] == "hit" and movie.call_count == 2


async def test_expires_at_is_the_min_of_match_and_details(gw_client, gw_http, gw_db):
    from app.utils.time import now_ms

    _, movie, _ = _mock_matrix(gw_http)
    key = scrape_keys.match_key("movie", "matrix", 1999, "fr-FR")
    t0 = now_ms()
    # Match written 10 days ago (expires in 20 d), details absent: re-fetched now (30 d).
    await cache.put(key, "matched", {"confidence": 0.97, "tmdbId": 603}, t0 - 10 * DAY,
                    session_factory=gw_db)
    first = (await _match(gw_client, title="matrix", year=1999)).json()
    assert first["cache"] == "miss" and movie.call_count == 1
    assert first["fetchedAt"] == t0 - 10 * DAY
    assert first["expiresAt"] == t0 - 10 * DAY + 30 * DAY  # the match expiry is the min

    # Details written 25 days ago (expire in 5 d) under a fresh match: details win.
    await cache.put(key, "matched", {"confidence": 0.97, "tmdbId": 603}, t0, session_factory=gw_db)
    details = (await cache.get(scrape_keys.movie_key(603, "fr-FR"), session_factory=gw_db)).payload
    await cache.put(scrape_keys.movie_key(603, "fr-FR"), "found", details, t0 - 25 * DAY,
                    session_factory=gw_db)
    hit = (await _match(gw_client, title="matrix", year=1999)).json()
    assert hit["cache"] == "hit" and movie.call_count == 1
    assert (hit["fetchedAt"], hit["expiresAt"]) == (t0, t0 - 25 * DAY + 30 * DAY)


async def test_refresh_redoes_search_and_details(gw_client, gw_http):
    search, movie, omdb = _mock_matrix(gw_http)
    await _match(gw_client, title="matrix", year=1999)
    body = (await _match(gw_client, title="matrix", year=1999, refresh="true")).json()
    assert body["cache"] == "miss" and body["result"] == "matched"
    assert (search.call_count, movie.call_count) == (2, 2)
    assert omdb.call_count == 1                      # FR-024: fresh omdb entry kept


async def test_refresh_failure_returns_the_error_and_keeps_the_old_entry(gw_client, gw_http, gw_db):
    import httpx

    gw_http.get(f"{TMDB}/search/movie").mock(side_effect=[
        httpx.Response(200, json={"results": [search_hit(603, "Matrix")]}),
        httpx.Response(500),
    ])
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    first = (await _match(gw_client, title="matrix", year=1999)).json()

    failed = await _match(gw_client, title="matrix", year=1999, refresh="true")
    assert failed.status_code == 502
    assert failed.json()["detail"]["code"] == "upstream_error"
    entry = await cache.get(
        scrape_keys.match_key("movie", "matrix", 1999, "fr-FR"), session_factory=gw_db)
    assert entry is not None and entry.fetched_at == first["fetchedAt"]
    hit = (await _match(gw_client, title="matrix", year=1999)).json()
    assert hit["cache"] == "hit"


async def test_matched_details_failure_stores_nothing(gw_client, gw_http, gw_db):
    gw_http.get(f"{TMDB}/search/movie").respond(200, json={"results": [search_hit(603, "Matrix")]})
    gw_http.get(f"{TMDB}/movie/603").respond(404)
    response = await _match(gw_client, title="matrix", year=1999)
    assert response.status_code == 502
    assert await cache.get(
        scrape_keys.match_key("movie", "matrix", 1999, "fr-FR"), session_factory=gw_db) is None
    assert await cache.get(scrape_keys.movie_key(603, "fr-FR"), session_factory=gw_db) is None


async def test_title_is_used_as_received_without_recleaning(gw_client, gw_http):
    gw_http.get(f"{TMDB}/search/movie").respond(200, json={"results": []})
    body = (await _match(gw_client, title="Terminator (VF)", year=1984)).json()
    assert body["cacheKey"] == "1|fr-FR|" + scrape_keys.match_key(
        "movie", "Terminator (VF)", 1984, "fr-FR").key


@pytest.mark.parametrize("title", ["!!!", "...", " - "])
async def test_punctuation_only_title_is_a_400(gw_client, gw_http, title):
    route = gw_http.get(f"{TMDB}/search/movie").respond(200, json={"results": []})
    response = await _match(gw_client, title=title)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_parameter"
    assert route.call_count == 0


@pytest.mark.parametrize("language", ["fr", "fr-fr", "FR-FR", "fr_FR", "", "fr-FRA", "fr-FR\n"])
async def test_invalid_language_is_a_400(gw_client, gw_http, language):
    response = await _match(gw_client, title="matrix", language=language)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_parameter"


async def test_cache_write_failure_still_serves_the_response(gw_client, gw_http, monkeypatch, caplog):
    import logging

    search, _, _ = _mock_matrix(gw_http)

    async def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cache, "put_many", _boom)
    handler_logger = logging.getLogger("plexhub.scrape_gateway")
    handler_logger.addHandler(caplog.handler)
    try:
        caplog.set_level(logging.WARNING)
        first = await _match(gw_client, title="matrix", year=1999)
    finally:
        handler_logger.removeHandler(caplog.handler)
    assert first.status_code == 200 and first.json()["cache"] == "miss"
    assert first.json()["result"] == "matched"
    messages = [r.getMessage() for r in caplog.records if r.name == "plexhub.scrape_gateway"]
    assert any("cache write failed" in m and "RuntimeError" in m for m in messages)
    assert not any("disk full" in m for m in messages)       # type only
    second = await _match(gw_client, title="matrix", year=1999)
    assert second.json()["cache"] == "miss" and search.call_count == 2
