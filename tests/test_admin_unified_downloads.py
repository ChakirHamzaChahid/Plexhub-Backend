"""tests/test_admin_unified_downloads.py — Admin UI unified "Téléchargements"
tab (feature "écran de téléchargement unifié", Vague W3).

Covers: Basic Auth gate (mirrors the other two download tabs), the merged
browse index/list (a title present in BOTH Plex and Xtream shows once with both
origin badges), the genre filter over both catalogues, and the per-card origin
version loaders inlined in the list fragment (each present origin embeds its
existing `/versions` picker, fired by the `<details>` toggle).
Seeds `Media` (Xtream) + `PlexMediaItem`/`PlexServer` (Plex) rows directly.
"""
from __future__ import annotations

from app.config import settings
from app.db import database as db_module
from app.models.database import Media, PlexMediaItem, PlexServer
from app.utils.server_id import build_plex_server_id, build_server_id
from app.utils.time import now_ms

# pytest-asyncio auto mode (pyproject.toml).

ADMIN_USER = "admin"
ADMIN_PASS = "admin-pass-unified"

XTREAM_A = build_server_id("accA")
PLEX_CID = "cid-unified"
PLEX_SID = build_plex_server_id(PLEX_CID)

SHARED_UID = "imdb://tt0816692"  # Interstellar, present in BOTH sources


