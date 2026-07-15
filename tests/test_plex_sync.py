"""`app.services.plex_sync_service.run_full_sync` — discover -> probe ->
catalogue-sync -> tmdb/imdb bridge -> mark-and-sweep (PH-PLEX-03).

All Plex HTTP is mocked via `respx` (`xtream_mock` fixture — host-agnostic,
registers full URLs against `plex.tv` and a fake PMS host alike). Every test
uses the `sync_env` fixture, which points `plex_sync_service.plex_api_service`
at a FRESH `PlexApiService()` instance (so respx interception + client
lifecycle never bleed across tests / the module singleton) and yields the
in-memory `db_factory` session factory `run_full_sync` expects.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.models.database import PlexMediaItem, PlexServer, PlexSyncStatus
from app.services import plex_sync_service as svc_mod
from app.services.plex_api_service import PlexApiService

pytestmark = pytest.mark.asyncio

RESOURCES_URL = "https://plex.tv/api/v2/resources"
PMS = "http://192.168.1.50:32400"


@pytest_asyncio.fixture
async def sync_env(monkeypatch, db_factory):
    """Configure the feature as enabled + isolate the Plex API client."""
    monkeypatch.setattr(settings, "PLEX_ACCOUNT_TOKEN", "acct-secret-token")  # noqa: S105
    monkeypatch.setattr(settings, "PLEX_CLIENT_IDENTIFIER", "backend-client-id")
    monkeypatch.setattr(settings, "PLEX_PROBE_TIMEOUT", 5)
    fresh_api = PlexApiService()
    monkeypatch.setattr(svc_mod, "plex_api_service", fresh_api)
    try:
        yield db_factory
    finally:
        await fresh_api.close()


# ─── Fixture builders (Plex JSON shapes) ────────────────────────────────


def _resource(
    client_id="cid-1", name="My Server", owned=True, source_title=None,
    access_token="srv-token", connections=None, provides="server",
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
        else [{"protocol": "http", "address": "192.168.1.50", "port": 32400,
               "uri": PMS, "local": True, "relay": False}],
    }


def _movie_meta(rating_key, title, *, guid_imdb=None, guid_tmdb=None, media=None):
    guid = []
    if guid_imdb:
        guid.append({"id": f"imdb://{guid_imdb}"})
    if guid_tmdb:
        guid.append({"id": f"tmdb://{guid_tmdb}"})
    return {
        "ratingKey": rating_key, "title": title, "year": 1999, "addedAt": 1_000_000_000,
        "duration": 7_200_000, "thumb": "/library/metadata/x/thumb", "Guid": guid,
        "Media": media or [
            {"height": 480, "width": 640, "videoCodec": "h264", "audioCodec": "aac",
             "container": "mp4", "bitrate": 1500, "Part": [{"key": f"/parts/{rating_key}-lo", "size": 111}]},
            {"height": 1080, "width": 1920, "videoCodec": "hevc", "audioCodec": "ac3",
             "container": "mkv", "bitrate": 8000, "Part": [{"key": f"/parts/{rating_key}-hi", "size": 999}]},
        ],
    }


def _show_meta(rating_key, title, *, guid_imdb=None, guid_tmdb=None):
    guid = []
    if guid_imdb:
        guid.append({"id": f"imdb://{guid_imdb}"})
    if guid_tmdb:
        guid.append({"id": f"tmdb://{guid_tmdb}"})
    return {
        "ratingKey": rating_key, "title": title, "year": 2008,
        "addedAt": 1_000_000_000, "thumb": "/t", "Guid": guid,
    }


def _season_meta(rating_key, index):
    return {"ratingKey": rating_key, "index": index, "title": f"Season {index}"}


def _episode_meta(rating_key, index):
    return {
        "ratingKey": rating_key, "index": index, "title": f"Episode {index}",
        "addedAt": 1_000_000_000, "duration": 1_800_000,
        "Media": [{"height": 720, "width": 1280, "videoCodec": "h264", "audioCodec": "aac",
                   "container": "mkv", "bitrate": 2000, "Part": [{"key": f"/parts/{rating_key}", "size": 222}]}],
    }


# ─── End-to-end: sections -> items -> children (2 levels) ────────────────


class TestFullSyncEndToEnd:
    async def test_movies_shows_episodes_and_best_media(self, sync_env, xtream_mock):
        xtream_mock.get(RESOURCES_URL).respond(200, json=[_resource()])
        xtream_mock.get(f"{PMS}/library/sections").respond(
            200, json={"MediaContainer": {"Directory": [
                {"key": "1", "title": "Movies", "type": "movie"},
                {"key": "2", "title": "Shows", "type": "show"},
            ]}},
        )
        xtream_mock.get(f"{PMS}/library/sections/1/all").respond(
            200, json={"MediaContainer": {"Metadata": [
                _movie_meta("100", "The Matrix", guid_imdb="tt0133093"),
            ]}},
        )
        xtream_mock.get(f"{PMS}/library/sections/2/all").respond(
            200, json={"MediaContainer": {"Metadata": [
                _show_meta("200", "Breaking Bad", guid_imdb="tt0903747"),
            ]}},
        )
        xtream_mock.get(f"{PMS}/library/metadata/200/children").respond(
            200, json={"MediaContainer": {"Metadata": [_season_meta("201", 1)]}},
        )
        xtream_mock.get(f"{PMS}/library/metadata/201/children").respond(
            200, json={"MediaContainer": {"Metadata": [
                _episode_meta("300", 1), _episode_meta("301", 2),
            ]}},
        )

        report = await svc_mod.run_full_sync(sync_env)

        assert report.status == "ok"
        assert report.servers_total == 1
        assert report.servers_reachable == 1
        assert report.movies == 1
        assert report.shows == 1
        assert report.episodes == 2
        assert report.errors == []

        server_id = "plex_cid-1"
        async with sync_env() as db:
            movie = (await db.execute(
                select(PlexMediaItem).where(PlexMediaItem.server_id == server_id, PlexMediaItem.type == "movie")
            )).scalars().one()
            assert movie.title == "The Matrix"
            assert movie.unification_id == "imdb://tt0133093"
            # best_media: multi-version Media[] -> the 1080p entry wins.
            assert movie.height == 1080
            assert movie.part_key == "/parts/100-hi"

            show = (await db.execute(
                select(PlexMediaItem).where(PlexMediaItem.server_id == server_id, PlexMediaItem.type == "show")
            )).scalars().one()
            assert show.unification_id == "imdb://tt0903747"

            episodes = (await db.execute(
                select(PlexMediaItem).where(PlexMediaItem.server_id == server_id, PlexMediaItem.type == "episode")
            )).scalars().all()
            assert {e.rating_key for e in episodes} == {"300", "301"}
            for ep in episodes:
                assert ep.unification_id is None
                assert ep.grandparent_rating_key == "200"
                assert ep.parent_rating_key == "201"
                assert ep.parent_index == 1

            server = await db.get(PlexServer, "cid-1")
            assert server.is_reachable is True
            assert server.base_uri == PMS
            assert server.last_synced_at is not None
            assert server.last_sync_error is None
            # The per-server secret is never logged, but it IS the encrypted
            # column's job to hold it — round-trips transparently.
            assert server.access_token == "srv-token"


# ─── Probe order ──────────────────────────────────────────────────────────


class TestProbeOrder:
    async def test_owned_prefers_local_over_public(self, sync_env, xtream_mock):
        local_uri = "http://10.0.0.5:32400"
        public_uri = "http://203.0.113.9:32400"
        xtream_mock.get(RESOURCES_URL).respond(
            200, json=[_resource(owned=True, connections=[
                {"protocol": "https", "address": "203.0.113.9", "port": 32400,
                 "uri": public_uri, "local": False, "relay": False},
                {"protocol": "http", "address": "10.0.0.5", "port": 32400,
                 "uri": local_uri, "local": True, "relay": False},
            ])],
        )
        local_route = xtream_mock.get(f"{local_uri}/library/sections").respond(
            200, json={"MediaContainer": {"Directory": []}},
        )
        public_route = xtream_mock.get(f"{public_uri}/library/sections").respond(
            200, json={"MediaContainer": {"Directory": []}},
        )

        report = await svc_mod.run_full_sync(sync_env)

        assert report.servers_reachable == 1
        assert public_route.call_count == 0, "public must never be probed once local wins"
        assert local_route.call_count >= 1

        async with sync_env() as db:
            server = await db.get(PlexServer, "cid-1")
            assert server.base_uri == local_uri

    async def test_shared_falls_back_public_local_then_relay(self, sync_env, xtream_mock):
        public_uri = "http://203.0.113.9:32400"
        local_uri = "http://10.0.0.5:32400"
        relay_uri = "https://relay.plex.tv:443"
        xtream_mock.get(RESOURCES_URL).respond(
            200, json=[_resource(owned=False, source_title="Friend", connections=[
                {"protocol": "https", "address": "203.0.113.9", "port": 32400,
                 "uri": public_uri, "local": False, "relay": False},
                {"protocol": "http", "address": "10.0.0.5", "port": 32400,
                 "uri": local_uri, "local": True, "relay": False},
                {"protocol": "https", "address": "relay.plex.tv", "port": 443,
                 "uri": relay_uri, "local": False, "relay": True},
            ])],
        )
        xtream_mock.get(f"{public_uri}/library/sections").mock(side_effect=httpx.ConnectTimeout("down"))
        xtream_mock.get(f"{local_uri}/library/sections").respond(503)
        relay_route = xtream_mock.get(f"{relay_uri}/library/sections").respond(
            200, json={"MediaContainer": {"Directory": []}},
        )

        report = await svc_mod.run_full_sync(sync_env)

        assert report.servers_reachable == 1
        assert relay_route.call_count >= 1
        async with sync_env() as db:
            server = await db.get(PlexServer, "cid-1")
            assert server.base_uri == relay_uri
            assert server.owned is False
            assert server.owner_title == "Friend"


# ─── Mark-and-sweep ─────────────────────────────────────────────────────


class TestMarkAndSweep:
    async def test_item_missing_from_listing_is_removed(self, sync_env, xtream_mock):
        xtream_mock.get(RESOURCES_URL).respond(200, json=[_resource()])
        xtream_mock.get(f"{PMS}/library/sections").respond(
            200, json={"MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "type": "movie"}]}},
        )
        xtream_mock.get(f"{PMS}/library/sections/1/all").respond(
            200, json={"MediaContainer": {"Metadata": [_movie_meta("100", "Still Here", guid_imdb="tt1")]}},
        )

        server_id = "plex_cid-1"
        async with sync_env() as db:
            db.add(PlexMediaItem(
                server_id=server_id, rating_key="999", type="movie", title="Gone Now",
                unification_id="plexsrc://plex_cid-1/999", synced_at=1,
            ))
            await db.commit()

        report = await svc_mod.run_full_sync(sync_env)
        assert report.movies == 1

        async with sync_env() as db:
            remaining = (await db.execute(
                select(PlexMediaItem.rating_key).where(PlexMediaItem.server_id == server_id)
            )).scalars().all()
            assert set(remaining) == {"100"}


# ─── Claim / release / reap ────────────────────────────────────────────


class TestClaim:
    async def test_second_concurrent_claim_is_refused(self, db_factory):
        first = await svc_mod._claim_sync(db_factory)
        second = await svc_mod._claim_sync(db_factory)
        assert first is True
        assert second is False

    async def test_release_then_reclaim_succeeds(self, db_factory):
        assert await svc_mod._claim_sync(db_factory) is True
        await svc_mod._release_sync(db_factory)
        assert await svc_mod._claim_sync(db_factory) is True

    async def test_reap_sync_status_resets_stuck_running(self, db_factory):
        assert await svc_mod._claim_sync(db_factory) is True
        await svc_mod.reap_sync_status(db_factory)
        async with db_factory() as db:
            row = await db.get(PlexSyncStatus, 1)
            assert row.state == "idle"

    async def test_run_full_sync_reports_already_running(self, sync_env):
        assert await svc_mod._claim_sync(sync_env) is True
        report = await svc_mod.run_full_sync(sync_env)
        assert report.status == "already_running"
        assert report.servers_total == 0


# ─── Network error -> status reset to idle, bounded, secret-free ────────


class TestNetworkErrorResetsStatus:
    async def test_discover_failure_releases_status_idle_with_bounded_secretless_error(
        self, sync_env, xtream_mock, monkeypatch,
    ):
        from app.services import plex_api_service as api_mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(api_mod.asyncio, "sleep", _no_sleep)
        xtream_mock.get(RESOURCES_URL).respond(500)

        report = await svc_mod.run_full_sync(sync_env)

        assert report.status == "error"
        joined_errors = " ".join(report.errors)
        assert settings.PLEX_ACCOUNT_TOKEN not in joined_errors

        async with sync_env() as db:
            row = await db.get(PlexSyncStatus, 1)
            assert row.state == "idle"
            assert row.error is not None
            assert len(row.error) <= 200
            assert settings.PLEX_ACCOUNT_TOKEN not in row.error


# ─── Feature disabled (no PLEX_ACCOUNT_TOKEN) -> no-op ───────────────────


class TestFeatureDisabled:
    async def test_empty_account_token_is_a_noop(self, db_factory, monkeypatch):
        monkeypatch.setattr(settings, "PLEX_ACCOUNT_TOKEN", "")
        report = await svc_mod.run_full_sync(db_factory)

        assert report.status == "disabled"
        assert report.servers_total == 0
        async with db_factory() as db:
            row = await db.get(PlexSyncStatus, 1)
            # No claim was ever attempted -> status row untouched (absent or idle).
            assert row is None or row.state == "idle"


# ─── Unreachable server: catalogue skipped, old data preserved ──────────


class TestUnreachableServer:
    async def test_all_connections_failing_marks_unreachable_and_skips_catalogue(
        self, sync_env, xtream_mock,
    ):
        xtream_mock.get(RESOURCES_URL).respond(200, json=[_resource(client_id="cid-2")])
        xtream_mock.get(f"{PMS}/library/sections").mock(side_effect=httpx.ConnectTimeout("down"))

        report = await svc_mod.run_full_sync(sync_env)

        assert report.servers_total == 1
        assert report.servers_reachable == 0
        assert report.movies == 0 and report.shows == 0 and report.episodes == 0

        async with sync_env() as db:
            server = await db.get(PlexServer, "cid-2")
            assert server.is_reachable is False
            assert server.base_uri is None
            assert server.last_synced_at is None

    async def test_unreachable_server_keeps_its_previous_catalogue(self, sync_env, xtream_mock):
        xtream_mock.get(RESOURCES_URL).respond(200, json=[_resource(client_id="cid-3")])
        xtream_mock.get(f"{PMS}/library/sections").respond(503)

        server_id = "plex_cid-3"
        async with sync_env() as db:
            db.add(PlexMediaItem(
                server_id=server_id, rating_key="1", type="movie", title="Old Movie",
                unification_id="plexsrc://plex_cid-3/1", synced_at=1,
            ))
            await db.commit()

        await svc_mod.run_full_sync(sync_env)

        async with sync_env() as db:
            remaining = (await db.execute(
                select(PlexMediaItem.rating_key).where(PlexMediaItem.server_id == server_id)
            )).scalars().all()
            assert remaining == ["1"], "an unreachable server's catalogue must never be swept"
