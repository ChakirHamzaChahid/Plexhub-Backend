"""Integration tests for POST /api/ai/search (F2 — semantic search).

Uses an ASGI in-process client backed by an isolated sqlite-vec engine (M008).
embedding_service.embed_query and ollama_service.generate are monkeypatched so
tests stay fully offline.

Fixtures follow the same pattern as tests/test_ai_rank.py.
"""
from __future__ import annotations

import struct
from typing import AsyncIterator

import httpx
import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import _VEC_LOADED, register_sqlite_vec_listener
from app.db.migrations import _migration_008_ai_embeddings
from app.services import embedding_service
from app.services.embedding_service import EMBEDDING_DIM
from app.services import ollama_service

pytestmark = pytest.mark.asyncio


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _unit_vec(idx: int) -> list[float]:
    """Return a unit vector with a 1.0 at position idx % EMBEDDING_DIM."""
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    arr[idx % EMBEDDING_DIM] = 1.0
    return arr.tolist()


def _serialize_vec(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


async def _seed_row(
    session: AsyncSession,
    *,
    tmdb_id: int,
    media_type: str,
    title: str,
    vec: list[float],
) -> None:
    """Insert one row into both ai_tmdb_cache and ai_embeddings."""
    now_ms = 1_700_000_000_000
    await session.execute(
        text(
            "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, "
            "overview, genres, fetched_at, embedded_at) "
            "VALUES(:tmdb_id, NULL, :media_type, :title, NULL, NULL, :now, :now)"
        ),
        {"tmdb_id": tmdb_id, "media_type": media_type, "title": title, "now": now_ms},
    )
    await session.execute(
        text("INSERT INTO ai_embeddings(tmdb_id, embedding) VALUES(:tid, :v)"),
        {"tid": tmdb_id, "v": _serialize_vec(vec)},
    )
    await session.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def search_engine(tmp_path):
    """Isolated file-backed sqlite-vec engine with M008 applied."""
    db_path = tmp_path / "search_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def search_factory(search_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(search_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def search_client(
    search_engine,
    search_factory,
    monkeypatch,
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to the isolated AI engine; X-API-Key pre-seeded."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", "test-key-search")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    from app.main import app
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", search_factory)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with search_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[db_module.get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(db_module.get_db, None)


# Convenience fixture: client + 3 seeded rows (movie ×2, tv ×1).
# Vectors chosen so that a query vec at index 0 ranks tmdb_id=10 first.
@pytest_asyncio.fixture
async def seeded_client(search_client, search_factory, monkeypatch):
    """Seed rows and monkeypatch embed_query + ollama generate; return client."""
    async with search_factory() as session:
        # tmdb_id=10: movie, vec at index 0 (most similar to query)
        await _seed_row(session, tmdb_id=10, media_type="movie", title="Inception", vec=_unit_vec(0))
        # tmdb_id=20: movie, vec at index 1 (orthogonal to query)
        await _seed_row(session, tmdb_id=20, media_type="movie", title="Drôle de film", vec=_unit_vec(1))
        # tmdb_id=30: tv, vec at index 2 (orthogonal to query)
        await _seed_row(session, tmdb_id=30, media_type="tv", title="Dark", vec=_unit_vec(2))

    # Monkeypatch embed_query to return the query vector at index 0 (closest to tmdb_id=10).
    async def fake_embed_query(text: str) -> list[float]:
        return _unit_vec(0)

    monkeypatch.setattr(embedding_service, "_model", object())  # mark model as loaded
    monkeypatch.setattr(embedding_service, "embed_query", fake_embed_query)

    # Default: Ollama reformulation returns a modified query string.
    async def fake_generate(prompt: str) -> str:
        return "A mind-bending thriller with comedic twists"

    monkeypatch.setattr(ollama_service, "generate", fake_generate)

    return search_client


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

async def test_missing_api_key_returns_401(search_client):
    """No X-API-Key header must return 401."""
    resp = await search_client.post(
        "/api/ai/search",
        json={"query": "des films comme Inception"},
    )
    assert resp.status_code == 401


