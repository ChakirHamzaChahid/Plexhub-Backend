"""Tests for T10.2: GET /api/media/recent."""
from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import database as db_module
from app.models.database import Media, XtreamAccount
from app.utils.server_id import build_server_id
from app.utils.time import now_ms

API_KEY = "test-key-recent"
API_HEADERS = {"X-API-Key": API_KEY}


@pytest_asyncio.fixture
async def recent_client(db_session, monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    from app.main import app

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_module.get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(db_module.get_db, None)


async def test_recent_filters_sort_unification_outage_and_adult(recent_client, db_session):
    now = now_ms()
    day_ms = 86_400_000

    db_session.add_all([
        XtreamAccount(
            id="acc_ok", label="OK", base_url="http://ok.example",
            port=80, username="u", password="p", outage_strikes=0,
        ),
        XtreamAccount(
            id="acc_ok2", label="OK2", base_url="http://ok2.example",
            port=80, username="u", password="p", outage_strikes=0,
        ),
        XtreamAccount(
            id="acc_out", label="Out", base_url="http://out.example",
            port=80, username="u", password="p", outage_strikes=10,
        ),
    ])
    srv1 = build_server_id("acc_ok")
    srv2 = build_server_id("acc_ok2")
    srv_out = build_server_id("acc_out")

    db_session.add_all([
        # Movie A (2 versions, newest at now - 1h)
        Media(
            rating_key="m1_a", server_id=srv1, library_section_id="1", page_offset=1,
            title="Movie Newest", type="movie", tmdb_id="1001", unification_id="tmdb://1001",
            added_at=now - 3_600_000, is_in_allowed_categories=True, is_broken=False,
        ),
        Media(
            rating_key="m1_b", server_id=srv2, library_section_id="1", page_offset=2,
            title="Movie Newest", type="movie", tmdb_id="1001", unification_id="tmdb://1001",
            added_at=now - 7_200_000, is_in_allowed_categories=True, is_broken=False,
        ),
        # Movie B (2 days ago)
        Media(
            rating_key="m2", server_id=srv1, library_section_id="1", page_offset=3,
            title="Movie Older", type="movie", tmdb_id="1002", unification_id="tmdb://1002",
            added_at=now - 2 * day_ms, is_in_allowed_categories=True, is_broken=False,
        ),
        # Movie C (10 days ago -> outside days=7)
        Media(
            rating_key="m3", server_id=srv1, library_section_id="1", page_offset=4,
            title="Movie Too Old", type="movie", tmdb_id="1003", unification_id="tmdb://1003",
            added_at=now - 10 * day_ms, is_in_allowed_categories=True, is_broken=False,
        ),
        # Broken movie -> excluded
        Media(
            rating_key="m4", server_id=srv1, library_section_id="1", page_offset=5,
            title="Movie Broken", type="movie", tmdb_id="1004", unification_id="tmdb://1004",
            added_at=now - 1_000_000, is_in_allowed_categories=True, is_broken=True,
        ),
        # Disallowed category -> excluded
        Media(
            rating_key="m5", server_id=srv1, library_section_id="1", page_offset=6,
            title="Movie Disallowed", type="movie", tmdb_id="1005", unification_id="tmdb://1005",
            added_at=now - 1_000_000, is_in_allowed_categories=False, is_broken=False,
        ),
        # Outage server -> excluded
        Media(
            rating_key="m6", server_id=srv_out, library_section_id="1", page_offset=7,
            title="Movie Outage", type="movie", tmdb_id="1006", unification_id="tmdb://1006",
            added_at=now - 1_000_000, is_in_allowed_categories=True, is_broken=False,
        ),
        # Adult movie -> excluded unless includeAdult=true
        Media(
            rating_key="m7", server_id=srv1, library_section_id="1", page_offset=8,
            title="Adult Film", type="movie", tmdb_id="1007", unification_id="tmdb://1007",
            added_at=now - 500_000, is_in_allowed_categories=True, is_broken=False, is_adult=True,
        ),
        # Recent show -> excluded when type=movie, included when type=show or omitted
        Media(
            rating_key="s1", server_id=srv1, library_section_id="2", page_offset=9,
            title="Show Recent", type="show", tmdb_id="2001", unification_id="tmdb://2001",
            added_at=now - 1_800_000, is_in_allowed_categories=True, is_broken=False,
        ),
    ])
    await db_session.commit()

    # type=movie, days=7
    resp = await recent_client.get(
        "/api/media/recent?type=movie&days=7&limit=50", headers=API_HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [it["title"] for it in body["items"]] == ["Movie Newest", "Movie Older"]
    assert body["items"][0]["versionCount"] == 2
    assert body["total"] == 2
    assert body["hasMore"] is False

    # type=show
    resp_show = await recent_client.get(
        "/api/media/recent?type=show&days=7", headers=API_HEADERS
    )
    assert resp_show.status_code == 200
    assert [it["title"] for it in resp_show.json()["items"]] == ["Show Recent"]

    # type omitted + limit=2 -> Show Recent (30m ago), Movie Newest (1h ago), hasMore=True
    resp_mixed = await recent_client.get(
        "/api/media/recent?days=7&limit=2", headers=API_HEADERS
    )
    assert resp_mixed.status_code == 200
    mixed = resp_mixed.json()
    assert [it["title"] for it in mixed["items"]] == ["Show Recent", "Movie Newest"]
    assert mixed["total"] == 3
    assert mixed["hasMore"] is True

    # includeAdult=true
    resp_adult = await recent_client.get(
        "/api/media/recent?type=movie&days=7&includeAdult=true", headers=API_HEADERS
    )
    assert resp_adult.status_code == 200
    titles_adult = [it["title"] for it in resp_adult.json()["items"]]
    assert titles_adult[0] == "[XXX] Adult Film"
