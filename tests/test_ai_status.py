"""Integration tests for GET /api/ai/embed/status (J6).

Uses ASGI in-process client with an isolated sqlite-vec engine. The endpoint
returns a diagnostic snapshot of the AI subsystem: counts, RSS, model & vec
extension state.
"""
from __future__ import annotations

import time
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import _VEC_LOADED, register_sqlite_vec_listener
from app.db.migrations import _migration_008_ai_embeddings
from app.services.embedding_service import DEFAULT_MODEL_NAME


pytestmark = pytest.mark.asyncio


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ai_test_engine(tmp_path):
    db_path = tmp_path / "ai_status_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def ai_test_factory(ai_test_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(ai_test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def status_client(
    ai_test_engine,
    ai_test_factory,
    monkeypatch,
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with AI_API_KEY set, sqlite-vec marked OK, and get_db overridden."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", "secret-test-key")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    from app.main import app
    from app.db import database as db_module

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with ai_test_factory() as session:
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


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

async def test_status_shape(status_client):
    """GET with auth returns 200 + all 10 expected camelCase fields."""
    resp = await status_client.get(
        "/api/ai/embed/status",
        headers={"X-API-Key": "secret-test-key"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    expected_keys = {
        "totalEmbeddings",
        "totalCacheEntries",
        "pendingEmbed",
        "lastIndexedAt",
        "rssMb",
        "modelLoaded",
        "modelName",
        "embeddingDim",
        "vecLoaded",
        "vecError",
    }
    assert expected_keys.issubset(set(data.keys())), (
        f"missing keys: {expected_keys - set(data.keys())}"
    )

    assert isinstance(data["rssMb"], int)
    assert data["modelName"] == DEFAULT_MODEL_NAME
    assert data["embeddingDim"] == 384
    assert isinstance(data["vecLoaded"], bool)
    assert isinstance(data["vecError"], str)
    assert isinstance(data["modelLoaded"], bool)
    assert isinstance(data["totalEmbeddings"], int)
    assert isinstance(data["totalCacheEntries"], int)
    assert isinstance(data["pendingEmbed"], int)
    # lastIndexedAt is None on an empty cache
    assert data["lastIndexedAt"] is None or isinstance(data["lastIndexedAt"], int)


async def test_status_auth_required_401(status_client):
    """GET without X-API-Key header returns 401."""
    resp = await status_client.get("/api/ai/embed/status")
    assert resp.status_code == 401


async def test_status_rss_is_int_not_float(status_client):
    """C6: rssMb must be an int, not a float, and strictly positive."""
    resp = await status_client.get(
        "/api/ai/embed/status",
        headers={"X-API-Key": "secret-test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["rssMb"], int)
    assert not isinstance(data["rssMb"], bool)
    assert data["rssMb"] > 0


async def test_status_pending_count_reflects_db(status_client, ai_test_factory):
    """Insert 3 pending + 2 indexed rows; verify pendingEmbed=3, totalCacheEntries=5."""
    now = int(time.time() * 1000)
    async with ai_test_factory() as s:
        # 3 pending (embedded_at IS NULL)
        for tid in range(1, 4):
            await s.execute(
                text(
                    "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, overview, genres, fetched_at, embedded_at) "
                    "VALUES(:t, NULL, 'movie', NULL, 'overview', 'Action', :n, NULL)"
                ),
                {"t": tid, "n": now},
            )
        # 2 already embedded
        for tid in range(100, 102):
            await s.execute(
                text(
                    "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, overview, genres, fetched_at, embedded_at) "
                    "VALUES(:t, NULL, 'tv', NULL, 'overview', 'Drama', :n, :n)"
                ),
                {"t": tid, "n": now},
            )
        await s.commit()

    resp = await status_client.get(
        "/api/ai/embed/status",
        headers={"X-API-Key": "secret-test-key"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pendingEmbed"] == 3
    assert data["totalCacheEntries"] == 5
    assert data["lastIndexedAt"] == now
