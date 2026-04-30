"""Smoke tests for the /admin HTMX UI."""
from __future__ import annotations

import pytest

from app.db import database as db_module
from app.models.database import Media


pytestmark = pytest.mark.asyncio


async def test_admin_movies_missing_imdb_returns_200(
    monkeypatch, api_client, db_factory,
):
    """`GET /admin/movies?missing_imdb=true` must serve HTML even when DB is empty."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)

    resp = await api_client.get("/admin/movies?missing_imdb=true")
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

    resp = await api_client.get("/admin")
    assert resp.status_code == 200
    assert "Inception" in resp.text
    assert "Sans IMDb" in resp.text
    assert "Sans TMDB" in resp.text


async def test_admin_movies_missing_tmdb_returns_200(
    monkeypatch, api_client, db_factory,
):
    """`GET /admin/movies?missing_tmdb=true` serves HTML."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    resp = await api_client.get("/admin/movies?missing_tmdb=true")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