def _media(
    rating_key: str, *, title: str, unification_id: str, type: str = "movie",
    year: int | None = 2014, genres: str | None = None, page_offset: int = 0,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=XTREAM_A, filter="all", sort_order="default",
        library_section_id="xtream", title=title, type=type, year=year,
        unification_id=unification_id, genres=genres, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


def _plex(
    rating_key: str, *, title: str, unification_id: str, type: str = "movie",
    year: int | None = 2014, genres: str | None = None,
) -> PlexMediaItem:
    return PlexMediaItem(
        server_id=PLEX_SID, rating_key=rating_key, type=type, title=title, year=year,
        unification_id=unification_id, genres=genres, height=1080, part_size=4_000_000_000,
        container="mkv", added_at=now_ms(), synced_at=now_ms(),
    )


def _plex_server() -> PlexServer:
    return PlexServer(
        client_identifier=PLEX_CID, name="PMS", owned=True,
        access_token="secret-token", base_uri="https://10.0.0.9:32400",
        is_reachable=True, created_at=now_ms(), updated_at=now_ms(),
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
        resp = await api_client.get("/admin/unified-downloads")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate", "").lower().startswith("basic")

    async def test_list_401_without_credentials(self, api_client, monkeypatch):
        _configure_admin(monkeypatch)
        resp = await api_client.get("/admin/unified-downloads/list")
        assert resp.status_code == 401


# ─── Merged, deduplicated browse ────────────────────────────────────────────


class TestUnifiedBrowse:
    async def test_index_200_with_credentials(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        resp = await api_client.get("/admin/unified-downloads", auth=(ADMIN_USER, ADMIN_PASS))
        assert resp.status_code == 200
        assert "Téléchargements" in resp.text

    async def test_movie_in_both_sources_appears_once_with_both_badges(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add_all([
                _plex_server(),
                _media("x1", title="Interstellar", unification_id=SHARED_UID),
                _plex("p1", title="Interstellar", unification_id=SHARED_UID),
            ])
            await s.commit()

        resp = await api_client.get(
            "/admin/unified-downloads/list", params={"type": "movie"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        # One card, both origin badges, "2 sources".
        assert resp.text.count("Interstellar") == 1
        assert ">Plex<" in resp.text
        assert ">Xtream<" in resp.text
        assert "2 sources" in resp.text

    async def test_xtream_only_and_plex_only_both_listed(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add_all([
                _plex_server(),
                _media("x1", title="Xtream Only", unification_id="imdb://tt111"),
                _plex("p1", title="Plex Only", unification_id="imdb://tt222"),
            ])
            await s.commit()

        resp = await api_client.get(
            "/admin/unified-downloads/list", params={"type": "movie"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert "Xtream Only" in resp.text
        assert "Plex Only" in resp.text

    async def test_genre_filter_over_both_catalogues(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add_all([
                _plex_server(),
                _media("x1", title="Action Xtream", unification_id="imdb://tt1",
                       genres="Action, Thriller", page_offset=0),
                _media("x2", title="Comedy Xtream", unification_id="imdb://tt2",
                       genres="Comedy", page_offset=1),
                _plex("p1", title="Action Plex", unification_id="imdb://tt3", genres="Action"),
            ])
            await s.commit()

        resp = await api_client.get(
            "/admin/unified-downloads/list", params={"type": "movie", "genre": "Action"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert "Action Xtream" in resp.text
        assert "Action Plex" in resp.text
        assert "Comedy Xtream" not in resp.text

    async def test_series_dedup(self, api_client, monkeypatch, db_factory):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add_all([
                _plex_server(),
                _media("x1", type="show", title="Firefly", unification_id="imdb://tt0303461"),
                _plex("p1", type="show", title="Firefly", unification_id="imdb://tt0303461"),
            ])
            await s.commit()

        resp = await api_client.get(
            "/admin/unified-downloads/list", params={"type": "show"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.text.count("Firefly") == 1
        assert "2 sources" in resp.text


# ─── Per-card origin version loaders (inlined in the list fragment) ──────────


class TestOriginVersionLoaders:
    """Each merged card embeds, per present origin, a lazy loader for that
    origin's EXISTING `/versions` fragment — fired by the ``<details>``'s real
    ``toggle`` event (``toggle once from:closest details``), never a
    swap-time auto-trigger (see the router docstring / the browser-verified
    htmx descendant-target quirk)."""

    async def test_list_embeds_both_origin_version_loaders(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add_all([
                _plex_server(),
                _media("x1", title="Interstellar", unification_id=SHARED_UID),
                _plex("p1", title="Interstellar", unification_id=SHARED_UID),
            ])
            await s.commit()

        resp = await api_client.get(
            "/admin/unified-downloads/list", params={"type": "movie"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        # Each present origin embeds its EXISTING version picker endpoint,
        # fired by the details toggle (not a swap-time auto-trigger).
        assert f"/admin/downloads/movie/{SHARED_UID}/versions" in resp.text
        assert f"/admin/plex-downloads/movie/{SHARED_UID}/versions" in resp.text
        assert "toggle once from:closest details" in resp.text

    async def test_list_only_present_origin_loader(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add_all([
                _plex_server(),
                _plex("p1", title="Plex Only", unification_id="imdb://tt999"),
            ])
            await s.commit()

        resp = await api_client.get(
            "/admin/unified-downloads/list", params={"type": "movie"},
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert resp.status_code == 200
        assert "/admin/plex-downloads/movie/imdb://tt999/versions" in resp.text
        assert "/admin/downloads/movie/imdb://tt999/versions" not in resp.text

    async def test_no_secret_leaks_in_any_response(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add_all([
                _plex_server(),
                _plex("p1", title="Interstellar", unification_id=SHARED_UID),
            ])
            await s.commit()

        for url in (
            "/admin/unified-downloads",
            "/admin/unified-downloads/list?type=movie",
        ):
            resp = await api_client.get(url, auth=(ADMIN_USER, ADMIN_PASS))
            assert "secret-token" not in resp.text
            assert "10.0.0.9" not in resp.text


    async def test_id_filter_and_plex_server_filter(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        monkeypatch.setattr(settings, "PLEX_ACCOUNT_TOKEN", "plex-acct-tok")
        async with db_factory() as s:
            s.add_all([
                _plex_server(),
                _plex("p1", title="Interstellar", unification_id=SHARED_UID),
                _media("x1", title="Local Only", unification_id="title_local_only"),
            ])
            await s.commit()

        # Index exposes Plex sync button + Plex server option
        idx = await api_client.get("/admin/unified-downloads", auth=(ADMIN_USER, ADMIN_PASS))
        assert idx.status_code == 200
        assert "Synchroniser Plex" in idx.text
        assert PLEX_SID in idx.text

        # id_filter=imdb keeps Interstellar, excludes title_local_only
        r_imdb = await api_client.get(
            "/admin/unified-downloads/list?type=movie&id_filter=imdb",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert r_imdb.status_code == 200
        assert "Interstellar" in r_imdb.text
        assert "Local Only" not in r_imdb.text

        # id_filter=none keeps only title_local_only
        r_none = await api_client.get(
            "/admin/unified-downloads/list?type=movie&id_filter=none",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert r_none.status_code == 200
        assert "Local Only" in r_none.text
        assert "Interstellar" not in r_none.text

        # plex_server_id filters to media present on that Plex server
        r_srv = await api_client.get(
            f"/admin/unified-downloads/list?type=movie&plex_server_id={PLEX_SID}",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert r_srv.status_code == 200
        assert "Interstellar" in r_srv.text
        assert "Local Only" not in r_srv.text


    async def test_date_year_and_sort_filters(
        self, api_client, monkeypatch, db_factory,
    ):
        from app.utils.time import now_ms

        _configure_admin(monkeypatch)
        _wire_db(monkeypatch, db_factory)
        recent_ms = now_ms() - 2 * 86_400_000   # 2 days ago
        old_ms = now_ms() - 60 * 86_400_000     # 60 days ago

        m_new = _media("x_new", title="Recent Movie", year=2026, unification_id="tmdb://901")
        m_new.added_at = recent_ms
        m_old = _media("x_old", title="Older Movie", year=2020, unification_id="tmdb://902")
        m_old.added_at = old_ms
        m_old.page_offset = 1
        async with db_factory() as s:
            s.add_all([m_new, m_old])
            await s.commit()

        r_days = await api_client.get(
            "/admin/unified-downloads/list?type=movie&days=7",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert r_days.status_code == 200
        assert "Recent Movie" in r_days.text
        assert "Older Movie" not in r_days.text

        r_year = await api_client.get(
            "/admin/unified-downloads/list?type=movie&year=2020",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert r_year.status_code == 200
        assert "Older Movie" in r_year.text
        assert "Recent Movie" not in r_year.text

        r_sort = await api_client.get(
            "/admin/unified-downloads/list?type=movie&sort=added_desc",
            auth=(ADMIN_USER, ADMIN_PASS),
        )
        assert r_sort.status_code == 200
        assert r_sort.text.index("Recent Movie") < r_sort.text.index("Older Movie")
