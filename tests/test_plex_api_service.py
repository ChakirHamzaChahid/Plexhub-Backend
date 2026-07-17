"""`app.services.plex_api_service` — pure HTTP client for plex.tv discovery
+ a Plex Media Server's REST API (PH-PLEX-02).

All HTTP is mocked via `respx` (`xtream_mock` fixture from `conftest.py` —
host-agnostic, registers full URLs, works for `plex.tv` and a fake PMS host
alike). A fresh `PlexApiService()` instance is used per test (never the
module singleton) to avoid client-state bleed across tests, mirroring
`test_tmdb_service_mocked.py`'s `configured_tmdb` fixture pattern.
"""
from __future__ import annotations

import logging

import httpx
import pytest
import pytest_asyncio

from app.services.plex_api_service import (
    PlexApiError,
    PlexApiService,
    best_media,
    parse_genres,
    parse_guids,
)

RESOURCES_URL = "https://plex.tv/api/v2/resources"
PMS = "http://192.168.1.50:32400"


@pytest_asyncio.fixture
async def svc(monkeypatch):
    from app.services import plex_api_service as mod

    monkeypatch.setattr(mod.settings, "PLEX_CLIENT_IDENTIFIER", "test-client-id")
    monkeypatch.setattr(mod.settings, "PLEX_PROBE_TIMEOUT", 5)
    instance = PlexApiService()
    try:
        yield instance
    finally:
        await instance.close()


def _resource(
    name="Living Room", client_id="abc123", owned=True, source_title=None,
    access_token="srv-token-1", provides="server", connections=None,
):
    return {
        "name": name,
        "clientIdentifier": client_id,
        "owned": owned,
        "sourceTitle": source_title,
        "accessToken": access_token,
        "provides": provides,
        "connections": connections
        if connections is not None
        else [
            {"protocol": "https", "address": "1.2.3.4", "port": 32400,
             "uri": "https://1-2-3-4.plex.direct:32400", "local": False, "relay": False},
        ],
    }


# ─── discover_servers ────────────────────────────────────────────────────


class TestDiscoverServers:
    async def test_filters_by_provides_server(self, svc, xtream_mock):
        xtream_mock.get(RESOURCES_URL).respond(
            200,
            json=[
                _resource(client_id="has-server", provides="server,player"),
                _resource(client_id="no-server", provides="player,camera"),
            ],
        )
        resources = await svc.discover_servers("acct-token")
        assert [r.client_identifier for r in resources] == ["has-server"]

    async def test_extracts_all_fields_and_connections(self, svc, xtream_mock):
        xtream_mock.get(RESOURCES_URL).respond(
            200,
            json=[
                _resource(
                    name="My Server", client_id="cid-1", owned=True,
                    source_title=None, access_token="tok-1",
                    connections=[
                        {"protocol": "https", "address": "10.0.0.5", "port": 32400,
                         "uri": "https://10-0-0-5.plex.direct:32400", "local": True, "relay": False},
                        {"protocol": "https", "address": "relay.plex.tv", "port": 443,
                         "uri": "https://relay.plex.tv:443", "local": False, "relay": True},
                    ],
                ),
            ],
        )
        [r] = await svc.discover_servers("acct-token")
        assert r.name == "My Server"
        assert r.client_identifier == "cid-1"
        assert r.owned is True
        assert r.owner_title is None
        assert r.access_token == "tok-1"
        assert len(r.connections) == 2
        assert r.connections[0].local is True and r.connections[0].relay is False
        assert r.connections[1].relay is True

    async def test_keeps_both_owned_and_shared_servers(self, svc, xtream_mock):
        xtream_mock.get(RESOURCES_URL).respond(
            200,
            json=[
                _resource(client_id="owned-1", owned=True, source_title=None),
                _resource(client_id="shared-1", owned=False, source_title="Friend Name"),
            ],
        )
        resources = await svc.discover_servers("acct-token")
        by_id = {r.client_identifier: r for r in resources}
        assert set(by_id) == {"owned-1", "shared-1"}
        assert by_id["owned-1"].owned is True
        assert by_id["shared-1"].owned is False
        assert by_id["shared-1"].owner_title == "Friend Name"

    async def test_sends_account_token_and_client_identifier_headers(self, svc, xtream_mock):
        seen = {}

        def _responder(request: httpx.Request) -> httpx.Response:
            seen["token"] = request.headers.get("X-Plex-Token")
            seen["client_id"] = request.headers.get("X-Plex-Client-Identifier")
            seen["accept"] = request.headers.get("Accept")
            return httpx.Response(200, json=[])

        xtream_mock.get(RESOURCES_URL).mock(side_effect=_responder)
        await svc.discover_servers("my-account-secret")
        assert seen["token"] == "my-account-secret"
        assert seen["client_id"] == "test-client-id"
        assert seen["accept"] == "application/json"

    async def test_non_list_response_returns_empty(self, svc, xtream_mock):
        xtream_mock.get(RESOURCES_URL).respond(200, json={"unexpected": "shape"})
        assert await svc.discover_servers("tok") == []


