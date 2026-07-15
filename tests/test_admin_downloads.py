"""tests/test_admin_downloads.py — Admin UI "Télécharger" tab (Xtream
-sourced physical media download), ticket X3 (granular per-episode/per
-season download with per-unit source choice + size display).

Mirrors `tests/test_admin_plex_downloads.py` (the Plex-sourced twin of this
router) 1:1 for the browse/enqueue shape: movie versions now show a size
column, and a show offers "Série entière"/"Par saison"/"Par épisode" source
pickers (the last two letting the operator choose a DIFFERENT source per
season/episode). Basic Auth rejection + credential-leak invariants are
already covered end-to-end by `tests/test_download_security.py` — this file
focuses on the X3 browse/enqueue behaviour itself (still re-asserts one 401
per new/changed route as a cheap regression guard).

Fixtures insert `Media`/`XtreamAccount` rows directly (no sync worker
involved), same convention as `tests/test_download_service.py` and
`tests/test_download_granular.py` (whose size-estimate math this file's
fixtures deliberately reuse).
"""
from __future__ import annotations

import json

from app.config import settings
from app.db import database as db_module
from app.models.database import Media, XtreamAccount
from app.services import download_service
from app.utils.server_id import build_server_id

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

ADMIN_USER = "admin"
ADMIN_PASS = "admin-pass-downloads-x3"

ACCOUNT_A = "accA"
ACCOUNT_B = "accB"
SERVER_A = build_server_id(ACCOUNT_A)
SERVER_B = build_server_id(ACCOUNT_B)

MOVIE_UNI_ID = "imdb://tt0088247"
SHOW_UNI_ID = "imdb://tt0303461"


def _account(
    account_id: str = ACCOUNT_A, *, label: str = "Compte A", active: bool = True,
) -> XtreamAccount:
    return XtreamAccount(
        id=account_id, label=label, base_url="http://provider.example", port=80,
        username="u", password="p", is_active=active, created_at=0,
    )


def _movie(
    rating_key: str = "1001", server_id: str = SERVER_A, *,
    title: str = "Terminator", year: int = 1984, unification_id: str = MOVIE_UNI_ID,
    file_size: int | None = 4_500_000_000, media_parts: str = "[]",
    duration: int | None = None, is_broken: bool = False,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id, filter="all", sort_order="default",
        library_section_id="xtream_vod", title=title, type="movie", year=year,
        unification_id=unification_id, is_in_allowed_categories=True, is_broken=is_broken,
        file_size=file_size, media_parts=media_parts, duration=duration,
    )


