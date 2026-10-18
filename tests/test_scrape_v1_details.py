"""B1-11 — `/movie/{id}`, `/tv/{id}`, `/tv/{id}/season/{n}` (US3, FR-021, FR-023,
FR-024, ADR 0009 D2.10: OMDb composition)."""
from __future__ import annotations

import copy

import httpx
import pytest

from app.config import settings
from app.models import scrape_schemas as S
from app.services import scrape_gateway_cache as cache
from app.services import scrape_keys
from tests._scrape_v1_support import BASE, MASTER_HEADERS, OMDB, TMDB, load

DAY = 86_400_000

# Contract 6.1 / 6.2 / 6.3: the exact field sets.
MOVIE_FIELDS = {
    "tmdbId", "title", "originalTitle", "overview", "tagline", "releaseDate", "year",
    "runtimeMinutes", "status", "contentRating", "genres", "studios", "countries",
    "collection", "externalIds", "ratings", "omdbStatus", "cast", "crew", "trailer",
    "images", "imageBaseUrl",
}
SHOW_FIELDS = (MOVIE_FIELDS - {"releaseDate", "runtimeMinutes", "collection"}) | {
    "firstAirDate", "episodeRuntimeMinutes", "numberOfSeasons", "numberOfEpisodes", "seasons",
}
SEASON_FIELDS = {
    "showTmdbId", "tmdbId", "seasonNumber", "name", "overview", "airDate", "externalIds",
    "images", "imageBaseUrl", "episodes",
}


@pytest.fixture(autouse=True)
def _auto(gw_flights_cleanup):
    yield


def _get(client, path, **params):
    return client.get(f"{BASE}{path}", params=params, headers=MASTER_HEADERS)


# --- Movie ----------------------------------------------------------------------


async def test_movie_found_miss_then_hit(gw_client, gw_http):
    movie = gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    omdb = gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))

    first = await _get(gw_client, "/movie/603")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["cacheKey"] == "1|fr-FR|movie|603" and body["cache"] == "miss"
    assert body["result"] == "found" and body["language"] == "fr-FR"
    assert body["expiresAt"] - body["fetchedAt"] == 30 * DAY
    details = body["details"]
    assert set(details) == MOVIE_FIELDS
    assert details["runtimeMinutes"] == 136
    assert details["externalIds"]["imdb"] == "tt0133093"
    assert details["imageBaseUrl"] == "https://image.tmdb.org/t/p/"
    assert details["omdbStatus"] == "ok"
    assert [(r["source"], r["max"]) for r in details["ratings"]] == [
        ("tmdb", 10), ("imdb", 10), ("metacritic", 100), ("rottenTomatoes", 100)]
    S.MovieResponse.model_validate(body)
    params = movie.calls.last.request.url.params
    assert params["append_to_response"] == "credits,external_ids,release_dates,videos,images"
    assert params["language"] == "fr-FR" and params["include_image_language"] == "fr,en,null"
    assert omdb.calls.last.request.url.params["i"] == "tt0133093"

    hit = (await _get(gw_client, "/movie/603")).json()
    assert hit["cache"] == "hit" and {**hit, "cache": "miss"} == body
    assert (movie.call_count, omdb.call_count) == (1, 1)


async def test_movie_not_found_is_a_cached_negative(gw_client, gw_http):
    movie = gw_http.get(f"{TMDB}/movie/999999").respond(404, json={"status_code": 34})
    body = (await _get(gw_client, "/movie/999999")).json()
    assert body["result"] == "not_found" and body["details"] is None
    assert body["expiresAt"] - body["fetchedAt"] == 3 * DAY
    hit = (await _get(gw_client, "/movie/999999")).json()
    assert hit["cache"] == "hit" and hit["result"] == "not_found"
    assert movie.call_count == 1


async def test_content_rating_region_follows_language(gw_client, gw_http):
    raw = load("movie_603_fr.json")
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=raw)
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    fr = (await _get(gw_client, "/movie/603")).json()["details"]["contentRating"]
    us = (await _get(gw_client, "/movie/603", language="en-US")).json()["details"]["contentRating"]
    from app.services.tmdb_service import _parse_movie_certification

    assert fr == _parse_movie_certification(raw, "FR")
    assert us == _parse_movie_certification(raw, "US")


async def test_refresh_redoes_tmdb_but_not_a_fresh_omdb_entry(gw_client, gw_http):
    movie = gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    omdb = gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    first = (await _get(gw_client, "/movie/603")).json()
    again = (await _get(gw_client, "/movie/603", refresh="true")).json()
    assert again["cache"] == "miss" and again["fetchedAt"] >= first["fetchedAt"]
    assert (movie.call_count, omdb.call_count) == (2, 1)