# ─── probe ────────────────────────────────────────────────────────────────


class TestProbe:
    async def test_200_with_media_container_returns_true(self, svc, xtream_mock):
        xtream_mock.get(f"{PMS}/library/sections").respond(
            200, json={"MediaContainer": {"size": 0}},
        )
        assert await svc.probe(PMS, "srv-token") is True

    async def test_non_200_returns_false(self, svc, xtream_mock):
        xtream_mock.get(f"{PMS}/library/sections").respond(401)
        assert await svc.probe(PMS, "srv-token") is False

    async def test_non_json_body_returns_false(self, svc, xtream_mock):
        xtream_mock.get(f"{PMS}/library/sections").respond(200, content=b"<xml/>")
        assert await svc.probe(PMS, "srv-token") is False

    async def test_timeout_returns_false_without_raising(self, svc, xtream_mock):
        xtream_mock.get(f"{PMS}/library/sections").mock(side_effect=httpx.ConnectTimeout("timed out"))
        assert await svc.probe(PMS, "srv-token") is False

    async def test_empty_uri_returns_false(self, svc):
        assert await svc.probe("", "srv-token") is False


# ─── list_sections ──────────────────────────────────────────────────────


class TestListSections:
    async def test_filters_to_movie_and_show(self, svc, xtream_mock):
        xtream_mock.get(f"{PMS}/library/sections").respond(
            200,
            json={"MediaContainer": {"Directory": [
                {"key": "1", "title": "Movies", "type": "movie"},
                {"key": "2", "title": "TV Shows", "type": "show"},
                {"key": "3", "title": "Music", "type": "artist"},
            ]}},
        )
        sections = await svc.list_sections(PMS, "srv-token")
        assert {s["type"] for s in sections} == {"movie", "show"}
        assert {s["key"] for s in sections} == {"1", "2"}


# ─── list_section_items / list_children pagination ─────────────────────


class TestPagination:
    async def test_list_section_items_paginates_until_total_size(self, svc, xtream_mock):
        url = f"{PMS}/library/sections/1/all"
        starts_seen = []

        def _responder(request: httpx.Request) -> httpx.Response:
            start = int(request.headers["X-Plex-Container-Start"])
            starts_seen.append(start)
            if start == 0:
                page = [{"ratingKey": "1"}, {"ratingKey": "2"}]
            else:
                page = [{"ratingKey": "3"}]
            return httpx.Response(
                200, json={"MediaContainer": {"totalSize": 3, "size": len(page), "Metadata": page}},
            )

        xtream_mock.get(url).mock(side_effect=_responder)
        items = await svc.list_section_items(PMS, "srv-token", "1")
        assert [i["ratingKey"] for i in items] == ["1", "2", "3"]
        assert starts_seen == [0, 2]

    async def test_list_children_paginates(self, svc, xtream_mock):
        url = f"{PMS}/library/metadata/999/children"

        def _responder(request: httpx.Request) -> httpx.Response:
            start = int(request.headers["X-Plex-Container-Start"])
            page = [{"ratingKey": "s1"}] if start == 0 else []
            return httpx.Response(
                200, json={"MediaContainer": {"totalSize": 1, "size": len(page), "Metadata": page}},
            )

        xtream_mock.get(url).mock(side_effect=_responder)
        items = await svc.list_children(PMS, "srv-token", "999")
        assert [i["ratingKey"] for i in items] == ["s1"]

    async def test_single_page_no_total_size_stops_after_one_request(self, svc, xtream_mock):
        url = f"{PMS}/library/sections/1/all"
        route = xtream_mock.get(url).respond(
            200, json={"MediaContainer": {"Metadata": [{"ratingKey": "x"}]}},
        )
        items = await svc.list_section_items(PMS, "srv-token", "1")
        assert [i["ratingKey"] for i in items] == ["x"]
        assert route.call_count == 1


# ─── retry backoff ───────────────────────────────────────────────────────


class TestRetry:
    async def test_retries_on_429_honoring_retry_after(self, svc, xtream_mock, monkeypatch):
        from app.services import plex_api_service as mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

        route = xtream_mock.get(f"{PMS}/library/sections")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"MediaContainer": {"Directory": []}}),
        ]
        sections = await svc.list_sections(PMS, "srv-token")
        assert sections == []
        assert route.call_count == 2

    async def test_retries_on_5xx_then_succeeds(self, svc, xtream_mock, monkeypatch):
        from app.services import plex_api_service as mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

        route = xtream_mock.get(f"{PMS}/library/sections")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, json={"MediaContainer": {"Directory": []}}),
        ]
        sections = await svc.list_sections(PMS, "srv-token")
        assert sections == []
        assert route.call_count == 2

    async def test_exhausted_retries_raise_plexapierror(self, svc, xtream_mock, monkeypatch):
        from app.services import plex_api_service as mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

        xtream_mock.get(f"{PMS}/library/sections").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(PlexApiError):
            await svc.list_sections(PMS, "srv-token")

    async def test_non_retryable_4xx_raises_immediately(self, svc, xtream_mock):
        route = xtream_mock.get(f"{PMS}/library/sections").respond(404)
        with pytest.raises(PlexApiError):
            await svc.list_sections(PMS, "srv-token")
        assert route.call_count == 1


