"""TMDB `videos` parsing + retrieval (Lot A trailers).

Two layers:
  - `_select_youtube_trailer` (pure, no HTTP) — the same JSON shape
    `append_to_response=...,videos` and the dedicated `/videos` endpoint
    both produce (`{"results": [...]}`), so one parser covers both callers.
  - `TMDBService.get_movie_details`/`get_tv_details` (append_to_response
    widened) and the new `get_videos_trailer` (dedicated lean lookup used by
    `trailer_service`'s live repli path) — mocked via respx.
"""
from __future__ import annotations

import pytest_asyncio

from app.services.tmdb_service import TMDBService, _select_youtube_trailer


# ─── Pure parser ─────────────────────────────────────────────────────────


def _video(key: str, *, site="YouTube", type_="Trailer", official=True, lang="en"):
    return {
        "key": key, "site": site, "type": type_, "official": official,
        "iso_639_1": lang,
    }


class TestSelectYoutubeTrailer:
    def test_no_results_returns_none(self):
        assert _select_youtube_trailer({}) is None
        assert _select_youtube_trailer({"results": []}) is None

    def test_non_youtube_site_ignored(self):
        payload = {"results": [_video("v1", site="Vimeo")]}
        assert _select_youtube_trailer(payload) is None

    def test_non_trailer_type_ignored(self):
        payload = {"results": [_video("v1", type_="Teaser")]}
        assert _select_youtube_trailer(payload) is None

    def test_single_trailer_returned(self):
        payload = {"results": [_video("dQw4w9WgXcQ")]}
        assert _select_youtube_trailer(payload) == "dQw4w9WgXcQ"

    def test_official_preferred_over_non_official(self):
        payload = {"results": [
            _video("nonofficial", official=False, lang="fr"),
            _video("official", official=True, lang="en"),
        ]}
        assert _select_youtube_trailer(payload) == "official"

    def test_french_preferred_over_english_when_both_official(self):
        payload = {"results": [
            _video("en_key", official=True, lang="en"),
            _video("fr_key", official=True, lang="fr"),
        ]}
        assert _select_youtube_trailer(payload) == "fr_key"

    def test_english_preferred_over_other_language(self):
        payload = {"results": [
            _video("de_key", official=True, lang="de"),
            _video("en_key", official=True, lang="en"),
        ]}
        assert _select_youtube_trailer(payload) == "en_key"

    def test_official_wins_even_over_language_preference(self):
        """Plan: "official d'abord, fr puis en" — official ranks before
        language, so a non-official French trailer loses to an official
        English one."""
        payload = {"results": [
            _video("fr_nonofficial", official=False, lang="fr"),
            _video("en_official", official=True, lang="en"),
        ]}
        assert _select_youtube_trailer(payload) == "en_official"

    def test_entries_missing_a_key_are_skipped(self):
        payload = {"results": [
            {"site": "YouTube", "type": "Trailer", "official": True},  # no "key"
            _video("valid"),
        ]}
        assert _select_youtube_trailer(payload) == "valid"

    def test_non_dict_results_entry_ignored(self):
        payload = {"results": ["not-a-dict", _video("valid")]}
        assert _select_youtube_trailer(payload) == "valid"


# ─── TMDBService retrieval (mocked HTTP) ──────────────────────────────────


@pytest_asyncio.fixture
async def configured_tmdb(monkeypatch):
    from app.services import tmdb_service as mod

    monkeypatch.setattr(mod.settings, "TMDB_API_KEY", "test_key")
    monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")
    svc = TMDBService()
    try:
        yield svc
    finally:
        await svc.close()


class TestGetDetailsIncludesVideos:
    async def test_get_movie_details_requests_videos_with_language_filter(
        self, configured_tmdb, tmdb_mock,
    ):
        route = tmdb_mock.get("/3/movie/42").respond(200, json={
            "id": 42, "title": "T", "release_date": "2020-01-01",
            "videos": {"results": [_video("dQw4w9WgXcQ")]},
        })
        data = await configured_tmdb.get_movie_details(42)

        assert data.youtube_trailer == "dQw4w9WgXcQ"
        sent_params = dict(route.calls.last.request.url.params)
        assert "videos" in sent_params["append_to_response"]
        assert sent_params["include_video_language"] == "fr,en,null"

    async def test_get_tv_details_requests_videos_with_language_filter(
        self, configured_tmdb, tmdb_mock,
    ):
        route = tmdb_mock.get("/3/tv/7").respond(200, json={
            "id": 7, "name": "S", "first_air_date": "2020-01-01",
            "videos": {"results": [_video("abc12345678")]},
        })
        data = await configured_tmdb.get_tv_details(7)

        assert data.youtube_trailer == "abc12345678"
        sent_params = dict(route.calls.last.request.url.params)
        assert "videos" in sent_params["append_to_response"]
        assert sent_params["include_video_language"] == "fr,en,null"

    async def test_get_movie_details_no_trailer_in_videos(self, configured_tmdb, tmdb_mock):
        tmdb_mock.get("/3/movie/1").respond(200, json={
            "id": 1, "title": "T", "release_date": "2020-01-01",
            "videos": {"results": []},
        })
        data = await configured_tmdb.get_movie_details(1)
        assert data.youtube_trailer is None

    async def test_get_movie_details_missing_videos_key_defaults_none(
        self, configured_tmdb, tmdb_mock,
    ):
        """A TMDB response that omits `videos` entirely (older cached
        payload shape, or an item with genuinely no videos data) must not
        raise — `data.get("videos") or {}` covers this."""
        tmdb_mock.get("/3/movie/2").respond(200, json={
            "id": 2, "title": "T", "release_date": "2020-01-01",
        })
        data = await configured_tmdb.get_movie_details(2)
        assert data.youtube_trailer is None


class TestGetVideosTrailer:
    async def test_movie_kind_hits_movie_videos_endpoint(self, configured_tmdb, tmdb_mock):
        route = tmdb_mock.get("/3/movie/99/videos").respond(
            200, json={"results": [_video("dQw4w9WgXcQ")]},
        )
        result = await configured_tmdb.get_videos_trailer(99, "movie")
        assert result == "dQw4w9WgXcQ"
        sent_params = dict(route.calls.last.request.url.params)
        assert sent_params["include_video_language"] == "fr,en,null"

    async def test_tv_kind_hits_tv_videos_endpoint(self, configured_tmdb, tmdb_mock):
        tmdb_mock.get("/3/tv/5/videos").respond(
            200, json={"results": [_video("abc12345678")]},
        )
        result = await configured_tmdb.get_videos_trailer(5, "tv")
        assert result == "abc12345678"

    async def test_unconfigured_returns_none_without_http(self, monkeypatch, tmdb_mock):
        from app.services import tmdb_service as mod

        monkeypatch.setattr(mod.settings, "TMDB_API_KEY", "")
        svc = TMDBService()
        try:
            result = await svc.get_videos_trailer(1, "movie")
        finally:
            await svc.close()
        assert result is None
        assert len(tmdb_mock.calls) == 0

    async def test_request_failure_returns_none(self, configured_tmdb, tmdb_mock):
        tmdb_mock.get("/3/movie/3/videos").respond(500)
        result = await configured_tmdb.get_videos_trailer(3, "movie")
        assert result is None
