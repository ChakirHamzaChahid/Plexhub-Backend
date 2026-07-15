"""tests/test_admin_plex_downloads.py -- Admin UI "Télécharger Plex" tab
(feature "Télécharger Plex", ticket C6, docs/10-prd-media-download.md).

Covers: Basic Auth gate (mirrors `/admin/downloads`), the browse
index/list/versions/episodes fragments (movie: per-source table; show: whole
-series + per-season + per-episode pickers), the enqueue POST for each scope
(movie/series_all/seasons/episodes), the catalogue-sync POST when the feature
is disabled, and the sync-status fragment -- plus the DoD invariant that NO
HTML response ever leaks a Plex `access_token`/`base_uri`.

Fixtures insert `PlexServer`/`PlexMediaItem` rows directly (no Plex sync
worker involved, same convention as `tests/test_plex_catalog.py` and
`tests/test_plex_download_enqueue.py`).
"""
from __future__ import annotations

from app.config import settings
from app.db import database as db_module
from app.models.database import PlexMediaItem, PlexServer
from app.services import download_service
from app.utils.server_id import build_plex_server_id
from app.utils.time import now_ms

# pytest-asyncio auto mode (pyproject.toml) -- async tests need no decorator.

ADMIN_USER = "admin"
ADMIN_PASS = "admin-pass-plex-downloads"

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


def _episode(
    server_id: str, show_rk: str, ep_rk: str, *, season: int, episode: int,
    title: str | None = None, height: int | None = 720, part_size: int | None = 900_000_000,
) -> PlexMediaItem:
    return PlexMediaItem(
        server_id=server_id, rating_key=ep_rk, type="episode",
        title=title or f"Episode {episode}", grandparent_rating_key=show_rk,
        parent_index=season, index=episode, height=height, part_size=part_size,
        synced_at=now_ms(),
    )


def _configure_admin(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)


def _wire_db(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)


# ─── Basic Auth gate ────────────────────────────────────────────────────────