async def test_refresh_failure_keeps_the_old_entry(gw_client, gw_http, gw_db):
    gw_http.get(f"{TMDB}/movie/603").mock(side_effect=[
        httpx.Response(200, json=load("movie_603_fr.json")), httpx.Response(500)])
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    first = (await _get(gw_client, "/movie/603")).json()
    failed = await _get(gw_client, "/movie/603", refresh="true")
    assert failed.status_code == 502
    entry = await cache.get(scrape_keys.movie_key(603, "fr-FR"), session_factory=gw_db)
    assert entry.fetched_at == first["fetchedAt"]
    assert (await _get(gw_client, "/movie/603")).json()["cache"] == "hit"


async def test_deterministic_order_whatever_the_tmdb_order(gw_client, gw_http):
    raw = load("movie_603_fr.json")
    shuffled = copy.deepcopy(raw)
    for kind in ("posters", "backdrops", "logos"):
        shuffled["images"][kind] = list(reversed(shuffled["images"].get(kind) or []))
    gw_http.get(f"{TMDB}/movie/603").mock(side_effect=[
        httpx.Response(200, json=raw), httpx.Response(200, json=shuffled)])
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    one = (await _get(gw_client, "/movie/603")).json()["details"]
    two = (await _get(gw_client, "/movie/603", refresh="true")).json()["details"]
    # Images are ranked by language (stable inside a rank): the ranks hold.
    def ranks(paths, entries):
        lang = {e["file_path"]: e.get("iso_639_1") for e in entries}
        order = {"fr": 0, "en": 1, None: 2}
        return [order.get(lang[p], 3) for p in paths]

    for kind in ("posters", "backdrops"):
        entries = raw["images"].get(kind) or []
        assert ranks(one["images"][kind], entries) == sorted(ranks(one["images"][kind], entries))
        assert ranks(two["images"][kind], entries) == sorted(ranks(two["images"][kind], entries))
    assert one["cast"] == two["cast"] and one["genres"] == two["genres"]


# --- OMDb statuses (US3-AC5, D2.10) -------------------------------------------------


async def test_omdb_not_found_is_cached_as_a_negative(gw_client, gw_http, gw_db):
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    omdb = gw_http.get(OMDB).respond(200, json={"Response": "False", "Error": "Incorrect IMDb ID."})
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["details"]["omdbStatus"] == "not_found"
    assert [r["source"] for r in body["details"]["ratings"]] == ["tmdb"]
    assert body["expiresAt"] - body["fetchedAt"] == 30 * DAY        # unaffected
    entry = await cache.get(scrape_keys.omdb_key("tt0133093"), session_factory=gw_db)
    assert entry is not None and entry.result == "not_found"
    assert entry.expires_at - entry.fetched_at == 3 * DAY
    again = (await _get(gw_client, "/movie/603")).json()
    assert again["details"]["omdbStatus"] == "not_found" and omdb.call_count == 1


async def test_omdb_not_configured(gw_client, gw_http, monkeypatch):
    monkeypatch.setattr(settings, "OMDB_API_KEY", "")
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    omdb = gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["details"]["omdbStatus"] == "not_configured"
    assert body["expiresAt"] - body["fetchedAt"] == 30 * DAY
    assert omdb.call_count == 0


@pytest.mark.parametrize("case", ["guard", "request_limit"])
async def test_omdb_budget_exhausted_shortens_expires_at(gw_client, gw_http, gw_db, monkeypatch, case):
    if case == "guard":
        monkeypatch.setattr(settings, "OMDB_DAILY_LIMIT", 0)
        omdb = gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    else:
        omdb = gw_http.get(OMDB).respond(401, json={"Response": "False", "Error": "Request limit reached!"})
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["details"]["omdbStatus"] == "budget_exhausted"
    assert body["expiresAt"] - body["fetchedAt"] == 3 * DAY
    assert await cache.get(scrape_keys.omdb_key("tt0133093"), session_factory=gw_db) is None
    assert omdb.call_count == (0 if case == "guard" else 1)
    # The TMDB entry keeps its own (success) expiry: the hit re-asks OMDb.
    again = (await _get(gw_client, "/movie/603")).json()
    assert again["cache"] == "hit" and again["details"]["omdbStatus"] == "budget_exhausted"


async def test_omdb_error_is_not_cached_and_shortens_expires_at(gw_client, gw_http, gw_db):
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    omdb = gw_http.get(OMDB).mock(side_effect=[
        httpx.Response(500), httpx.Response(200, json=load("omdb_tt0133093.json"))])
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["details"]["omdbStatus"] == "error"
    assert body["expiresAt"] - body["fetchedAt"] == 3 * DAY
    assert await cache.get(scrape_keys.omdb_key("tt0133093"), session_factory=gw_db) is None
    again = (await _get(gw_client, "/movie/603")).json()
    assert again["cache"] == "hit" and again["details"]["omdbStatus"] == "ok"
    assert again["expiresAt"] - again["fetchedAt"] == 30 * DAY
    assert omdb.call_count == 2


async def test_omdb_invalid_key_is_not_configured(gw_client, gw_http):
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=load("movie_603_fr.json"))
    gw_http.get(OMDB).respond(401, json={"Response": "False", "Error": "Invalid API key!"})
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["details"]["omdbStatus"] == "not_configured"


