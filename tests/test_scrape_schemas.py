"""B1-09 — Pydantic models of the scraping gateway (`app/models/scrape_schemas.py`)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.models import scrape_schemas as S

ENVELOPE = {
    "schemaVersion": 1, "cacheKey": "1|fr-FR|movie|603", "language": "fr-FR",
    "fetchedAt": 1760745600000, "expiresAt": 1763337600000, "cache": "hit",
}
IMAGES = {"poster": None, "backdrop": None, "posters": [], "backdrops": [], "logos": []}
MOVIE = {
    "tmdbId": 603, "title": "Matrix", "originalTitle": None, "overview": None, "tagline": None,
    "releaseDate": "1999-03-31", "year": 1999, "runtimeMinutes": 136, "status": None,
    "contentRating": None, "genres": [], "studios": [], "countries": [], "collection": None,
    "externalIds": {"imdb": "tt0133093", "tvdb": None, "wikidata": None},
    "ratings": [], "omdbStatus": "ok", "cast": [], "crew": [], "trailer": None,
    "images": IMAGES, "imageBaseUrl": "https://image.tmdb.org/t/p/",
}
SHOW = {
    "tmdbId": 1396, "title": "Breaking Bad", "originalTitle": None, "overview": None,
    "tagline": None, "firstAirDate": "2008-01-20", "year": 2008, "episodeRuntimeMinutes": 45,
    "status": None, "contentRating": None, "genres": [], "studios": [], "countries": [],
    "externalIds": {"imdb": None, "tvdb": "81189", "wikidata": None}, "ratings": [],
    "omdbStatus": "not_configured", "cast": [], "crew": [], "trailer": None, "images": IMAGES,
    "imageBaseUrl": "https://image.tmdb.org/t/p/", "numberOfSeasons": 5,
    "numberOfEpisodes": 62, "seasons": [],
}


# --- Conventions ------------------------------------------------------------

ALL_MODELS = [
    S.Rating, S.CastMember, S.CrewMember, S.Candidate, S.Images, S.ExternalIds,
    S.SeasonExternalIds, S.Collection, S.Trailer, S.SeasonSummary, S.MovieDetails,
    S.ShowDetails, S.EpisodeDetails, S.SeasonDetails, S.FindMatch, S.MatchResponse,
    S.SearchResponse, S.MovieResponse, S.ShowResponse, S.SeasonResponse, S.FindResponse,
    S.FamilyStats, S.TtlInfo, S.SinceStart, S.CacheStatsResponse, S.InvalidateRequest,
    S.InvalidateResponse, S.PurgeRequest, S.PurgeResponse, S.ErrorDetail, S.ErrorResponse,
]


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_every_model_is_camelcase_and_forbids_extra(model):
    config = model.model_config
    assert config["extra"] == "forbid"
    assert config["populate_by_name"] is True
    for name, field in model.model_fields.items():
        alias = field.alias
        assert alias is not None
        # camelCase: no underscore, first letter lower-case
        assert "_" not in alias and alias[0].islower(), f"{model.__name__}.{name} -> {alias}"


@pytest.mark.parametrize("model, base", [
    (S.MovieDetails, MOVIE), (S.ShowDetails, SHOW),
    (S.Rating, {"source": "tmdb", "value": 8.2, "max": 10, "votes": 5}),
    (S.Images, IMAGES), (S.PurgeRequest, {"limit": 5}), (S.InvalidateRequest, {}),
])
def test_unknown_key_is_rejected(model, base):
    with pytest.raises(ValidationError):
        model.model_validate({**base, "unexpected": 1})


def test_population_by_name_and_by_alias_both_work():
    by_name = S.CastMember(name="Neo", order=0, tmdb_person_id=6384, profile_path="/a.jpg")
    by_alias = S.CastMember.model_validate(
        {"name": "Neo", "order": 0, "tmdbPersonId": 6384, "profilePath": "/a.jpg"})
    assert by_name == by_alias
    dumped = by_name.model_dump(by_alias=True)
    assert dumped == {"name": "Neo", "character": None, "order": 0,
                      "profilePath": "/a.jpg", "tmdbPersonId": 6384}


# --- Building blocks --------------------------------------------------------

def test_rating_sources_and_ranges():
    for source in ("tmdb", "imdb", "rottenTomatoes", "metacritic"):
        S.Rating(source=source, value=1, max=10, votes=None)
    with pytest.raises(ValidationError):
        S.Rating(source="allocine", value=1, max=10, votes=None)
    with pytest.raises(ValidationError):
        S.Rating(source="tmdb", value=-1, max=10, votes=None)
    with pytest.raises(ValidationError):
        S.Rating(source="tmdb", value=1, max=0, votes=None)


def test_image_and_list_bounds_are_enforced():
    S.Images(posters=["/a"] * 20, backdrops=["/a"] * 20, logos=["/a"] * 10)
    for field, size in (("posters", 21), ("backdrops", 21), ("logos", 11)):
        with pytest.raises(ValidationError):
            S.Images(**{field: ["/a"] * size})
    person = {"name": "n", "order": 0, "tmdbPersonId": 1}
    crew = {"name": "n", "job": "Director", "tmdbPersonId": 1}
    S.MovieDetails.model_validate({**MOVIE, "cast": [person] * 30, "crew": [crew] * 30})
    with pytest.raises(ValidationError):
        S.MovieDetails.model_validate({**MOVIE, "cast": [person] * 31})
    with pytest.raises(ValidationError):
        S.MovieDetails.model_validate({**MOVIE, "crew": [crew] * 31})
    with pytest.raises(ValidationError):
        S.MovieDetails.model_validate({**MOVIE, "studios": ["a"] * 6})


def test_trailer_site_is_youtube_only():
    S.Trailer(site="YouTube", key="abc")
    with pytest.raises(ValidationError):
        S.Trailer(site="Vimeo", key="abc")


def test_candidate_score_is_a_unit_interval_and_kind_is_closed():
    base = {"tmdbId": 1, "mediaType": "movie", "title": "T", "score": 0.5}
    S.Candidate.model_validate(base)
    for bad in ({"score": 1.01}, {"score": -0.01}, {"mediaType": "episode"}):
        with pytest.raises(ValidationError):
            S.Candidate.model_validate({**base, **bad})


def test_movie_and_show_require_omdb_status_and_a_known_value():
    movie = {k: v for k, v in MOVIE.items() if k != "omdbStatus"}
    with pytest.raises(ValidationError):
        S.MovieDetails.model_validate(movie)
    for status in ("ok", "not_configured", "budget_exhausted", "not_found", "error", "no_imdb_id"):
        S.MovieDetails.model_validate({**MOVIE, "omdbStatus": status})
    with pytest.raises(ValidationError):
        S.MovieDetails.model_validate({**MOVIE, "omdbStatus": "timeout"})


def test_episode_number_starts_at_one_and_episodes_are_capped():
    episode = {"tmdbId": 1, "episodeNumber": 1, "seasonNumber": 1}
    S.EpisodeDetails.model_validate(episode)
    with pytest.raises(ValidationError):
        S.EpisodeDetails.model_validate({**episode, "episodeNumber": 0})
    season = {"showTmdbId": 1, "tmdbId": 2, "seasonNumber": 1, "externalIds": {"tvdb": None},
              "images": IMAGES, "imageBaseUrl": "https://image.tmdb.org/t/p/"}
    eps = [{**episode, "episodeNumber": n} for n in range(1, 502)]
    S.SeasonDetails.model_validate({**season, "episodes": eps[:500]})
    with pytest.raises(ValidationError):
        S.SeasonDetails.model_validate({**season, "episodes": eps})
    with pytest.raises(ValidationError):
        S.EpisodeDetails.model_validate({**episode, "crew": [
            {"name": "n", "job": "Writer", "tmdbPersonId": 1}] * 11})
    with pytest.raises(ValidationError):
        S.EpisodeDetails.model_validate({**episode, "guestStars": [
            {"name": "n", "order": 0, "tmdbPersonId": 1}] * 21})


def test_season_external_ids_only_carry_tvdb():
    S.SeasonExternalIds.model_validate({"tvdb": "30272"})
    with pytest.raises(ValidationError):
        S.SeasonExternalIds.model_validate({"tvdb": "1", "imdb": "tt0133093"})


# --- Envelope and responses -------------------------------------------------

def test_match_response_matched_and_negative_forms():
    matched = S.MatchResponse.model_validate({
        **ENVELOPE, "cacheKey": "1|fr-FR|movie|matrix|1999", "result": "matched",
        "confidence": 0.97, "tmdbId": 603, "mediaType": "movie", "candidates": [],
        "details": MOVIE})
    assert isinstance(matched.details, S.MovieDetails)
    as_show = S.MatchResponse.model_validate({
        **ENVELOPE, "result": "matched", "confidence": 0.9, "tmdbId": 1396,
        "mediaType": "show", "candidates": [], "details": SHOW})
    assert isinstance(as_show.details, S.ShowDetails)
    nomatch = S.MatchResponse.model_validate({
        **ENVELOPE, "result": "nomatch", "confidence": None, "tmdbId": None,
        "mediaType": "movie", "candidates": [], "details": None})
    assert nomatch.details is None and nomatch.tmdb_id is None
    dumped = matched.model_dump(by_alias=True)
    assert {"schemaVersion", "cacheKey", "language", "fetchedAt", "expiresAt", "cache",
            "result", "confidence", "tmdbId", "mediaType", "candidates", "details"} <= set(dumped)


def test_match_response_result_and_confidence_are_closed():
    base = {**ENVELOPE, "result": "matched", "confidence": 0.5, "mediaType": "movie"}
    S.MatchResponse.model_validate(base)
    for bad in ({"result": "found"}, {"confidence": 1.5}, {"cache": "stale"},
                {"mediaType": "tv"}):
        with pytest.raises(ValidationError):
            S.MatchResponse.model_validate({**base, **bad})
    with pytest.raises(ValidationError):
        S.MatchResponse.model_validate({**base, "candidates": [
            {"tmdbId": i, "mediaType": "movie", "title": "t", "score": 0.1} for i in range(11)]})


def test_search_response_has_null_cache_key():
    resp = S.SearchResponse.model_validate({
        **ENVELOPE, "cacheKey": None, "expiresAt": ENVELOPE["fetchedAt"], "cache": "miss",
        "result": "not_found", "candidates": []})
    assert resp.cache_key is None
    assert resp.model_dump(by_alias=True)["cacheKey"] is None


@pytest.mark.parametrize("model, body", [
    (S.MovieResponse, {"result": "found", "details": MOVIE}),
    (S.ShowResponse, {"result": "found", "details": SHOW}),
    (S.MovieResponse, {"result": "not_found", "details": None}),
    (S.SeasonResponse, {"result": "not_found", "details": None}),
])
def test_details_responses(model, body):
    resp = model.model_validate({**ENVELOPE, **body})
    assert resp.model_dump(by_alias=True)["result"] == body["result"]
    with pytest.raises(ValidationError):
        model.model_validate({**ENVELOPE, **body, "result": "matched"})


def test_find_response_bounds():
    match = {"mediaType": "movie", "tmdbId": 603}
    resp = S.FindResponse.model_validate({
        **ENVELOPE, "cacheKey": "1|-|find|imdb|tt0133093", "language": "-",
        "result": "found", "matches": [match]})
    assert resp.matches[0].show_tmdb_id is None
    with pytest.raises(ValidationError):
        S.FindResponse.model_validate({**ENVELOPE, "result": "found", "matches": [match] * 6})
    with pytest.raises(ValidationError):
        S.FindResponse.model_validate({**ENVELOPE, "result": "found",
                                       "matches": [{"mediaType": "season", "tmdbId": 1}]})


# --- Cache administration ---------------------------------------------------

STATS = {
    "generatedAt": 1760745600000,
    "families": [{"family": "match", "keyVersion": 1, "entries": 1234, "positive": 1050,
                  "ambiguous": 50, "negative": 134, "expired": 40,
                  "oldestFetchedAt": 1758000000000, "newestFetchedAt": 1760740000000}],
    "ttl": {"successDays": 30, "negativeDays": 3},
    "sinceStart": {"hits": 120, "misses": 30, "coalesced": 4, "upstreamErrors": 1},
}


def test_cache_stats_contract_example_validates_and_roundtrips():
    resp = S.CacheStatsResponse.model_validate(STATS)
    assert resp.model_dump(by_alias=True) == STATS


def test_stats_entries_is_positive_plus_ambiguous_plus_negative():
    family = STATS["families"][0]
    for bad in ({"entries": 1235}, {"entries": 1233}):
        with pytest.raises(ValidationError):
            S.FamilyStats.model_validate({**family, **bad})
    # `expired` is a subset counted in addition, never a fourth class.
    S.FamilyStats.model_validate({**family, "expired": 1234})
    with pytest.raises(ValidationError):
        S.FamilyStats.model_validate({**family, "expired": 1235})
    S.FamilyStats.model_validate({
        "family": "omdb", "keyVersion": 1, "entries": 0, "positive": 0, "ambiguous": 0,
        "negative": 0, "expired": 0, "oldestFetchedAt": None, "newestFetchedAt": None})
    with pytest.raises(ValidationError):
        S.FamilyStats.model_validate({**family, "family": "tmdb_scrape_cache"})


def test_invalidate_request_fields_are_all_optional_at_model_level():
    """Exclusivity (`keys` XOR `family`+`tmdbId`) is a 400 of the service."""
    assert S.InvalidateRequest.model_validate({}).keys is None
    both = S.InvalidateRequest.model_validate(
        {"keys": ["1|fr-FR|movie|603"], "family": "movie", "tmdbId": 603})
    assert both.keys == ["1|fr-FR|movie|603"] and both.tmdb_id == 603
    only_family = S.InvalidateRequest.model_validate({"family": "season"})
    assert only_family.tmdb_id is None
    # Malformed key strings are NOT a validation error either (service -> 400).
    S.InvalidateRequest.model_validate({"keys": ["not a key"]})


def test_invalidate_request_bounds():
    S.InvalidateRequest.model_validate({"keys": ["k"] * 500})
    for bad in ({"keys": []}, {"keys": ["k"] * 501}, {"family": "omdb"},
                {"family": "movie", "tmdbId": 0}, {"family": "movie", "tmdbId": 1_000_000_000}):
        with pytest.raises(ValidationError):
            S.InvalidateRequest.model_validate(bad)
    S.InvalidateRequest.model_validate({"family": "show", "tmdbId": 999_999_999})


def test_purge_request_limit_bounds_and_default():
    assert S.PurgeRequest().limit == 10_000
    assert S.PurgeRequest.model_validate({}).limit == 10_000
    assert S.PurgeRequest.model_validate({"limit": 1}).limit == 1
    assert S.PurgeRequest.model_validate({"limit": 100_000}).limit == 100_000
    for bad in (0, -1, 100_001):
        with pytest.raises(ValidationError):
            S.PurgeRequest.model_validate({"limit": bad})


def test_responses_of_invalidate_and_purge():
    assert S.InvalidateResponse(deleted=3).model_dump(by_alias=True) == {"deleted": 3}
    assert S.PurgeResponse(deleted=2, remaining=7).model_dump(by_alias=True) == {
        "deleted": 2, "remaining": 7}
    with pytest.raises(ValidationError):
        S.PurgeResponse(deleted=-1, remaining=0)


# --- Errors -----------------------------------------------------------------

@pytest.mark.parametrize("code", [
    "invalid_parameter", "invalid_api_key", "insufficient_scope", "rate_limited",
    "upstream_error", "upstream_unavailable", "provider_not_configured", "upstream_timeout",
])
def test_error_codes_of_the_contract(code):
    body = S.ErrorResponse.model_validate({"detail": {"code": code, "message": "m"}})
    assert body.detail.retry_after_seconds is None


def test_error_body_shape_camelcase_and_closed_codes():
    body = S.ErrorResponse.model_validate({"detail": {
        "code": "upstream_unavailable", "message": "TMDB injoignable", "retryAfterSeconds": 30}})
    assert body.model_dump(by_alias=True, exclude_none=True) == {"detail": {
        "code": "upstream_unavailable", "message": "TMDB injoignable", "retryAfterSeconds": 30}}
    for bad in ({"code": "teapot", "message": "m"}, {"code": "rate_limited"},
                {"code": "rate_limited", "message": "m", "retryAfterSeconds": -1},
                {"code": "rate_limited", "message": "m", "trace": "boom"}):
        with pytest.raises(ValidationError):
            S.ErrorDetail.model_validate(bad)


# --- OpenAPI / serialisation by alias ---------------------------------------

def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/match", response_model=S.MatchResponse, response_model_by_alias=True)
    def match():
        return S.MatchResponse.model_validate({
            **ENVELOPE, "result": "matched", "confidence": 0.9, "tmdbId": 603,
            "mediaType": "movie", "candidates": [], "details": MOVIE})

    @app.get("/stats", response_model=S.CacheStatsResponse, response_model_by_alias=True)
    def stats():
        return S.CacheStatsResponse.model_validate(STATS)

    @app.post("/invalidate", response_model=S.InvalidateResponse, response_model_by_alias=True,
              responses={400: {"model": S.ErrorResponse}, 429: {"model": S.ErrorResponse}})
    def invalidate(body: S.InvalidateRequest):
        return S.InvalidateResponse(deleted=len(body.keys or []))

    @app.post("/purge", response_model=S.PurgeResponse, response_model_by_alias=True)
    def purge(body: S.PurgeRequest | None = None):
        return S.PurgeResponse(deleted=(body or S.PurgeRequest()).limit, remaining=0)

    @app.get("/search", response_model=S.SearchResponse, response_model_by_alias=True)
    def search():
        return S.SearchResponse.model_validate({
            **ENVELOPE, "cacheKey": None, "result": "not_found", "candidates": []})

    return app


def test_models_appear_in_openapi_with_camelcase_properties():
    schemas = _app().openapi()["components"]["schemas"]
    for name in ("MatchResponse", "MovieDetails", "ShowDetails", "Candidate", "Rating",
                 "CacheStatsResponse", "InvalidateRequest", "InvalidateResponse",
                 "PurgeRequest", "PurgeResponse", "ErrorResponse", "ErrorDetail"):
        assert name in schemas, name
    assert {"schemaVersion", "cacheKey", "fetchedAt", "expiresAt", "tmdbId"} <= set(
        schemas["MatchResponse"]["properties"])
    assert "omdbStatus" in schemas["MovieDetails"]["properties"]
    assert "retryAfterSeconds" in schemas["ErrorDetail"]["properties"]
    assert schemas["PurgeRequest"]["properties"]["limit"]["maximum"] == 100000


def test_routes_serialise_by_alias_and_validate_request_bounds():
    client = TestClient(_app())
    body = client.get("/match").json()
    assert body["schemaVersion"] == 1 and body["details"]["omdbStatus"] == "ok"
    assert "schema_version" not in body
    assert client.get("/stats").json() == STATS
    assert client.get("/search").json()["cacheKey"] is None
    assert client.post("/purge", json={"limit": 100_001}).status_code == 422
    assert client.post("/purge", json={"limit": 50}).json() == {"deleted": 50, "remaining": 0}
    assert client.post("/purge").json() == {"deleted": 10_000, "remaining": 0}
    assert client.post("/invalidate", json={"keys": []}).status_code == 422
    assert client.post("/invalidate", json={"keys": ["a", "b"]}).json() == {"deleted": 2}