async def test_empty_query_returns_422(search_client, monkeypatch):
    """Empty string violates min_length=1 -> 422 Unprocessable Entity."""
    resp = await search_client.post(
        "/api/ai/search",
        headers={"X-API-Key": "test-key-search"},
        json={"query": ""},
    )
    assert resp.status_code == 422


async def test_known_row_ranks_first(seeded_client):
    """Query vector at index 0 should rank tmdb_id=10 (Inception) first."""
    resp = await seeded_client.post(
        "/api/ai/search",
        headers={"X-API-Key": "test-key-search"},
        json={"query": "des films comme Inception mais plus drôles"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Structural shape — camelCase aliases present.
    assert "results" in body
    assert "queryUsed" in body
    assert "model" in body

    results = body["results"]
    assert len(results) >= 1
    assert results[0]["tmdbId"] == 10
    # score for a cosine=1 pair (same unit vector) must be 1.0.
    assert abs(results[0]["score"] - 1.0) < 1e-4
    # Items are camelCase.
    assert "tmdbId" in results[0]
    assert "mediaType" in results[0]


async def test_media_type_filter_returns_only_that_type(seeded_client):
    """mediaType='tv' must exclude movie rows even if they have higher similarity."""
    resp = await seeded_client.post(
        "/api/ai/search",
        headers={"X-API-Key": "test-key-search"},
        json={"query": "anything", "mediaType": "tv"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for item in body["results"]:
        assert item["mediaType"] == "tv", f"Expected only tv but got {item}"
    # tmdb_id=30 (Dark, tv) should be present.
    ids = [r["tmdbId"] for r in body["results"]]
    assert 30 in ids


async def test_limit_respected(seeded_client):
    """limit=1 must return at most 1 result."""
    resp = await seeded_client.post(
        "/api/ai/search",
        headers={"X-API-Key": "test-key-search"},
        json={"query": "anything", "limit": 1},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) <= 1


async def test_query_used_reflects_reformulation(seeded_client):
    """queryUsed should be the reformulated string when Ollama succeeds."""
    resp = await seeded_client.post(
        "/api/ai/search",
        headers={"X-API-Key": "test-key-search"},
        json={"query": "des films comme Inception mais plus drôles"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # fake_generate returns "A mind-bending thriller with comedic twists"
    assert body["queryUsed"] == "A mind-bending thriller with comedic twists"


async def test_ollama_failure_degrades_gracefully(seeded_client, monkeypatch):
    """When Ollama raises (httpx error), /search must return 200 using the raw query."""

    async def failing_generate(prompt: str) -> str:
        raise httpx.ConnectError("Ollama unreachable")

    monkeypatch.setattr(ollama_service, "generate", failing_generate)

    raw_query = "des films comme Inception mais plus drôles"
    resp = await seeded_client.post(
        "/api/ai/search",
        headers={"X-API-Key": "test-key-search"},
        json={"query": raw_query},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Must fall back to the raw query.
    assert body["queryUsed"] == raw_query
    # Results must still be returned (embedding works fine).
    assert isinstance(body["results"], list)


async def test_ollama_timeout_degrades_gracefully(seeded_client, monkeypatch):
    """asyncio.TimeoutError from Ollama must also degrade gracefully."""
    import asyncio

    async def timing_out_generate(prompt: str) -> str:
        raise asyncio.TimeoutError()

    monkeypatch.setattr(ollama_service, "generate", timing_out_generate)

    raw_query = "thrilling science fiction"
    resp = await seeded_client.post(
        "/api/ai/search",
        headers={"X-API-Key": "test-key-search"},
        json={"query": raw_query},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["queryUsed"] == raw_query


async def test_camelcase_aliases_in_results(seeded_client):
    """All response fields must use camelCase aliases."""
    resp = await seeded_client.post(
        "/api/ai/search",
        headers={"X-API-Key": "test-key-search"},
        json={"query": "comedy"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Top-level
    assert "queryUsed" in body
    # Items
    if body["results"]:
        item = body["results"][0]
        assert "tmdbId" in item
        assert "mediaType" in item
        assert "score" in item
        # snake_case aliases must NOT appear in serialised output.
        assert "tmdb_id" not in item
        assert "media_type" not in item
