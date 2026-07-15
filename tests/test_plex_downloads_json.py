"""tests/test_plex_downloads_json.py -- JSON read-only mirror of the Plex
shared-servers catalogue (feature "Télécharger Plex", Tâche C7,
docs/10-prd-plex-download.md).

Covers: `verify_master_key` gate (401 without/with-wrong key, mirrors
`tests/test_download_security.py::TestApiAdminDownloadsRequiresMasterKey`),
`GET /servers`, `GET /catalog` (dedup + pagination), `GET
/catalog/{type}/{unification_id:path}` (detail + 404), and the DoD invariant
that NO response ever leaks `PlexServer.access_token`/`base_uri`.

Fixtures insert `PlexServer`/`PlexMediaItem` rows directly (no Plex sync
worker involved), same convention as `tests/test_admin_plex_downloads.py`
and `tests/test_plex_catalog.py`.
"""
from __future__ import annotations

from app.config import settings
from app.db import database as db_module
from app.models.database import PlexMediaItem, PlexServer
from app.services import api_key_service
from app.utils.server_id import build_plex_server_id
from app.utils.time import now_ms

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

MASTER_KEY = "master-secret-plex-downloads-json"

CID = "cid-mon-pms"
SERVER_ID = build_plex_server_id(CID)
SECRET_TOKEN = "super-secret-plex-token-xyz"
SECRET_BASE_URI = "https://10.9.9.9:32400"


def _server(
    cid: str = CID, *, name: str = "Mon PMS", owner_title: str | None = None,
    access_token: str = SECRET_TOKEN, base_uri: str = SECRET_BASE_URI, is_reachable: bool = True,
    last_synced_at: int | None = None,
) -> PlexServer:
    return PlexServer(
        client_identifier=cid, name=name, owner_title=owner_title, owned=owner_title is None,
        access_token=access_token, base_uri=base_uri, is_reachable=is_reachable,
        last_synced_at=last_synced_at, created_at=now_ms(), updated_at=now_ms(),
    )


def _movie(
    server_id: str = SERVER_ID, rating_key: str = "1001", *,
    title: str = "Blade Runner", year: int = 1982, unification_id: str = "imdb://tt0083658",
    height: int | None = 1080, part_size: int | None = 4_500_000_000,
    video_codec: str | None = "hevc", container: str | None = "mkv",
    added_at: int = 1_700_000_000_000,
) -> PlexMediaItem:
    return PlexMediaItem(
        server_id=server_id, rating_key=rating_key, type="movie", title=title, year=year,
        unification_id=unification_id, height=height, part_size=part_size,
        video_codec=video_codec, container=container, added_at=added_at, synced_at=now_ms(),
    )


def _show(
    server_id: str = SERVER_ID, rating_key: str = "2001", *,
    title: str = "Firefly", year: int = 2002, unification_id: str = "imdb://tt0303461",
    added_at: int = 1_700_000_000_000,
) -> PlexMediaItem:
    return PlexMediaItem(
        server_id=server_id, rating_key=rating_key, type="show", title=title, year=year,
        unification_id=unification_id, added_at=added_at, synced_at=now_ms(),
    )


def _wire_db(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)


def _configure_master(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER_KEY)


# ─── verify_master_key gate ─────────────────────────────────────────────────


class TestPlexDownloadsJsonRequiresMasterKey:
    """Mirrors `TestApiAdminDownloadsRequiresMasterKey` (Xtream download JSON
    mirror): ONLY `settings.AI_API_KEY` is accepted, never a per-user
    `api_keys` row."""

    async def test_servers_401_without_key(self, api_client, monkeypatch):
        _configure_master(monkeypatch)
        resp = await api_client.get("/api/admin/plex-downloads/servers")
        assert resp.status_code == 401

    async def test_servers_401_with_wrong_key(self, api_client, monkeypatch):
        _configure_master(monkeypatch)
        resp = await api_client.get(
            "/api/admin/plex-downloads/servers", headers={"X-API-Key": "definitely-wrong"},
        )
        assert resp.status_code == 401

    async def test_servers_not_401_with_master_key(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get(
            "/api/admin/plex-downloads/servers", headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code != 401

    async def test_catalog_401_without_key(self, api_client, monkeypatch):
        _configure_master(monkeypatch)
        resp = await api_client.get("/api/admin/plex-downloads/catalog")
        assert resp.status_code == 401

    async def test_catalog_detail_401_without_key(self, api_client, monkeypatch):
        _configure_master(monkeypatch)
        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog/movie/imdb%3A%2F%2Ftt0083658"
        )
        assert resp.status_code == 401

    async def test_catalog_401_with_a_valid_but_non_master_per_user_key(
        self, api_client, monkeypatch, db_factory,
    ):
        """A genuinely active per-user key (accepted by `verify_backend_secret`
        on /api/media, /api/accounts, etc.) must still be REJECTED here —
        same master-only design as the Xtream download JSON mirror."""
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
        async with db_factory() as s:
            _row, plaintext = await api_key_service.create_key(s, label="Per-user")

        resp_ok = await api_client.get("/api/accounts", headers={"X-API-Key": plaintext})
        assert resp_ok.status_code != 401

        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog", headers={"X-API-Key": plaintext},
        )
        assert resp.status_code == 401


# ─── GET /servers ────────────────────────────────────────────────────────────