def _show(
    rating_key: str = "2001", server_id: str = SERVER_A, *,
    title: str = "Firefly", year: int = 2002, unification_id: str = SHOW_UNI_ID,
    page_offset: int = 0,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id, filter="all", sort_order="default",
        library_section_id="xtream_series", title=title, type="show", year=year,
        unification_id=unification_id, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


def _episode(
    rating_key: str, show_rk: str, server_id: str = SERVER_A, *, season: int, episode: int,
    title: str = "Ep", page_offset: int = 0,
    file_size: int | None = None, media_parts: str = "[]", duration: int | None = None,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id, filter="all", sort_order="default",
        library_section_id="xtream_series", title=title, type="episode",
        grandparent_rating_key=show_rk, parent_index=season, index=episode,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
        file_size=file_size, media_parts=media_parts, duration=duration,
    )


def _video_media_parts(bitrate_bps: int) -> str:
    """Mirror the JSON shape `sync_worker._build_media_parts` writes (see
    `tests/test_download_granular.py::_video_media_parts`)."""
    return json.dumps([{
        "id": "x", "key": "/stream/x", "duration": None, "file": None, "size": None,
        "container": "mkv",
        "streams": [{
            "type": "VideoStream", "id": "0", "index": 0, "codec": "h264",
            "width": 1920, "height": 1080, "bitrate": bitrate_bps,
            "selected": True, "hasHDR": False,
        }],
    }])


def _configure_admin(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)


def _wire_db(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)


# ─── Basic Auth gate (cheap regression guard for the two NEW/changed routes) ─


class TestRequiresBasicAuth:
    async def test_versions_401_without_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.get(f"/admin/downloads/movie/{MOVIE_UNI_ID}/versions")
        assert resp.status_code == 401

    async def test_episodes_401_without_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.get(
            f"/admin/downloads/show/{SHOW_UNI_ID}/episodes", params={"season": 1},
        )
        assert resp.status_code == 401

    async def test_enqueue_401_without_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.post(
            "/admin/downloads",
            data={"type": "movie", "unification_id": MOVIE_UNI_ID, "scope": "movie",
                  "source": f"{SERVER_A}|1001"},
        )
        assert resp.status_code == 401


# ─── Versions: movie (per-source size) ──────────────────────────────────────


class TestVersionsMovie:
    async def test_movie_versions_render_size_and_source(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_movie())
            await s.commit()

        resp = await api_client.get(
            f"/admin/downloads/movie/{MOVIE_UNI_ID}/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "Terminator" in resp.text
        assert "4.2 Go" in resp.text  # 4_500_000_000 bytes ~ 4.19 GiB, exact (file_size set)
        assert "Compte A" in resp.text
        # enqueue form carries the structured source, not raw request data
        assert f'value="{SERVER_A}|1001"' in resp.text
        assert 'name="scope" value="movie"' in resp.text
        assert "ok" in resp.text  # not broken

    async def test_movie_versions_size_estimated_shows_tilde(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            # No file_size -> falls back to bitrate*duration estimate
            # (4 Mbps, 20 min -> 600_000_000 bytes ~ 572.2 MiB).
            s.add(_movie(
                file_size=None, media_parts=_video_media_parts(4_000_000), duration=1_200_000,
            ))
            await s.commit()

        resp = await api_client.get(
            f"/admin/downloads/movie/{MOVIE_UNI_ID}/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "572.2 Mo" in resp.text
        assert "~" in resp.text  # estimated-size marker

    async def test_movie_versions_broken_source_shows_badge(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_movie(is_broken=True))
            await s.commit()

        resp = await api_client.get(
            f"/admin/downloads/movie/{MOVIE_UNI_ID}/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "cassé" in resp.text

    async def test_unknown_unification_id_404(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get(
            "/admin/downloads/movie/imdb://tt9999999/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 404

    async def test_unknown_type_404(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get(
            f"/admin/downloads/person/{MOVIE_UNI_ID}/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 404


# ─── Versions: show (whole-series + per-season/per-episode source pickers) ──


class TestVersionsShow:
    async def test_show_versions_render_whole_series_and_season_picker(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_show())
            s.add(_episode("ep-1", "2001", season=1, episode=1, page_offset=1,
                            file_size=500_000_000))
            s.add(_episode("ep-2", "2001", season=1, episode=2, page_offset=2,
                            file_size=600_000_000))
            await s.commit()

        resp = await api_client.get(
            f"/admin/downloads/show/{SHOW_UNI_ID}/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "Firefly" in resp.text
        assert "Série complète" in resp.text
        assert f'value="{SERVER_A}|2001"' in resp.text
        assert 'value="series_all"' in resp.text
        # whole-series sources carry no size (per-episode granularity below).
        assert "Aucune version disponible." not in resp.text

        # per-season picker: one <select name="season_pick"> option per source
        assert 'name="season_pick"' in resp.text
        assert f"1|{SERVER_A}|2001" in resp.text
        assert "2 ép." in resp.text
        assert "1.0 Go" in resp.text  # 500M + 600M = 1_100_000_000 bytes ~ 1.02 GiB

        # per-episode nav target for season 1
        assert 'id="dl-episodes-1"' in resp.text
        assert (
            f"/admin/downloads/show/{SHOW_UNI_ID}/episodes?season=1" in resp.text
        )

    async def test_show_versions_season_size_estimated_shows_tilde(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_show())
            s.add(_episode(
                "ep-1", "2001", season=1, episode=1, page_offset=1,
                media_parts=_video_media_parts(4_000_000), duration=1_200_000,
            ))
            await s.commit()

        resp = await api_client.get(
            f"/admin/downloads/show/{SHOW_UNI_ID}/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "572.2 Mo" in resp.text
        assert "~" in resp.text

    async def test_show_with_no_episodes_shows_no_seasons(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_show())
            await s.commit()

        resp = await api_client.get(
            f"/admin/downloads/show/{SHOW_UNI_ID}/versions",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "Aucune saison disponible." in resp.text


class TestEpisodesFragment:
    async def test_episodes_fragment_renders_picker_with_size(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_show())
            s.add(_episode(
                "ep-1", "2001", season=1, episode=1, page_offset=1,
                title="Serenity", file_size=500_000_000,
            ))
            await s.commit()

        resp = await api_client.get(
            f"/admin/downloads/show/{SHOW_UNI_ID}/episodes",
            params={"season": 1},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "S01E01" in resp.text
        assert "Serenity" in resp.text
        assert f'value="{SERVER_A}|ep-1"' in resp.text
        assert 'name="episode_pick"' in resp.text
        assert 'value="episodes"' in resp.text
        assert "476.8 Mo" in resp.text  # 500_000_000 bytes ~ 476.84 MiB, exact

    async def test_episodes_fragment_empty_season(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_show())
            await s.commit()

        resp = await api_client.get(
            f"/admin/downloads/show/{SHOW_UNI_ID}/episodes",
            params={"season": 9},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "Aucun épisode disponible" in resp.text


# ─── Enqueue ─────────────────────────────────────────────────────────────────


class TestEnqueueMovie:
    async def test_enqueue_movie_creates_job(self, api_client, monkeypatch, db_factory, download_dir):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_movie())
            await s.commit()

        resp = await api_client.post(
            "/admin/downloads",
            data={
                "type": "movie", "unification_id": MOVIE_UNI_ID, "scope": "movie",
                "source": f"{SERVER_A}|1001",
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "1 job(s)" in resp.text
        assert "Terminator" in resp.text

        async with db_factory() as s:
            jobs, total = await download_service.list_jobs(s, limit=10, offset=0)
        assert total == 1
        assert jobs[0].server_id == SERVER_A
        assert jobs[0].rating_key == "1001"
        assert jobs[0].media_type == "movie"

    async def test_enqueue_movie_malformed_source_errors(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)

        resp = await api_client.post(
            "/admin/downloads",
            data={"type": "movie", "unification_id": "x", "scope": "movie",
                  "source": "no-pipe-here"},
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
            "/admin/downloads",
            data={"type": "movie", "unification_id": "x", "scope": "bogus-scope",
                  "source": f"{SERVER_A}|1001"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "invalide" in resp.text.lower()

    async def test_enqueue_invalid_type_errors(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)

        resp = await api_client.post(
            "/admin/downloads",
            data={"type": "bogus-type", "unification_id": "x", "scope": "movie",
                  "source": f"{SERVER_A}|1001"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "invalide" in resp.text.lower()

    async def test_enqueue_series_all_creates_episode_jobs(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_show())
            s.add(_episode("ep-1", "2001", season=1, episode=1, page_offset=1))
            s.add(_episode("ep-2", "2001", season=2, episode=1, page_offset=2))
            await s.commit()

        resp = await api_client.post(
            "/admin/downloads",
            data={
                "type": "show", "unification_id": SHOW_UNI_ID, "scope": "series_all",
                "source": f"{SERVER_A}|2001",
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "2 job(s)" in resp.text

        async with db_factory() as s:
            jobs, total = await download_service.list_jobs(s, limit=10, offset=0)
        assert total == 2
        assert {j.rating_key for j in jobs} == {"ep-1", "ep-2"}


class TestEnqueueSeasons:
    async def test_enqueue_seasons_creates_episode_jobs(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_show())
            s.add(_episode("ep-1", "2001", season=1, episode=1, page_offset=1))
            s.add(_episode("ep-2", "2001", season=1, episode=2, page_offset=2))
            await s.commit()

        resp = await api_client.post(
            "/admin/downloads",
            data={
                "type": "show", "unification_id": SHOW_UNI_ID, "scope": "seasons",
                "season_pick": [f"1|{SERVER_A}|2001"],
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
            "/admin/downloads",
            data={
                "type": "show", "unification_id": SHOW_UNI_ID, "scope": "seasons",
                "season_pick": [""],
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "aucune saison" in resp.text.lower()


class TestEnqueueEpisodes:
    async def test_enqueue_episodes_creates_job(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_show())
            s.add(_episode("ep-1", "2001", season=1, episode=1, page_offset=1))
            await s.commit()

        resp = await api_client.post(
            "/admin/downloads",
            data={
                "type": "show", "unification_id": SHOW_UNI_ID, "scope": "episodes",
                "episode_pick": [f"{SERVER_A}|ep-1"],
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

    async def test_enqueue_episodes_multi_account_one_batch(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add_all([
                _account(ACCOUNT_A, label="Compte A"),
                _account(ACCOUNT_B, label="Compte B"),
                _show("2001", SERVER_A, unification_id=SHOW_UNI_ID),
                _show("3001", SERVER_B, unification_id=SHOW_UNI_ID),
                _episode("ep-a1", "2001", SERVER_A, season=1, episode=1, page_offset=1),
                _episode("ep-b1", "3001", SERVER_B, season=1, episode=1, page_offset=1),
            ])
            await s.commit()

        resp = await api_client.post(
            "/admin/downloads",
            data={
                "type": "show", "unification_id": SHOW_UNI_ID, "scope": "episodes",
                "episode_pick": [f"{SERVER_A}|ep-a1", f"{SERVER_B}|ep-b1"],
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "2 job(s)" in resp.text

        async with db_factory() as s:
            jobs, total = await download_service.list_jobs(s, limit=10, offset=0)
        assert total == 2
        assert {j.server_id for j in jobs} == {SERVER_A, SERVER_B}

    async def test_enqueue_episodes_empty_selection_errors(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)

        resp = await api_client.post(
            "/admin/downloads",
            data={
                "type": "show", "unification_id": SHOW_UNI_ID, "scope": "episodes",
                "episode_pick": [""],
            },
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "aucun épisode" in resp.text.lower()
