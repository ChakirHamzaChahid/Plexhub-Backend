"""CR-T07 (P2): direct HTTP coverage for router endpoints that previously had
NONE — status codes + response shapes, both success and representative error
cases, hermetic (in-memory DB, mocked Xtream/external calls).

Already covered elsewhere (NOT duplicated here):
  - Fail-closed auth (401 with no/wrong key, master key passes) for one GET
    per guarded router, + `POST /api/plex/generate` auth: `test_auth_guard.py`.
  - `POST /api/plex/generate` output-dir confinement + a full dry-run
    generation happy path: `test_plex_api_security.py`.
  - `GET/POST /api/accounts` PUT/DELETE lock-retry behavior (200/204 happy
    path already exercised as a side effect): `test_accounts_retry.py`,
    `test_account_service.py` (service-level, not HTTP).
  - `POST /api/accounts/{id}/categories/refresh` camelCase contract:
    `test_categories_refresh_camelcase.py`.
  - `GET /api/sync/jobs`, `DELETE /api/sync/cancel/{name}`,
    `POST /api/sync/enrichment` typed responses: `test_sync_responses.py`.
  - `GET /api/live/channels` search/count/pagination: `test_live_channels_query.py`.
  - `GET /api/media/movies` (auth only): `test_auth_guard.py`.
  - `/admin/*` HTML UI: `test_admin.py`.

Newly covered here (no prior HTTP-level test existed for any of these):
  - `GET /api/stream/{rating_key}` (stream.py) — happy path + all 3 error
    branches (bad server_id format, unknown account, unbuildable rating_key).
  - `PATCH /api/media/{rating_key}` and `POST /api/media/{rating_key}/rescrape`
    (media.py) — happy path + 404.
  - `GET /api/live/channels/{stream_id}`, `.../stream`, `.../epg`,
    `GET /api/live/epg` (live.py) — happy path + error branches, including the
    DB-cache-empty -> Xtream-fetch -> persist path for the per-channel EPG
    endpoint.
  - `/api/admin/keys` CRUD (api_keys.py) — create/list/revoke/delete with the
    master key, and the master-key-only guard (a per-user key must NOT be
    able to manage other keys).
  - `POST /api/accounts` (create) and `POST /api/accounts/{id}/test` — happy
    path + domain-error branches (409 duplicate, 400 auth failure, 404).
"""
from __future__ import annotations

import pytest_asyncio

from app.config import settings
from app.db import database as db_module
from app.models.database import EpgEntry, LiveChannel, Media, XtreamAccount
from app.services import api_key_service
from app.services.xtream_service import xtream_service

# pytest-asyncio runs in auto mode (pyproject.toml) — async tests need no mark.

API_KEY = "test-master-key-router-coverage"
API_HEADERS = {"X-API-Key": API_KEY}


