"""B1-08 - strict TMDB/OMDb access for the scrape gateway (ADR 0009 D2.6).

HTTP is mocked with respx (fixtures `tmdb_mock` / `omdb_mock`); `asyncio.sleep`
is neutralised and recorded so no test waits for real.
"""
from __future__ import annotations

import logging
import time

import httpx
import pytest
import pytest_asyncio

from app.services import omdb_service as omdb_mod
from app.services import tmdb_service as tmdb_mod
from app.services.omdb_service import OmdbRatings, OMDbService
from app.services.scrape_provider_errors import ProviderError
from app.services.tmdb_service import (
    TMDBService,
    _parse_movie_certification,
    _parse_tv_certification,
    count_requests,
)

CANARY = "TESTKEY-LEAK-CANARY"


def _deadline(seconds: float = 60.0) -> float:
    return time.monotonic() + seconds


@pytest.fixture
def sleeps(monkeypatch):
    """Record (and skip) every asyncio.sleep done by the services."""
    recorded: list[float] = []

    async def _no_sleep(d):
        recorded.append(d)

    monkeypatch.setattr(tmdb_mod.asyncio, "sleep", _no_sleep)
    return recorded


@pytest_asyncio.fixture
async def tmdb(monkeypatch):
    monkeypatch.setattr(tmdb_mod.settings, "TMDB_API_KEY", CANARY)
    monkeypatch.setattr(tmdb_mod.settings, "TMDB_LANGUAGE", "en-US")
    svc = TMDBService()
    try:
        yield svc
    finally:
        await svc.close()


@pytest_asyncio.fixture
async def omdb(monkeypatch):
    monkeypatch.setattr(omdb_mod.settings, "OMDB_API_KEY", CANARY)
    monkeypatch.setattr(omdb_mod.settings, "OMDB_DAILY_LIMIT", 1000)
    svc = OMDbService()
    try:
        yield svc
    finally:
        await svc.close()