class TestRequiresBasicAuth:
    async def test_index_401_without_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.get("/admin/plex-downloads")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate", "").lower().startswith("basic")
        assert "plex-downloads-list" not in resp.text

    async def test_index_403_or_401_with_wrong_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.get("/admin/plex-downloads", auth=(ADMIN_USER, "wrong"))
        assert resp.status_code == 401

    async def test_list_fragment_401_without_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.get("/admin/plex-downloads/list")
        assert resp.status_code == 401

    async def test_enqueue_401_without_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.post(
            "/admin/plex-downloads",
            data={"type": "movie", "unification_id": "x", "scope": "movie", "source": "a|b"},
        )
        assert resp.status_code == 401

    async def test_sync_status_401_without_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.get("/admin/plex-downloads/sync/status")
        assert resp.status_code == 401

    async def test_index_200_with_correct_credentials(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get("/admin/plex-downloads", auth=(ADMIN_USER, ADMIN_PASS))
        assert resp.status_code == 200


# ─── Index / list ───────────────────────────────────────────────────────────


class TestIndex:
    async def test_index_renders_title_and_catalogue(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            await s.commit()

        resp = await api_client.get("/admin/plex-downloads", auth=(ADMIN_USER, ADMIN_PASS))
        assert resp.status_code == 200
        assert "Télécharger Plex" in resp.text
        assert "Blade Runner" in resp.text
        assert "1 source" in resp.text

    async def test_index_feature_disabled_shows_warning(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        monkeypatch.setattr(settings, "PLEX_ACCOUNT_TOKEN", "")

        resp = await api_client.get("/admin/plex-downloads", auth=(ADMIN_USER, ADMIN_PASS))
        assert resp.status_code == 200
        assert "PLEX_ACCOUNT_TOKEN" in resp.text


class TestListFragment:
    async def test_list_fragment_filters_by_type(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            s.add(_show())
            await s.commit()

        resp = await api_client.get(
            "/admin/plex-downloads/list", params={"type": "movie"}, auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "Blade Runner" in resp.text
        assert "Firefly" not in resp.text

        resp2 = await api_client.get(
            "/admin/plex-downloads/list", params={"type": "show"}, auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert "Firefly" in resp2.text
        assert "Blade Runner" not in resp2.text

    async def test_list_fragment_search(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            await s.commit()

        resp = await api_client.get(
            "/admin/plex-downloads/list",
            params={"type": "movie", "search": "runner"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert "Blade Runner" in resp.text

        resp_miss = await api_client.get(
            "/admin/plex-downloads/list",
            params={"type": "movie", "search": "nomatch-xyz"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert "Blade Runner" not in resp_miss.text
        assert "Aucun titre trouvé" in resp_miss.text


# ─── Versions ───────────────────────────────────────────────────────────────


class TestVersionsMovie:
    async def test_movie_versions_render_sources(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            await s.commit()

        resp = await api_client.get(
            "/admin/plex-downloads/movie/imdb://tt0083658/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "Blade Runner" in resp.text
        assert "1080p" in resp.text
        assert "4.2 Go" in resp.text  # 4_500_000_000 bytes ~ 4.19 GiB
        assert "hevc" in resp.text
        assert "Mon PMS" in resp.text
        # enqueue form carries the structured source, not raw request data
        assert f'value="{SERVER_ID}|1001"' in resp.text
        assert 'name="scope" value="movie"' in resp.text

    async def test_unknown_unification_id_404(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get(
            "/admin/plex-downloads/movie/imdb://tt9999999/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 404

    async def test_unknown_type_404(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get(
            "/admin/plex-downloads/person/imdb://tt0083658/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 404


class TestVersionsShow:
    async def test_show_versions_render_whole_series_and_seasons(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_show())
            s.add(_episode(SERVER_ID, "2001", "ep-1", season=1, episode=1))
            s.add(_episode(SERVER_ID, "2001", "ep-2", season=1, episode=2))
            await s.commit()

        resp = await api_client.get(
            "/admin/plex-downloads/show/imdb://tt0303461/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "Firefly" in resp.text
        assert "Série complète" in resp.text
        assert f'value="{SERVER_ID}|2001"' in resp.text
        assert 'value="series_all"' in resp.text
        # per-season picker: one <select name="season_pick"> option per source
        assert 'name="season_pick"' in resp.text
        assert f"{1}|{SERVER_ID}|2001" in resp.text
        assert "2 ép." in resp.text
        # per-episode nav target for season 1
        assert 'id="plex-episodes-1"' in resp.text


class TestEpisodesFragment:
    async def test_episodes_fragment_renders_picker(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_show())
            s.add(_episode(SERVER_ID, "2001", "ep-1", season=1, episode=1, title="Serenity"))
            await s.commit()

        resp = await api_client.get(
            "/admin/plex-downloads/show/imdb://tt0303461/episodes",
            params={"season": 1},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "S01E01" in resp.text
        assert "Serenity" in resp.text
        assert f'value="{SERVER_ID}|ep-1"' in resp.text
        assert 'name="episode_pick"' in resp.text
        assert 'value="episodes"' in resp.text

    async def test_episodes_fragment_empty_season(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_show())
            await s.commit()

        resp = await api_client.get(
            "/admin/plex-downloads/show/imdb://tt0303461/episodes",
            params={"season": 9},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "Aucun épisode disponible" in resp.text


# ─── Enqueue ────────────────────────────────────────────────────────────────


class TestEnqueueMovie:
    async def test_enqueue_movie_creates_job(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            await s.commit()

        resp = await api_client.post(
            "/admin/plex-downloads",
            data={
                "type": "movie", "unification_id": "imdb://tt0083658", "scope": "movie",
                "source": f"{SERVER_ID}|1001",
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "1 job(s)" in resp.text
        assert "Blade Runner" in resp.text

        async with db_factory() as s:
            jobs, total = await download_service.list_jobs(s, limit=10, offset=0)
        assert total == 1
        assert jobs[0].server_id == SERVER_ID
        assert jobs[0].rating_key == "1001"
        assert jobs[0].media_type == "movie"

    async def test_enqueue_movie_malformed_source_errors(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)

        resp = await api_client.post(
            "/admin/plex-downloads",
            data={"type": "movie", "unification_id": "x", "scope": "movie", "source": "no-pipe-here"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "invalide" in resp.text.lower()

    async def test_enqueue_invalid_scope_errors(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)

        resp = await api_client.post(
            "/admin/plex-downloads",
            data={"type": "movie", "unification_id": "x", "scope": "bogus-scope", "source": "a|b"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "invalide" in resp.text.lower()


class TestEnqueueSeasons:
    async def test_enqueue_seasons_creates_episode_jobs(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_show())
            s.add(_episode(SERVER_ID, "2001", "ep-1", season=1, episode=1))
            s.add(_episode(SERVER_ID, "2001", "ep-2", season=1, episode=2))
            await s.commit()

        resp = await api_client.post(
            "/admin/plex-downloads",
            data={
                "type": "show", "unification_id": "imdb://tt0303461", "scope": "seasons",
                "season_pick": [f"1|{SERVER_ID}|2001"],
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "2 job(s)" in resp.text

        async with db_factory() as s:
            jobs, total = await download_service.list_jobs(s, limit=10, offset=0)
        assert total == 2
        assert {j.rating_key for j in jobs} == {"ep-1", "ep-2"}

    async def test_enqueue_seasons_ignores_blank_placeholder(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)

        resp = await api_client.post(
            "/admin/plex-downloads",
            data={
                "type": "show", "unification_id": "imdb://tt0303461", "scope": "seasons",
                "season_pick": [""],
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "aucune saison" in resp.text.lower() or "0 job(s)" in resp.text


class TestEnqueueEpisodes:
    async def test_enqueue_episodes_creates_job(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_server())
            s.add(_show())
            s.add(_episode(SERVER_ID, "2001", "ep-1", season=1, episode=1))
            await s.commit()

        resp = await api_client.post(
            "/admin/plex-downloads",
            data={
                "type": "show", "unification_id": "imdb://tt0303461", "scope": "episodes",
                "episode_pick": [f"{SERVER_ID}|ep-1"],
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "1 job(s)" in resp.text

        async with db_factory() as s:
            jobs, total = await download_service.list_jobs(s, limit=10, offset=0)
        assert total == 1
        assert jobs[0].rating_key == "ep-1"
        assert jobs[0].media_type == "episode"

    async def test_enqueue_episodes_empty_selection_errors(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)

        resp = await api_client.post(
            "/admin/plex-downloads",
            data={
                "type": "show", "unification_id": "imdb://tt0303461", "scope": "episodes",
                "episode_pick": [""],
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "aucun épisode" in resp.text.lower()


# ─── Catalogue sync ─────────────────────────────────────────────────────────


class TestSync:
    async def test_sync_feature_off_does_not_launch(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        monkeypatch.setattr(settings, "PLEX_ACCOUNT_TOKEN", "")

        resp = await api_client.post("/admin/plex-downloads/sync", auth=(ADMIN_USER, ADMIN_PASS))
        assert resp.status_code == 200
        assert "PLEX_ACCOUNT_TOKEN" in resp.text
        # no server/sync data rendered — nothing was launched
        assert "joignable" not in resp.text
        assert "synchronisation…" not in resp.text

    async def test_sync_status_fragment_idle_by_default(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)

        resp = await api_client.get(
            "/admin/plex-downloads/sync/status", auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert 'id="plex-sync-status"' in resp.text
        # not running -> no self-poll trigger
        assert "hx-trigger" not in resp.text


# ─── Secrets never leak (DoD: no access_token/base_uri in any HTML) ────────


class TestNoSecretLeak:
    async def _seed(self, db_factory):
        async with db_factory() as s:
            s.add(_server())
            s.add(_movie())
            s.add(_show())
            s.add(_episode(SERVER_ID, "2001", "ep-1", season=1, episode=1))
            await s.commit()

    async def test_index_never_leaks_token_or_base_uri(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        await self._seed(db_factory)

        resp = await api_client.get("/admin/plex-downloads", auth=(ADMIN_USER, ADMIN_PASS))
        assert resp.status_code == 200
        assert SECRET_TOKEN not in resp.text
        assert SECRET_BASE_URI not in resp.text

    async def test_versions_never_leak_token_or_base_uri(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        await self._seed(db_factory)

        for url in (
            "/admin/plex-downloads/movie/imdb://tt0083658/versions",
            "/admin/plex-downloads/show/imdb://tt0303461/versions",
        ):
            resp = await api_client.get(url, auth=(ADMIN_USER, ADMIN_PASS))
            assert resp.status_code == 200
            assert SECRET_TOKEN not in resp.text
            assert SECRET_BASE_URI not in resp.text

    async def test_episodes_never_leak_token_or_base_uri(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        await self._seed(db_factory)

        resp = await api_client.get(
            "/admin/plex-downloads/show/imdb://tt0303461/episodes",
            params={"season": 1},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert SECRET_TOKEN not in resp.text
        assert SECRET_BASE_URI not in resp.text

    async def test_sync_status_never_leaks_token_or_base_uri(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        await self._seed(db_factory)

        resp = await api_client.get(
            "/admin/plex-downloads/sync/status", auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert SECRET_TOKEN not in resp.text
        assert SECRET_BASE_URI not in resp.text

    async def test_queue_fragment_never_leaks_token_or_base_uri(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        await self._seed(db_factory)

        resp = await api_client.post(
            "/admin/plex-downloads",
            data={
                "type": "movie", "unification_id": "imdb://tt0083658", "scope": "movie",
                "source": f"{SERVER_ID}|1001",
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert SECRET_TOKEN not in resp.text
        assert SECRET_BASE_URI not in resp.text