async def test_no_imdb_id_skips_omdb(gw_client, gw_http):
    raw = load("movie_603_fr.json")
    raw["imdb_id"] = None
    raw["external_ids"]["imdb_id"] = None
    gw_http.get(f"{TMDB}/movie/603").respond(200, json=raw)
    omdb = gw_http.get(OMDB).respond(200, json=load("omdb_tt0133093.json"))
    body = (await _get(gw_client, "/movie/603")).json()
    assert body["details"]["omdbStatus"] == "no_imdb_id"
    assert body["details"]["externalIds"]["imdb"] is None
    assert omdb.call_count == 0


# --- Show ---------------------------------------------------------------------------


async def test_show_found_with_its_seasons(gw_client, gw_http):
    tv = gw_http.get(f"{TMDB}/tv/1396").respond(200, json=load("tv_1396_fr.json"))
    gw_http.get(OMDB).respond(200, json=load("omdb_tt0903747_partial.json"))
    body = (await _get(gw_client, "/tv/1396")).json()
    assert body["cacheKey"] == "1|fr-FR|show|1396" and body["result"] == "found"
    details = body["details"]
    assert set(details) == SHOW_FIELDS
    numbers = [s["seasonNumber"] for s in details["seasons"]]
    assert numbers == sorted(numbers)
    assert details["externalIds"]["tvdb"] == "81189"
    S.ShowResponse.model_validate(body)
    params = tv.calls.last.request.url.params
    assert params["append_to_response"] == "credits,external_ids,content_ratings,videos,images"
    assert (await _get(gw_client, "/tv/1396")).json()["cache"] == "hit"
    assert tv.call_count == 1


async def test_show_not_found(gw_client, gw_http):
    gw_http.get(f"{TMDB}/tv/42").respond(404)
    body = (await _get(gw_client, "/tv/42")).json()
    assert body["cacheKey"] == "1|fr-FR|show|42"
    assert body["result"] == "not_found" and body["details"] is None


# --- Season -------------------------------------------------------------------------


async def test_season_found_in_one_call_with_sorted_episodes(gw_client, gw_http):
    route = gw_http.get(f"{TMDB}/tv/1396/season/1").respond(200, json=load("season_1396_1_fr.json"))
    body = (await _get(gw_client, "/tv/1396/season/1")).json()
    assert body["cacheKey"] == "1|fr-FR|season|1396|1"
    assert body["result"] == "found"
    details = body["details"]
    assert set(details) == SEASON_FIELDS
    assert details["showTmdbId"] == 1396 and details["seasonNumber"] == 1
    assert [e["episodeNumber"] for e in details["episodes"]] == [1, 2, 3, 4, 5, 6, 7]
    assert route.call_count == 1 and len(gw_http.calls) == 1        # no OMDb for a season
    params = route.calls.last.request.url.params
    assert params["append_to_response"] == "external_ids,images"
    assert params["language"] == "fr-FR"
    S.SeasonResponse.model_validate(body)
    assert (await _get(gw_client, "/tv/1396/season/1")).json()["cache"] == "hit"
    assert route.call_count == 1


async def test_specials_season_zero_and_missing_season(gw_client, gw_http):
    raw = load("season_1396_1_fr.json")
    raw["season_number"] = 0
    gw_http.get(f"{TMDB}/tv/1396/season/0").respond(200, json=raw)
    gw_http.get(f"{TMDB}/tv/1396/season/99").respond(404)
    specials = (await _get(gw_client, "/tv/1396/season/0")).json()
    assert specials["cacheKey"] == "1|fr-FR|season|1396|0" and specials["result"] == "found"
    missing = (await _get(gw_client, "/tv/1396/season/99")).json()
    assert missing["result"] == "not_found" and missing["details"] is None
    assert missing["expiresAt"] - missing["fetchedAt"] == 3 * DAY


async def test_season_of_500_full_episodes_stays_under_4_mib(gw_client, gw_http):
    raw = load("season_1396_1_fr.json")
    template = raw["episodes"][0]
    person = {"id": 1, "name": "N" * 40, "character": "C" * 40, "job": "Director",
              "department": "Directing", "profile_path": "/" + "p" * 30 + ".jpg"}
    episodes = []
    for n in range(1, 501):
        ep = copy.deepcopy(template)
        ep.update({"id": 100_000 + n, "episode_number": n, "overview": "o" * 1000,
                   "name": "e" * 100})
        ep["crew"] = [{**person, "id": 2000 + i} for i in range(10)]
        ep["guest_stars"] = [{**person, "id": 3000 + i} for i in range(20)]
        episodes.append(ep)
    raw["episodes"] = episodes
    gw_http.get(f"{TMDB}/tv/1396/season/1").respond(200, json=raw)
    response = await _get(gw_client, "/tv/1396/season/1")
    assert response.status_code == 200
    assert len(response.json()["details"]["episodes"]) == 500
    assert len(response.content) < 4 * 1024 * 1024
