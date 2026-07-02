"""Smoke tests for the /admin HTMX UI."""
from __future__ import annotations

import pytest

from app.config import settings
from app.db import database as db_module
from app.models.database import Media


pytestmark = pytest.mark.asyncio

# /admin is HTTP Basic-Auth gated (503 when ADMIN_PASSWORD is empty), so set
# known credentials and send them with every request.
ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-pass"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)


@pytest.fixture(autouse=True)
def _admin_creds(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)


async def test_admin_movies_missing_imdb_returns_200(
    monkeypatch, api_client, db_factory,
):
    """`GET /admin/movies?missing_imdb=true` must serve HTML even when DB is empty."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)

    resp = await api_client.get("/admin/movies?missing_imdb=true", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Aucun résultat" in resp.text


async def test_admin_index_renders_with_seeded_movie(
    monkeypatch, api_client, db_factory,
):
    """`GET /admin` lists a seeded movie and shows IMDb + TMDB counters."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)

    async with db_factory() as session:
        session.add(Media(
            rating_key="rk-1", server_id="srv-1",
            filter="all", sort_order="default",
            library_section_id="lib-1", title="Inception",
            type="movie", year=2010,
            added_at=1, updated_at=1,
        ))
        await session.commit()

    resp = await api_client.get("/admin", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert "Inception" in resp.text
    assert "Sans IMDb" in resp.text
    assert "Sans TMDB" in resp.text


async def test_admin_movies_missing_tmdb_returns_200(
    monkeypatch, api_client, db_factory,
):
    """`GET /admin/movies?missing_tmdb=true` serves HTML."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    resp = await api_client.get("/admin/movies?missing_tmdb=true", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
