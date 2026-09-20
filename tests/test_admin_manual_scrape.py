"""HTTP-level smoke tests for the W2 manual-scraper admin routes
(ADR 0005 D10): POST /admin/media/{sid}/{rk}/{apply,unlock,clear,rescrape}.

Network is mocked by monkeypatching the `manual_scrape_service` module's
`tmdb_service`/`omdb_service` singletons (same convention as
`tests/test_manual_scrape_apply.py`).
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.db import database as db_module
from app.models.database import Media
from app.services import manual_scrape_service as mss
from app.services import unified_group_service
from tests.test_manual_scrape_apply import FakeOMDb, FakeTMDB, _details

pytestmark = pytest.mark.asyncio

ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-pass-manual-scrape"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)


@pytest.fixture(autouse=True)
def _admin_creds(monkeypatch, db_factory):
    from app.api import admin as admin_module

    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    # `admin.py` binds `async_session_factory` at import time (`from
    # app.db.database import async_session_factory, get_db`) — a direct
    # `Depends(get_db)` route picks up a patched `db_module.
    # async_session_factory` at call time, but the manual-scrape routes'
    # OWN reference to the name does not, so it needs patching separately.
    monkeypatch.setattr(admin_module, "async_session_factory", db_factory)
    yield
    unified_group_service._pending_rebuild_tasks.clear()
    unified_group_service._pending_session_factories.clear()
    unified_group_service._running_rebuild_tasks.clear()


async def test_apply_by_tmdb_id_renders_applied_row(api_client, db_factory, monkeypatch):
    async with db_factory() as s:
        s.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="The Matrix", type="movie", year=1999, page_offset=0,
        ))
        await s.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/apply",
        data={"tmdb_id": "603", "type": "movie"},
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("HX-Trigger") == "refresh-stats"
    assert "tt0133093" in resp.text
    assert "verrouill" in resp.text.lower()


async def test_apply_missing_both_ids_returns_422(api_client, db_factory, monkeypatch):
    async with db_factory() as s:
        s.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="No Ids Given", type="movie", year=1999, page_offset=0,
        ))
        await s.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB())
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/apply",
        data={"type": "movie"},
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 422


async def test_apply_conflict_returns_409(api_client, db_factory, monkeypatch):
    async with db_factory() as s:
        s.add_all([
            Media(
                rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
                title="Target", type="movie", year=1999, page_offset=0,
            ),
            Media(
                rating_key="vod_other.mp4", server_id="xtream_a", library_section_id="1",
                title="Other", type="movie", year=1999, page_offset=1,
                tmdb_id="603", imdb_id="tt7777777",
            ),
        ])
        await s.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/apply",
        data={"tmdb_id": "603", "type": "movie"},
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 409


async def test_apply_unknown_row_returns_404(api_client, monkeypatch):
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    resp = await api_client.post(
        "/admin/media/xtream_a/nope/apply",
        data={"tmdb_id": "603", "type": "movie"},
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404


async def test_unlock_renders_unlocked_row(api_client, db_factory):
    async with db_factory() as s:
        s.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Locked", type="movie", year=1999, page_offset=0,
            match_locked=True, match_source="manual",
        ))
        await s.commit()

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/unlock", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("HX-Trigger") == "refresh-stats"

    async with db_factory() as s:
        row = await mss.load_row("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert row.match_locked is False


async def test_unlock_unknown_row_returns_404(api_client):
    resp = await api_client.post(
        "/admin/media/xtream_a/nope/unlock", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404


async def test_clear_renders_cleared_row(api_client, db_factory):
    async with db_factory() as s:
        s.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Some Movie", type="movie", year=2020, page_offset=0,
            tmdb_id="603", imdb_id="tt0133093",
        ))
        await s.commit()

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/clear", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200, resp.text

    row = await mss.load_row("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert row.tmdb_id is None
    assert row.imdb_id is None


async def test_rescrape_locked_shows_locked_state(api_client, db_factory):
    async with db_factory() as s:
        s.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Locked", type="movie", year=1999, page_offset=0,
            match_locked=True, match_source="manual",
        ))
        await s.commit()

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/rescrape", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert "verrouill" in resp.text.lower()


async def test_rescrape_unlocked_queues(api_client, db_factory):
    async with db_factory() as s:
        s.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Free", type="movie", year=1999, page_offset=0,
        ))
        await s.commit()

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/rescrape", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("HX-Trigger") == "refresh-stats"


async def test_unauthenticated_apply_is_rejected(api_client):
    """/admin is Basic-Auth gated — no credentials -> not a 200 row render."""
    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/apply",
        data={"tmdb_id": "603", "type": "movie"},
    )
    assert resp.status_code in (401, 403)


async def test_error_responses_are_rendered_not_swallowed(
    api_client, db_factory, monkeypatch,
):
    """ADR 0005 W2 review follow-up: htmx 1.x performs NO swap on a 4xx, so a
    409/422 answer would leave the screen unchanged and the Appliquer button
    looking dead. Both routes answer with the row fragment carrying the
    message, and the admin layout opts those two codes back in."""
    async with db_factory() as s:
        s.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Target", type="movie", year=1999, page_offset=0,
        ))
        await s.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/apply",
        data={"type": "movie"}, auth=ADMIN_AUTH,
    )
    assert resp.status_code == 422
    assert 'id="row-xtream_a-vod_1.mp4"' in resp.text

    layout = await api_client.get("/admin", auth=ADMIN_AUTH)
    assert layout.status_code == 200
    assert "htmx:beforeSwap" in layout.text, (
        "without this handler htmx drops every 4xx and the operator sees nothing"
    )
    assert "409" in layout.text and "422" in layout.text