@pytest.fixture
def captured_logs(caplog):
    """Capture EVERY logger (plexhub.* may have propagate=False once app.main
    was imported). httpx stays pinned at WARNING like production (main.py)."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="httpx")
    caplog.set_level(logging.WARNING, logger="httpcore")
    plexhub = logging.getLogger("plexhub")
    plexhub.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        plexhub.removeHandler(caplog.handler)


def _movie(id_, title, date="", overview="", vote=0):
    return {"id": id_, "title": title, "release_date": date, "vote_count": vote,
            "overview": overview, "original_title": title, "poster_path": f"/p{id_}.jpg"}


# --------------------------------------------------------------------------
# ProviderError
# --------------------------------------------------------------------------

def test_provider_error_str_has_no_extra_text():
    err = ProviderError("rate_limited", provider="tmdb", retry_after=12, status_code=429)
    assert str(err) == "tmdb rate_limited HTTP 429"
    assert err.retry_after == 12
    assert str(ProviderError("timeout", provider="omdb")) == "omdb timeout"


def test_provider_error_rejects_unknown_kind_or_provider():
    with pytest.raises(ValueError):
        ProviderError("nope", provider="tmdb")
    with pytest.raises(ValueError):
        ProviderError("timeout", provider="imdb")


# --------------------------------------------------------------------------
# TMDB fetch_* : parameters
# --------------------------------------------------------------------------

async def test_fetch_movie_params(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/movie/603").respond(200, json={"id": 603})
    out = await tmdb.fetch_movie(603, language="fr-FR", deadline=_deadline())
    assert out == {"id": 603}
    q = route.calls.last.request.url.params
    assert q["language"] == "fr-FR"
    assert q["append_to_response"] == "credits,external_ids,release_dates,videos,images"
    assert q["include_image_language"] == "fr,en,null"
    assert q["include_video_language"] == "fr,en,null"
    assert q["api_key"] == CANARY


async def test_fetch_tv_params(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/tv/1396").respond(200, json={"id": 1396})
    await tmdb.fetch_tv(1396, language="en-US", deadline=_deadline())
    q = route.calls.last.request.url.params
    assert q["language"] == "en-US"
    assert q["append_to_response"] == "credits,external_ids,content_ratings,videos,images"
    # short language equal to "en": no duplicated token.
    assert q["include_image_language"] == "en,null"


async def test_fetch_season_params(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/tv/1396/season/2").respond(200, json={"season_number": 2})
    out = await tmdb.fetch_season(1396, 2, language="de-DE", deadline=_deadline())
    assert out["season_number"] == 2
    q = route.calls.last.request.url.params
    assert q["append_to_response"] == "external_ids,images"
    assert q["language"] == "de-DE"
    assert q["include_image_language"] == "de,en,null"


async def test_methods_not_configured_make_no_call(monkeypatch, tmdb_mock):
    monkeypatch.setattr(tmdb_mod.settings, "TMDB_API_KEY", "")
    svc = TMDBService()
    route = tmdb_mock.route().respond(200, json={})
    d = _deadline()
    calls = [
        svc.fetch_movie(1, language="fr-FR", deadline=d),
        svc.fetch_tv(1, language="fr-FR", deadline=d),
        svc.fetch_season(1, 1, language="fr-FR", deadline=d),
        svc.find_external("imdb", "tt1", deadline=d),
        svc.search_scored("movie", "x", 2000, language="fr-FR", deadline=d),
        svc.search_candidates_raw("show", "x", None, language="fr-FR", deadline=d),
    ]
    for coro in calls:
        with pytest.raises(ProviderError) as ei:
            await coro
        assert ei.value.kind == "not_configured"
        assert ei.value.provider == "tmdb"
    assert not route.called
    await svc.close()


async def test_find_external_reduced_and_no_language(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/find/81189").respond(200, json={
        "movie_results": [{"id": 1}], "tv_results": [{"id": 2}],
        "tv_episode_results": [{"id": 3}], "tv_season_results": [{"id": 4}],
        "person_results": [{"id": 5}],
    })
    out = await tmdb.find_external("tvdb", "81189", deadline=_deadline())
    assert out == {"movie_results": [{"id": 1}], "tv_results": [{"id": 2}],
                   "tv_episode_results": [{"id": 3}]}
    q = route.calls.last.request.url.params
    assert q["external_source"] == "tvdb_id"
    assert "language" not in q


async def test_find_external_imdb_and_missing_keys(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/find/tt0133093").respond(200, json={"movie_results": [{"id": 603}]})
    out = await tmdb.find_external("imdb", "tt0133093", deadline=_deadline())
    assert out == {"movie_results": [{"id": 603}], "tv_results": [], "tv_episode_results": []}
    assert route.calls.last.request.url.params["external_source"] == "imdb_id"


async def test_find_external_bad_source(tmdb):
    with pytest.raises(ValueError):
        await tmdb.find_external("foo", "1", deadline=_deadline())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# TMDB error translation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,kind", [
    (401, "not_configured"), (403, "not_configured"), (404, "not_found"),
    (400, "bad_response"), (422, "bad_response"), (500, "bad_response"),
])
async def test_http_status_translation(tmdb, tmdb_mock, sleeps, status, kind):
    tmdb_mock.get("/3/movie/1").respond(status, json={"status_message": "x"})
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == kind
    assert ei.value.status_code == status
    assert sleeps == []  # none of these is retried


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_5xx_gateway_retried_then_unavailable(tmdb, tmdb_mock, sleeps, status):
    route = tmdb_mock.get("/3/movie/1").respond(status)
    before = tmdb.real_request_count
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "unavailable"
    assert ei.value.status_code == status
    assert route.call_count == 4
    assert sleeps == [1, 2, 4]
    assert tmdb.real_request_count - before == 4


async def test_5xx_then_success(tmdb, tmdb_mock, sleeps):
    tmdb_mock.get("/3/movie/1").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={"id": 1})])
    assert await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline()) == {"id": 1}
    assert sleeps == [1]


async def test_connect_error_unavailable(tmdb, tmdb_mock, sleeps):
    tmdb_mock.get("/3/movie/1").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "unavailable"
    assert sleeps == [1, 2, 4]


async def test_remote_protocol_error_unavailable(tmdb, tmdb_mock, sleeps):
    tmdb_mock.get("/3/movie/1").mock(side_effect=httpx.RemoteProtocolError("boom"))
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "unavailable"


async def test_timeout_translation(tmdb, tmdb_mock, sleeps):
    tmdb_mock.get("/3/movie/1").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "timeout"


async def test_invalid_json_bad_response(tmdb, tmdb_mock):
    tmdb_mock.get("/3/movie/1").respond(200, content=b"<html>not json</html>")
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "bad_response"


async def test_non_object_json_bad_response(tmdb, tmdb_mock):
    tmdb_mock.get("/3/movie/1").respond(200, json=[1, 2])
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "bad_response"


async def test_unexpected_exception_bad_response(tmdb, tmdb_mock):
    tmdb_mock.get("/3/movie/1").mock(side_effect=ValueError("weird"))
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "bad_response"


async def test_final_429_rate_limited_with_header(tmdb, tmdb_mock, sleeps):
    route = tmdb_mock.get("/3/movie/1").respond(429, headers={"Retry-After": "3"})
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "rate_limited"
    assert ei.value.retry_after == 3
    assert ei.value.status_code == 429
    assert route.call_count == 4
    assert sleeps == [3, 3, 3]


async def test_final_429_without_header_defaults_30(tmdb, tmdb_mock, sleeps):
    tmdb_mock.get("/3/movie/1").respond(429)
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert ei.value.retry_after == 30
    assert sleeps == [1, 2, 4]  # default delay, as _request


async def test_429_http_date_retry_after_never_value_error(tmdb, tmdb_mock, sleeps):
    date = "Wed, 21 Oct 2026 07:28:00 GMT"
    tmdb_mock.get("/3/movie/1").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": date}),
        httpx.Response(200, json={"id": 1}),
    ])
    assert await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline()) == {"id": 1}
    assert sleeps == [1]  # default delay of the attempt, not the date

    tmdb_mock.get("/3/movie/2").respond(429, headers={"Retry-After": date})
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(2, language="fr-FR", deadline=_deadline())
    assert ei.value.kind == "rate_limited"
    assert ei.value.retry_after == 30


async def test_long_retry_after_fails_immediately(tmdb, tmdb_mock, sleeps):
    route = tmdb_mock.get("/3/movie/1").respond(429, headers={"Retry-After": "120"})
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline(5))
    assert ei.value.kind == "rate_limited"
    assert ei.value.retry_after == 120
    assert route.call_count == 1
    assert sleeps == []


async def test_retry_delay_longer_than_remaining_is_timeout(tmdb, tmdb_mock, sleeps):
    route = tmdb_mock.get("/3/movie/1").respond(503)
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline(0.5))
    assert ei.value.kind == "timeout"
    assert route.call_count == 1
    assert sleeps == []


async def test_expired_deadline_no_request(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/movie/1").respond(200, json={})
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=time.monotonic() - 1)
    assert ei.value.kind == "timeout"
    assert not route.called


async def test_bounded_counts_into_tally_and_metric(tmdb, tmdb_mock, sleeps):
    from app.utils.metrics import tmdb_requests_total

    def val(kind, result):
        return tmdb_requests_total.labels(kind=kind, result=result)._value.get()

    tmdb_mock.get("/3/movie/1").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={"id": 1})])
    ok_before = val("details", "ok")
    with count_requests() as tally:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    assert tally.count == 2
    assert tmdb.get_request_count() == 2
    assert val("details", "ok") == ok_before + 1


# --------------------------------------------------------------------------
# search_scored / search_candidates_raw
# --------------------------------------------------------------------------

async def test_search_scored_matched_single_call(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/search/movie").respond(200, json={"results": [
        _movie(603, "The Matrix", "1999-03-31", vote=900),
        _movie(604, "The Matrix Reloaded", "2003-05-15"),
    ]})
    out = await tmdb.search_scored("movie", "The Matrix", 1999, language="fr-FR", deadline=_deadline())
    assert route.call_count == 1
    assert out.verdict == "matched"
    assert out.best_tmdb_id == 603
    assert out.confidence >= 0.85
    c = out.candidates[0]
    assert (c.tmdb_id, c.kind, c.title, c.year, c.poster_path) == (603, "movie", "The Matrix", 1999, "/p603.jpg")
    assert c.original_title == "The Matrix"
    q = route.calls.last.request.url.params
    assert q["language"] == "fr-FR" and q["year"] == "1999"


async def test_search_scored_retries_without_year(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/search/movie").mock(side_effect=[
        httpx.Response(200, json={"results": []}),
        httpx.Response(200, json={"results": [_movie(603, "The Matrix", "1999-03-31")]}),
    ])
    out = await tmdb.search_scored("movie", "The Matrix", 2005, language="en-US", deadline=_deadline())
    assert route.call_count == 2
    assert "year" in route.calls[0].request.url.params
    assert "year" not in route.calls[1].request.url.params
    assert out.verdict == "matched" and out.best_tmdb_id == 603


async def test_search_scored_no_year_no_second_call(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/search/tv").respond(200, json={"results": []})
    out = await tmdb.search_scored("show", "Nothing", None, language="en-US", deadline=_deadline())
    assert route.call_count == 1
    assert out.verdict == "nomatch" and out.best_tmdb_id is None and out.candidates == []


async def test_search_scored_show_uses_tv_keys(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/search/tv").respond(200, json={"results": [
        {"id": 1396, "name": "Breaking Bad", "original_name": "Breaking Bad",
         "first_air_date": "2008-01-20", "vote_count": 5, "overview": "o", "poster_path": "/bb.jpg"},
    ]})
    out = await tmdb.search_scored("show", "Breaking Bad", 2008, language="en-US", deadline=_deadline())
    assert out.candidates[0].kind == "show" and out.candidates[0].year == 2008
    assert route.calls.last.request.url.params["first_air_date_year"] == "2008"


async def test_candidates_sorted_score_desc_then_id_asc_after_verdict(tmdb, tmdb_mock):
    # Same title+year => identical score; the verdict (via _score_results) is
    # ordered by vote_count, the exposed list MUST be tmdb_id asc on ties.
    tmdb_mock.get("/3/search/movie").respond(200, json={"results": [
        _movie(50, "Dune", "2021-09-15", vote=10),
        _movie(30, "Dune", "2021-09-15", vote=500),
        _movie(40, "Dune", "2021-09-15", vote=100),
        _movie(10, "Dune Part Two", "2024-02-27", vote=1),
    ]})
    out = await tmdb.search_scored("movie", "Dune", 2021, language="en-US", deadline=_deadline())
    assert out.verdict == "ambiguous"
    assert out.best_tmdb_id == 30  # verdict keeps the vote_count tie-break
    ids = [c.tmdb_id for c in out.candidates]
    assert ids[:3] == [30, 40, 50]  # equal scores -> id asc
    assert ids[3] == 10
    scores = [c.score for c in out.candidates]
    assert scores == sorted(scores, reverse=True)


async def test_candidates_capped_at_10(tmdb, tmdb_mock):
    tmdb_mock.get("/3/search/movie").respond(200, json={"results": [
        _movie(i, f"Film {i}", "2000-01-01") for i in range(1, 16)
    ]})
    out = await tmdb.search_candidates_raw("movie", "Film", 2000, language="en-US", deadline=_deadline())
    assert len(out.candidates) == 10


async def test_search_candidates_raw_single_call_even_on_nomatch(tmdb, tmdb_mock):
    route = tmdb_mock.get("/3/search/movie").respond(200, json={"results": [_movie(1, "Zzz", "1990-01-01")]})
    out = await tmdb.search_candidates_raw("movie", "Aaa", 2020, language="en-US", deadline=_deadline())
    assert route.call_count == 1
    assert out.verdict == "nomatch"


async def test_search_error_propagates_typed(tmdb, tmdb_mock):
    tmdb_mock.get("/3/search/movie").respond(500)
    with pytest.raises(ProviderError) as ei:
        await tmdb.search_scored("movie", "x", 2000, language="en-US", deadline=_deadline())
    assert ei.value.kind == "bad_response"


# --------------------------------------------------------------------------
# certification region
# --------------------------------------------------------------------------

_RELEASES = {"release_dates": {"results": [
    {"iso_3166_1": "FR", "release_dates": [{"type": 3, "certification": "12"}]},
    {"iso_3166_1": "US", "release_dates": [{"type": 3, "certification": "PG-13"}]},
    {"iso_3166_1": "DE", "release_dates": [{"type": 3, "certification": "16"}]},
]}}
_RATINGS = {"content_ratings": {"results": [
    {"iso_3166_1": "FR", "rating": "-12"},
    {"iso_3166_1": "US", "rating": "TV-MA"},
]}}


def test_certification_default_follows_tmdb_language(monkeypatch):
    monkeypatch.setattr(tmdb_mod.settings, "TMDB_LANGUAGE", "fr-FR")
    assert _parse_movie_certification(_RELEASES) == "12"
    assert _parse_tv_certification(_RATINGS) == "-12"
    monkeypatch.setattr(tmdb_mod.settings, "TMDB_LANGUAGE", "en-US")
    assert _parse_movie_certification(_RELEASES) == "PG-13"
    assert _parse_tv_certification(_RATINGS) == "TV-MA"


def test_certification_explicit_region_overrides(monkeypatch):
    monkeypatch.setattr(tmdb_mod.settings, "TMDB_LANGUAGE", "fr-FR")
    assert _parse_movie_certification(_RELEASES, region="DE") == "16"
    assert _parse_movie_certification(_RELEASES, region="us") == "PG-13"
    assert _parse_tv_certification(_RATINGS, region="US") == "TV-MA"
    # Unknown region -> US fallback, same as before.
    assert _parse_movie_certification(_RELEASES, region="JP") == "PG-13"


# --------------------------------------------------------------------------
# OMDb fetch_ratings
# --------------------------------------------------------------------------

def _omdb_ok(**over):
    base = {
        "Response": "True", "imdbRating": "8.7", "imdbVotes": "2,100,000",
        "Metascore": "73",
        "Ratings": [
            {"Source": "Internet Movie Database", "Value": "8.7/10"},
            {"Source": "Rotten Tomatoes", "Value": "83%"},
            {"Source": "Metacritic", "Value": "73/100"},
        ],
    }
    base.update(over)
    return base


async def test_fetch_ratings_parses_all(omdb, omdb_mock):
    route = omdb_mock.get("/").respond(200, json=_omdb_ok())
    out = await omdb.fetch_ratings("tt0133093", deadline=_deadline())
    assert out == OmdbRatings(8.7, 2100000, 83, 73)
    assert route.calls.last.request.url.params["i"] == "tt0133093"
    assert omdb.get_request_count() == 1


async def test_fetch_ratings_metascore_fallback_and_na(omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json=_omdb_ok(
        Metascore="61", imdbRating="N/A", imdbVotes="N/A",
        Ratings=[{"Source": "Rotten Tomatoes", "Value": "N/A"}],
    ))
    out = await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert out == OmdbRatings(None, None, None, 61)


async def test_fetch_ratings_all_absent(omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json={"Response": "True", "Metascore": "N/A"})
    out = await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert out == OmdbRatings(None, None, None, None)


async def test_fetch_ratings_out_of_range_dropped(omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json=_omdb_ok(
        Ratings=[{"Source": "Rotten Tomatoes", "Value": "250%"}], Metascore="abc",
    ))
    out = await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert out.rotten_tomatoes is None and out.metacritic is None


@pytest.mark.parametrize("message", ["Incorrect IMDb ID.", "Movie not found!", "Error getting data."])
async def test_fetch_ratings_not_found_messages(omdb, omdb_mock, message):
    omdb_mock.get("/").respond(200, json={"Response": "False", "Error": message})
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "not_found" and ei.value.provider == "omdb"


@pytest.mark.parametrize("status", [200, 401])
async def test_fetch_ratings_request_limit_is_budget_exhausted(omdb, omdb_mock, status):
    omdb_mock.get("/").respond(status, json={"Response": "False", "Error": "Request limit reached!"})
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "budget_exhausted"


@pytest.mark.parametrize("status", [200, 401])
@pytest.mark.parametrize("message", ["Invalid API key!", "No API key provided."])
async def test_fetch_ratings_key_errors_not_configured(omdb, omdb_mock, message, status):
    omdb_mock.get("/").respond(status, json={"Response": "False", "Error": message})
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "not_configured"


async def test_fetch_ratings_unknown_error_bad_response(omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json={"Response": "False", "Error": "Something else"})
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "bad_response"


async def test_fetch_ratings_missing_response_field_bad_response(omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json={"foo": 1})
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "bad_response"


async def test_fetch_ratings_no_key_no_call(monkeypatch, omdb_mock):
    monkeypatch.setattr(omdb_mod.settings, "OMDB_API_KEY", "")
    route = omdb_mock.get("/").respond(200, json={})
    svc = OMDbService()
    with pytest.raises(ProviderError) as ei:
        await svc.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "not_configured"
    assert not route.called
    await svc.close()


async def test_fetch_ratings_budget_exhausted_no_call(monkeypatch, omdb, omdb_mock):
    monkeypatch.setattr(omdb_mod.settings, "OMDB_DAILY_LIMIT", 5)
    omdb.real_request_count = 5
    route = omdb_mock.get("/").respond(200, json=_omdb_ok())
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "budget_exhausted"
    assert not route.called


@pytest.mark.parametrize("status,kind", [
    (403, "not_configured"), (404, "not_found"), (500, "bad_response"),
])
async def test_fetch_ratings_http_status_translation(omdb, omdb_mock, status, kind):
    omdb_mock.get("/").respond(status)
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == kind


async def test_fetch_ratings_401_without_json_not_configured(omdb, omdb_mock):
    omdb_mock.get("/").respond(401, content=b"nope")
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "not_configured"


async def test_fetch_ratings_transport_errors(omdb, omdb_mock, sleeps):
    omdb_mock.get("/").mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "unavailable"
    assert omdb.get_request_count() == 4

    omdb_mock.get("/").mock(side_effect=httpx.ReadTimeout("x"))
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert ei.value.kind == "timeout"


async def test_fetch_ratings_long_retry_after_immediate(omdb, omdb_mock, sleeps):
    route = omdb_mock.get("/").respond(429, headers={"Retry-After": "90"})
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline(5))
    assert ei.value.kind == "rate_limited" and ei.value.retry_after == 90
    assert route.call_count == 1 and sleeps == []


async def test_fetch_ratings_metric_and_tally(omdb, omdb_mock):
    from app.utils.metrics import omdb_requests_total

    def val(result):
        return omdb_requests_total.labels(result=result)._value.get()

    ok, nf = val("ok"), val("not_found")
    omdb_mock.get("/").respond(200, json=_omdb_ok())
    with omdb_mod.count_requests() as tally:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    assert tally.count == 1 and val("ok") == ok + 1
    omdb_mock.get("/").respond(200, json={"Response": "False", "Error": "Movie not found!"})
    with pytest.raises(ProviderError):
        await omdb.fetch_ratings("tt2", deadline=_deadline())
    assert val("not_found") == nf + 1


async def test_get_by_imdb_id_unchanged(omdb, omdb_mock):
    omdb_mock.get("/").respond(200, json={"Response": "False", "Error": "Movie not found!"})
    assert await omdb.get_by_imdb_id("tt1") is None


# --------------------------------------------------------------------------
# No secret leak
# --------------------------------------------------------------------------

def _assert_no_leak(err: ProviderError, logs) -> None:
    assert CANARY not in str(err)
    assert CANARY not in repr(err)
    assert err.__cause__ is None
    assert err.__context__ is None
    for text in (str(a) for a in err.args):
        assert CANARY not in text
    assert CANARY not in logs.text
    for rec in logs.records:
        assert CANARY not in rec.getMessage()
        assert CANARY not in str(rec.args)
        if rec.exc_info:
            assert CANARY not in repr(rec.exc_info[1])


@pytest.mark.parametrize("responder", [
    lambda r: r.respond(500),
    lambda r: r.respond(503),
    lambda r: r.respond(404),
    lambda r: r.respond(401),
    lambda r: r.respond(429, headers={"Retry-After": "2"}),
    lambda r: r.respond(200, content=b"not json"),
    lambda r: r.mock(side_effect=httpx.ConnectError("conn")),
    lambda r: r.mock(side_effect=httpx.ReadTimeout("slow")),
    lambda r: r.mock(side_effect=ValueError("weird")),
])
async def test_tmdb_error_never_leaks_key(tmdb, tmdb_mock, sleeps, captured_logs, responder):
    responder(tmdb_mock.get("/3/movie/1"))
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline())
    _assert_no_leak(ei.value, captured_logs)


async def test_tmdb_deadline_errors_never_leak_key(tmdb, tmdb_mock, sleeps, captured_logs):
    tmdb_mock.get("/3/movie/1").respond(429, headers={"Retry-After": "500"})
    with pytest.raises(ProviderError) as ei:
        await tmdb.fetch_movie(1, language="fr-FR", deadline=_deadline(5))
    _assert_no_leak(ei.value, captured_logs)


@pytest.mark.parametrize("responder", [
    lambda r: r.respond(500),
    lambda r: r.respond(401, json={"Response": "False", "Error": "Invalid API key!"}),
    lambda r: r.mock(side_effect=httpx.ConnectError("conn")),
    lambda r: r.mock(side_effect=ValueError("weird")),
])
async def test_omdb_error_never_leaks_key(omdb, omdb_mock, sleeps, captured_logs, responder):
    responder(omdb_mock.get("/"))
    with pytest.raises(ProviderError) as ei:
        await omdb.fetch_ratings("tt1", deadline=_deadline())
    _assert_no_leak(ei.value, captured_logs)