# ─── Secrets never logged / never in exception messages ─────────────────


class TestSecretsNeverLeak:
    async def test_token_and_url_never_appear_in_logs_or_exception_on_error(
        self, svc, xtream_mock, monkeypatch, caplog,
    ):
        from app.services import plex_api_service as mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

        secret_token = "SUPER-SECRET-PLEX-TOKEN-XYZ"  # noqa: S105 (test fixture value)
        xtream_mock.get(RESOURCES_URL).mock(
            side_effect=httpx.ConnectError(f"connect failed with token={secret_token}")
        )

        caplog.set_level(logging.DEBUG, logger="plexhub.plex_api")
        with pytest.raises(PlexApiError) as excinfo:
            await svc.discover_servers(secret_token)

        assert secret_token not in str(excinfo.value)
        assert excinfo.value.__cause__ is None  # `raise ... from None`
        for record in caplog.records:
            assert secret_token not in record.getMessage()
            assert secret_token not in caplog.text

    async def test_probe_failure_never_logs_token(self, svc, xtream_mock, caplog):
        secret_token = "PROBE-SECRET-TOKEN"  # noqa: S105
        xtream_mock.get(f"{PMS}/library/sections").mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )
        caplog.set_level(logging.DEBUG)
        ok = await svc.probe(PMS, secret_token)
        assert ok is False
        assert secret_token not in caplog.text


# ─── parse_guids ──────────────────────────────────────────────────────────


class TestParseGuids:
    def test_extracts_imdb_tmdb_tvdb(self):
        metadata = {"Guid": [
            {"id": "imdb://tt0110912"}, {"id": "tmdb://680"}, {"id": "tvdb://12345"},
        ]}
        result = parse_guids(metadata)
        assert result == {"imdb_id": "tt0110912", "tmdb_id": "680", "tvdb_id": "12345"}

    def test_missing_guid_key_returns_all_none(self):
        assert parse_guids({}) == {"imdb_id": None, "tmdb_id": None, "tvdb_id": None}

    def test_non_dict_metadata_returns_all_none(self):
        assert parse_guids(None) == {"imdb_id": None, "tmdb_id": None, "tvdb_id": None}

    def test_unrecognized_prefix_ignored(self):
        metadata = {"Guid": [{"id": "anidb://999"}]}
        result = parse_guids(metadata)
        assert result == {"imdb_id": None, "tmdb_id": None, "tvdb_id": None}

    def test_partial_guids(self):
        metadata = {"Guid": [{"id": "tmdb://42"}]}
        result = parse_guids(metadata)
        assert result["imdb_id"] is None
        assert result["tmdb_id"] == "42"


# ─── parse_genres ─────────────────────────────────────────────────────────


class TestParseGenres:
    def test_joins_tags_comma_separated(self):
        metadata = {"Genre": [{"tag": "Action"}, {"tag": "Sci-Fi"}]}
        assert parse_genres(metadata) == "Action, Sci-Fi"

    def test_missing_genre_key_returns_none(self):
        assert parse_genres({}) is None

    def test_non_dict_metadata_returns_none(self):
        assert parse_genres(None) is None

    def test_empty_or_blank_tags_dropped(self):
        metadata = {"Genre": [{"tag": "  "}, {"tag": "Drama"}, {"noTag": "x"}, "junk"]}
        assert parse_genres(metadata) == "Drama"

    def test_all_blank_returns_none(self):
        assert parse_genres({"Genre": [{"tag": ""}, {}]}) is None


# ─── best_media ───────────────────────────────────────────────────────────


class TestBestMedia:
    def test_picks_max_height(self):
        metadata = {
            "Media": [
                {"height": 720, "width": 1280, "videoCodec": "h264", "audioCodec": "aac",
                 "container": "mp4", "bitrate": 3000, "Part": [{"key": "/p/1", "size": 100}]},
                {"height": 1080, "width": 1920, "videoCodec": "hevc", "audioCodec": "ac3",
                 "container": "mkv", "bitrate": 8000, "Part": [{"key": "/p/2", "size": 200}]},
            ]
        }
        result = best_media(metadata)
        assert result["height"] == 1080
        assert result["video_codec"] == "hevc"
        assert result["part_key"] == "/p/2"
        assert result["part_size"] == 200

    def test_tie_break_by_bitrate(self):
        metadata = {
            "Media": [
                {"height": 1080, "bitrate": 4000, "Part": [{"key": "/p/lo", "size": 1}]},
                {"height": 1080, "bitrate": 9000, "Part": [{"key": "/p/hi", "size": 2}]},
            ]
        }
        result = best_media(metadata)
        assert result["part_key"] == "/p/hi"

    def test_no_media_returns_none(self):
        assert best_media({}) is None
        assert best_media({"Media": []}) is None

    def test_media_without_part_returns_none(self):
        assert best_media({"Media": [{"height": 1080}]}) is None