def _account(id_: str = "acct1", base_url: str = "http://acct1.example") -> XtreamAccount:
    return XtreamAccount(
        id=id_, label="Compte", base_url=base_url, port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


@pytest_asyncio.fixture(autouse=True)
async def _wire_test_db(monkeypatch, db_factory):
    """Point `get_db` at the isolated in-memory engine (all tables created),
    same pattern as tests/test_auth_guard.py.

    `api_key_service.resolve()` (used by `verify_backend_secret`/
    `verify_master_key` -> `_authenticate` for the per-user-key path, and
    exercised directly by the /api/admin/keys tests below) opens its OWN
    session via a module-level `async_session_factory` name bound at import
    time (`from app.db.database import async_session_factory`) — patching
    `app.db.database.async_session_factory` alone does not reach it, so it
    must also be patched directly on `app.services.api_key_service` (same
    caveat as `tests/test_plex_api_security.py`'s `plex_source_module`
    wiring, and as `tests/test_api_key_service.py`)."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    return db_factory


@pytest_asyncio.fixture
async def seeded_account(db_factory):
    async with db_factory() as s:
        s.add(_account("acct1"))
        await s.commit()
    return db_factory


# ══════════════════════════════════════════════════════════════════════════
# GET /api/stream/{rating_key}  (stream.py)
# ══════════════════════════════════════════════════════════════════════════


class TestStreamEndpoint:
    async def test_happy_path_returns_built_movie_url(self, api_client, seeded_account):
        resp = await api_client.get(
            "/api/stream/vod_123.mp4",
            params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["url"] == "http://acct1.example/movie/u/p/123.mp4"

    async def test_episode_rating_key_builds_series_url(self, api_client, seeded_account):
        resp = await api_client.get(
            "/api/stream/ep_456.mkv",
            params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["url"] == "http://acct1.example/series/u/p/456.mkv"

    async def test_invalid_server_id_format_returns_400(self, api_client, seeded_account):
        resp = await api_client.get(
            "/api/stream/vod_123.mp4",
            params={"server_id": "not-xtream-prefixed"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400
        assert "server_id" in resp.json()["detail"]

    async def test_unknown_account_returns_404(self, api_client, seeded_account):
        resp = await api_client.get(
            "/api/stream/vod_123.mp4",
            params={"server_id": "xtream_no-such-account"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 404

    async def test_unbuildable_rating_key_returns_400(self, api_client, seeded_account):
        """A rating_key that doesn't match any known vod_/ep_/live_ prefix
        parses to type 'unknown' -> build_stream_url returns None -> 400."""
        resp = await api_client.get(
            "/api/stream/totally-unrecognized-key",
            params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400
        assert "Cannot build stream URL" in resp.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════
# PATCH /api/media/{rating_key} + POST /api/media/{rating_key}/rescrape
# ══════════════════════════════════════════════════════════════════════════


def _movie(rating_key: str = "rk-1", server_id: str = "xtream_acct1") -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id,
        filter="all", sort_order="default",
        library_section_id="lib-1", title="Inception",
        type="movie", year=2010, added_at=1, updated_at=1,
    )


@pytest_asyncio.fixture
async def seeded_movie(db_factory):
    async with db_factory() as s:
        s.add(_movie())
        await s.commit()
    return db_factory


class TestMediaMutateEndpoints:
    async def test_patch_updates_imdb_and_tmdb_ids(self, api_client, seeded_movie):
        resp = await api_client.patch(
            "/api/media/rk-1",
            params={"server_id": "xtream_acct1"},
            json={"imdbId": "tt1375666", "tmdbId": "27205"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imdbId"] == "tt1375666"
        assert body["tmdbId"] == "27205"

    async def test_patch_unknown_media_returns_404(self, api_client, seeded_movie):
        resp = await api_client.patch(
            "/api/media/no-such-key",
            params={"server_id": "xtream_acct1"},
            json={"imdbId": "tt0000000"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 404

    async def test_patch_empty_body_is_a_noop_returns_current_row(self, api_client, seeded_movie):
        resp = await api_client.patch(
            "/api/media/rk-1",
            params={"server_id": "xtream_acct1"},
            json={},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "Inception"

    async def test_rescrape_queues_media_returns_202(self, api_client, seeded_movie):
        resp = await api_client.post(
            "/api/media/rk-1/rescrape",
            params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 202
        assert resp.json() == {"status": "queued"}

    async def test_rescrape_unknown_media_returns_404(self, api_client, seeded_movie):
        resp = await api_client.post(
            "/api/media/no-such-key/rescrape",
            params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# GET /api/media/episodes — server_id is MANDATORY (homonym-collision guard)
# ══════════════════════════════════════════════════════════════════════════


def _episode(rating_key: str, server_id: str, grandparent_rating_key: str, title: str) -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id,
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=title, type="episode", year=2024,
        grandparent_rating_key=grandparent_rating_key,
        added_at=1, updated_at=1, is_in_allowed_categories=True, is_broken=False,
    )


@pytest_asyncio.fixture
async def seeded_homonym_series(db_factory):
    """Two DIFFERENT series colliding on the SAME provider rating key
    `series_7724` across two accounts (the real MAO vs Treadstone case). The
    episode rating keys are distinct, so a query WITHOUT server_id would return
    the UNION (mixed) — this fixture is what makes the collision observable."""
    async with db_factory() as s:
        s.add(_episode("ep_A_1", "xtream_acctA", "series_7724", "MAO S01E01"))
        s.add(_episode("ep_B_1", "xtream_acctB", "series_7724", "Treadstone S01E01"))
        await s.commit()
    return db_factory


class TestEpisodesRequireServerId:
    """Regression for the MAO/Treadstone bug: (parent_rating_key, server_id) is
    the only key that identifies a series, so the raw episodes endpoint must
    refuse an ambiguous query rather than mix two homonymous series."""

    async def test_missing_server_id_returns_400(self, api_client, seeded_homonym_series):
        resp = await api_client.get(
            "/api/media/episodes",
            params={"parent_rating_key": "series_7724"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400, resp.text
        assert "server_id" in resp.json()["detail"]

    async def test_blank_server_id_returns_400(self, api_client, seeded_homonym_series):
        resp = await api_client.get(
            "/api/media/episodes",
            params={"parent_rating_key": "series_7724", "server_id": ""},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400

    async def test_with_server_id_scopes_to_one_account_no_mixing(
        self, api_client, seeded_homonym_series,
    ):
        resp = await api_client.get(
            "/api/media/episodes",
            params={"parent_rating_key": "series_7724", "server_id": "xtream_acctA"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # ONLY account A's series — Treadstone (account B) must not bleed in.
        assert [i["title"] for i in body["items"]] == ["MAO S01E01"]
        assert body["total"] == 1

    async def test_other_account_returns_its_own_series(
        self, api_client, seeded_homonym_series,
    ):
        resp = await api_client.get(
            "/api/media/episodes",
            params={"parent_rating_key": "series_7724", "server_id": "xtream_acctB"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert [i["title"] for i in resp.json()["items"]] == ["Treadstone S01E01"]


# ══════════════════════════════════════════════════════════════════════════
# live.py — per-channel endpoints + /api/live/epg
# ══════════════════════════════════════════════════════════════════════════


def _channel(stream_id: int = 1, server_id: str = "xtream_acct1", **kw) -> LiveChannel:
    return LiveChannel(
        stream_id=stream_id, server_id=server_id,
        name=kw.get("name", "Channel 1"), name_sortable=kw.get("name", "channel 1").lower(),
        category_id="1", is_in_allowed_categories=True,
        container_extension=kw.get("container_extension", "ts"),
        epg_channel_id=kw.get("epg_channel_id", "epg-1"),
        added_at=0,
    )


@pytest_asyncio.fixture
async def seeded_channel(db_factory):
    async with db_factory() as s:
        s.add(_account("acct1"))
        s.add(_channel(1))
        await s.commit()
    return db_factory


class TestLiveChannelDetailEndpoint:
    async def test_get_channel_happy_path(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/channels/1", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Channel 1"

    async def test_get_channel_not_found_404(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/channels/999", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 404


class TestLiveChannelStreamEndpoint:
    async def test_stream_url_happy_path(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/channels/1/stream", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["url"] == "http://acct1.example/live/u/p/1.ts"

    async def test_stream_invalid_server_id_400(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/channels/1/stream", params={"server_id": "bogus"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400

    async def test_stream_unknown_account_404(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/channels/1/stream", params={"server_id": "xtream_no-such"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 404

    async def test_stream_unknown_channel_404(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/channels/999/stream", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 404


class TestLiveChannelEpgEndpoint:
    async def test_epg_served_from_db_cache_when_present(self, api_client, seeded_channel, db_factory):
        async with db_factory() as s:
            s.add(EpgEntry(
                server_id="xtream_acct1", epg_channel_id="epg-1", stream_id=1,
                title="Now Playing", start_time=0, end_time=99_999_999_999_999,
            ))
            await s.commit()

        resp = await api_client.get(
            "/api/live/channels/1/epg", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "Now Playing"

    async def test_epg_cache_miss_fetches_from_xtream_and_persists(
        self, monkeypatch, api_client, seeded_channel, db_factory,
    ):
        """DB cache empty -> live_service.ingest_short_epg fetches from
        Xtream, stages EpgEntry rows, and the router commits them."""
        async def _fake_get_short_epg(account, *, stream_id):
            assert stream_id == 1
            return {
                "epg_listings": [{
                    "epg_id": "epg-1", "title": "Fresh Show",
                    "description": "desc",
                    "start_timestamp": "9999999999",
                    "stop_timestamp": "9999999999",
                }],
            }

        monkeypatch.setattr(xtream_service, "get_short_epg", _fake_get_short_epg)

        resp = await api_client.get(
            "/api/live/channels/1/epg", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "Fresh Show"

        # Persisted — a second call with the fetch stubbed to fail must still
        # be served from the now-populated cache.
        async def _boom(*args, **kwargs):
            raise AssertionError("should not re-fetch — cache should be hit")

        monkeypatch.setattr(xtream_service, "get_short_epg", _boom)
        resp2 = await api_client.get(
            "/api/live/channels/1/epg", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp2.status_code == 200
        assert resp2.json()["total"] == 1

    async def test_epg_invalid_server_id_400(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/channels/1/epg", params={"server_id": "bogus"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400

    async def test_epg_unknown_account_404(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/channels/1/epg", params={"server_id": "xtream_no-such"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 404


class TestLiveEpgNowEndpoint:
    async def test_epg_now_returns_currently_airing_entries(
        self, api_client, seeded_channel, db_factory,
    ):
        async with db_factory() as s:
            s.add(EpgEntry(
                server_id="xtream_acct1", epg_channel_id="epg-1", stream_id=1,
                title="On Air", start_time=0, end_time=99_999_999_999_999,
            ))
            # Distinct start_time — (server_id, stream_id, start_time) is
            # unique (uix_epg_dedup).
            s.add(EpgEntry(
                server_id="xtream_acct1", epg_channel_id="epg-1", stream_id=1,
                title="Long Past", start_time=1, end_time=1,
            ))
            await s.commit()

        resp = await api_client.get(
            "/api/live/epg", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "On Air"

    async def test_epg_now_empty_when_nothing_airing(self, api_client, seeded_channel):
        resp = await api_client.get(
            "/api/live/epg", params={"server_id": "xtream_acct1"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ══════════════════════════════════════════════════════════════════════════
# /api/admin/keys — api_keys.py (master-key-only management)
# ══════════════════════════════════════════════════════════════════════════


class TestApiKeyManagementEndpoints:
    async def test_create_list_revoke_delete_lifecycle(self, api_client, seeded_account):
        # Create.
        create_resp = await api_client.post(
            "/api/admin/keys", json={"label": "Chakir's phone"}, headers=API_HEADERS,
        )
        assert create_resp.status_code == 201, create_resp.text
        created = create_resp.json()
        assert created["key"].startswith("phk_")
        assert created["status"] == "active"
        key_id = created["id"]

        # List — includes the created key, no plaintext leaked.
        list_resp = await api_client.get("/api/admin/keys", headers=API_HEADERS)
        assert list_resp.status_code == 200
        items = list_resp.json()
        assert any(k["id"] == key_id for k in items)
        assert all("key" not in k for k in items)  # ApiKeyOut has no plaintext field

        # The freshly minted key authenticates the JSON API (per-user key path).
        auth_check = await api_client.get(
            "/api/accounts", headers={"X-API-Key": created["key"]},
        )
        assert auth_check.status_code != 401

        # Revoke.
        revoke_resp = await api_client.post(
            f"/api/admin/keys/{key_id}/revoke", headers=API_HEADERS,
        )
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["status"] == "revoked"

        # Revoked key no longer authenticates.
        auth_after_revoke = await api_client.get(
            "/api/accounts", headers={"X-API-Key": created["key"]},
        )
        assert auth_after_revoke.status_code == 401

        # Delete (soft-revoke variant) on an already-revoked key still 200s.
        delete_resp = await api_client.delete(
            f"/api/admin/keys/{key_id}", headers=API_HEADERS,
        )
        assert delete_resp.status_code == 200
        assert delete_resp.json()["status"] == "revoked"

    async def test_revoke_unknown_key_returns_404(self, api_client, seeded_account):
        resp = await api_client.post(
            "/api/admin/keys/no-such-id/revoke", headers=API_HEADERS,
        )
        assert resp.status_code == 404

    async def test_create_key_requires_master_key_not_a_per_user_key(
        self, api_client, seeded_account, db_factory,
    ):
        """verify_master_key must reject an active per-user key — only the
        master secret can mint/revoke keys (a per-user key can't create
        sibling keys for itself)."""
        async with db_factory() as db:
            _row, plaintext = await api_key_service.create_key(db, label="Limited user")

        resp = await api_client.post(
            "/api/admin/keys",
            json={"label": "Should be rejected"},
            headers={"X-API-Key": plaintext},
        )
        assert resp.status_code == 401

    async def test_create_key_without_any_key_401(self, api_client, seeded_account):
        resp = await api_client.post("/api/admin/keys", json={"label": "No auth"})
        assert resp.status_code == 401

    async def test_create_key_with_expiry_sets_expires_at(self, api_client, seeded_account):
        resp = await api_client.post(
            "/api/admin/keys",
            json={"label": "Expiring", "expiresInDays": 30},
            headers=API_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["expiresAt"] is not None


# ══════════════════════════════════════════════════════════════════════════
# accounts.py — POST /api/accounts (create) + POST /api/accounts/{id}/test
# ══════════════════════════════════════════════════════════════════════════


class TestAccountsCreateEndpoint:
    async def test_create_account_happy_path_returns_201(self, monkeypatch, api_client):
        async def _fake_authenticate(creds):
            return {
                "user_info": {
                    "status": "Active", "exp_date": "1700000000",
                    "max_connections": "2", "allowed_output_formats": ["ts", "m3u8"],
                },
                "server_info": {"url": "example.com", "https_port": "8443"},
            }

        monkeypatch.setattr(xtream_service, "authenticate", _fake_authenticate)

        # The endpoint fires a background sync on success — stub it out so
        # the test stays hermetic (no real Xtream calls from the fire-and-
        # forget task) and doesn't leak a lingering task across the test.
        import app.workers.sync_worker as sync_worker_module

        async def _noop_sync(account_id: str):
            return None

        monkeypatch.setattr(sync_worker_module, "sync_account", _noop_sync)

        resp = await api_client.post(
            "/api/accounts",
            json={
                "label": "My IPTV", "baseUrl": "http://provider.example",
                "port": 8080, "username": "bob", "password": "secret",
            },
            headers=API_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["label"] == "My IPTV"
        assert body["status"] == "Active"
        assert body["maxConnections"] == 2

    async def test_create_account_duplicate_returns_409(self, monkeypatch, api_client, db_factory):
        """account_service.create_account keys the duplicate check on the
        MD5-derived id (`generate_account_id(base_url, username)`), not the
        DB row's own `id` column — seed the account under that derived id so
        the lookup actually collides (the generic `seeded_account` fixture
        uses an arbitrary literal id "acct1", which would NOT collide and
        would fall through to a live Xtream auth attempt instead)."""
        from app.services import account_service

        base_url, username = "http://acct1.example", "u"
        derived_id = account_service.generate_account_id(base_url, username)
        async with db_factory() as s:
            s.add(_account(derived_id, base_url=base_url))
            await s.commit()

        resp = await api_client.post(
            "/api/accounts",
            json={
                "label": "Dup", "baseUrl": base_url,
                "port": 80, "username": username, "password": "p",
            },
            headers=API_HEADERS,
        )
        assert resp.status_code == 409

    async def test_create_account_auth_failure_returns_400(self, monkeypatch, api_client):
        async def _boom(creds):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(xtream_service, "authenticate", _boom)

        resp = await api_client.post(
            "/api/accounts",
            json={
                "label": "X", "baseUrl": "http://unreachable.example",
                "port": 80, "username": "u", "password": "p",
            },
            headers=API_HEADERS,
        )
        assert resp.status_code == 400
        assert "Authentication failed" in resp.json()["detail"]


class TestAccountsTestConnectionEndpoint:
    async def test_test_connection_happy_path_returns_200(
        self, monkeypatch, api_client, seeded_account,
    ):
        async def _fake_authenticate(account):
            return {
                "user_info": {
                    "status": "Active", "exp_date": "1700000000",
                    "max_connections": "5",
                    "allowed_output_formats": ["ts", "m3u8"],
                },
            }

        monkeypatch.setattr(xtream_service, "authenticate", _fake_authenticate)

        resp = await api_client.post(
            "/api/accounts/acct1/test", headers=API_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "Active"
        assert body["maxConnections"] == 5
        assert body["allowedFormats"] == "ts,m3u8"

    async def test_test_connection_unknown_account_404(self, api_client, seeded_account):
        resp = await api_client.post(
            "/api/accounts/no-such-account/test", headers=API_HEADERS,
        )
        assert resp.status_code == 404

    async def test_test_connection_auth_failure_returns_400(
        self, monkeypatch, api_client, seeded_account,
    ):
        async def _boom(account):
            raise RuntimeError("timeout")

        monkeypatch.setattr(xtream_service, "authenticate", _boom)

        resp = await api_client.post(
            "/api/accounts/acct1/test", headers=API_HEADERS,
        )
        assert resp.status_code == 400
        assert "Connection test failed" in resp.json()["detail"]