class TestListServers:
    async def test_returns_known_servers(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            await s.commit()

        resp = await api_client.get(
            "/api/admin/plex-downloads/servers", headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["serverId"] == SERVER_ID
        assert item["clientIdentifier"] == CID
        assert item["name"] == "Mon PMS"
        assert item["owned"] is True
        assert item["isReachable"] is True

    async def test_empty_when_no_servers(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get(
            "/api/admin/plex-downloads/servers", headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0}

    async def test_never_leaks_access_token_or_base_uri(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            await s.commit()

        resp = await api_client.get(
            "/api/admin/plex-downloads/servers", headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        assert SECRET_TOKEN not in resp.text
        assert SECRET_BASE_URI not in resp.text
        assert "accessToken" not in resp.text
        assert "baseUri" not in resp.text


# ─── GET /catalog ────────────────────────────────────────────────────────────


class TestListCatalog:
    async def test_dedup_groups_by_unification_id(self, api_client, monkeypatch, db_factory):
        """Two `plex_media_item` rows (2 different servers) sharing the same
        `unification_id` collapse into ONE catalogue entry with
        `sourceCount=2`, and `sources` is left empty on the list endpoint."""
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server(CID, name="Serveur A"))
            s.add(_server("cid-b", name="Serveur B"))
            s.add(_movie(server_id=SERVER_ID, rating_key="1001"))
            s.add(_movie(
                server_id=build_plex_server_id("cid-b"), rating_key="9001",
                unification_id="imdb://tt0083658",
            ))
            await s.commit()

        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog", params={"type": "movie"},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["unificationId"] == "imdb://tt0083658"
        assert item["title"] == "Blade Runner"
        assert item["sourceCount"] == 2
        assert item["sources"] == []

    async def test_filters_by_type(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            s.add(_show())
            await s.commit()

        resp_movies = await api_client.get(
            "/api/admin/plex-downloads/catalog", params={"type": "movie"},
            headers={"X-API-Key": MASTER_KEY},
        )
        resp_shows = await api_client.get(
            "/api/admin/plex-downloads/catalog", params={"type": "show"},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert [it["type"] for it in resp_movies.json()["items"]] == ["movie"]
        assert [it["type"] for it in resp_shows.json()["items"]] == ["show"]

    async def test_pagination_limit_offset(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            for i in range(5):
                s.add(_movie(
                    rating_key=f"rk{i}", title=f"Film {i}", unification_id=f"imdb://tt{i:07d}",
                    added_at=1_700_000_000_000 + i,  # ascending -> most recent = i=4
                ))
            await s.commit()

        resp_all = await api_client.get(
            "/api/admin/plex-downloads/catalog",
            params={"type": "movie", "limit": 200, "offset": 0},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp_all.json()["total"] == 5
        assert len(resp_all.json()["items"]) == 5

        resp_page = await api_client.get(
            "/api/admin/plex-downloads/catalog",
            params={"type": "movie", "limit": 2, "offset": 0},
            headers={"X-API-Key": MASTER_KEY},
        )
        body_page = resp_page.json()
        assert body_page["total"] == 5
        assert len(body_page["items"]) == 2
        # Most recently added first (recency sort).
        assert body_page["items"][0]["title"] == "Film 4"
        assert body_page["items"][1]["title"] == "Film 3"

        resp_page2 = await api_client.get(
            "/api/admin/plex-downloads/catalog",
            params={"type": "movie", "limit": 2, "offset": 2},
            headers={"X-API-Key": MASTER_KEY},
        )
        body_page2 = resp_page2.json()
        assert len(body_page2["items"]) == 2
        assert body_page2["items"][0]["title"] == "Film 2"

    async def test_search_filters_by_title(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie(rating_key="1001", title="Blade Runner", unification_id="imdb://tt1"))
            s.add(_movie(rating_key="1002", title="The Matrix", unification_id="imdb://tt2"))
            await s.commit()

        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog",
            params={"type": "movie", "search": "blade"},
            headers={"X-API-Key": MASTER_KEY},
        )
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "Blade Runner"

    async def test_empty_catalog_returns_empty_list(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog", headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0}


# ─── GET /catalog/{type}/{unification_id:path} ──────────────────────────────


class TestCatalogDetail:
    async def test_detail_returns_hydrated_sources(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server(CID, name="Serveur A"))
            s.add(_movie(server_id=SERVER_ID, rating_key="1001", height=1080))
            await s.commit()

        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog/movie/imdb://tt0083658",
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["unificationId"] == "imdb://tt0083658"
        assert body["type"] == "movie"
        assert body["title"] == "Blade Runner"
        assert body["sourceCount"] == 1
        assert len(body["sources"]) == 1
        source = body["sources"][0]
        assert source["serverId"] == SERVER_ID
        assert source["ratingKey"] == "1001"
        assert source["serverName"] == "Serveur A"
        assert source["resolution"] == "1080p"
        assert source["videoCodec"] == "hevc"
        assert source["container"] == "mkv"

    async def test_path_converter_handles_literal_slashes_in_id(
        self, api_client, monkeypatch, db_factory,
    ):
        """`unification_id` embeds a literal `://` — the plain FastAPI
        single-segment converter would 404 on this without `:path`."""
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            await s.commit()

        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog/movie/imdb://tt0083658",
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200

    async def test_unknown_unification_id_404(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog/movie/imdb://tt9999999",
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 404

    async def test_unknown_type_404(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            await s.commit()

        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog/episode/imdb://tt0083658",
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 404

    async def test_never_leaks_access_token_or_base_uri(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            await s.commit()

        resp = await api_client.get(
            "/api/admin/plex-downloads/catalog/movie/imdb://tt0083658",
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        assert SECRET_TOKEN not in resp.text
        assert SECRET_BASE_URI not in resp.text
