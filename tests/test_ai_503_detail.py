"""Integration tests for the 3 503 paths of the AI router + camelCase alias.

Covers the endpoint-level behaviour (TestClient via httpx ASGI), complementing
the unit-level tests of verify_api_key in tests/test_ai_deps.py:

  1. AI_API_KEY empty                 -> 503 "AI service not configured"
  2. sqlite-vec not loaded            -> 503 "AI vector storage unavailable"
  3. EmbeddingUnavailableError during
     hydrate_misses                   -> 503 "AI model unavailable"
  4. camelCase alias `excludeRefs`    -> 200 (populate_by_name)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import _VEC_LOADED, register_sqlite_vec_listener
from app.db.migrations import _migration_008_ai_embeddings
from app.services import embedding_service, recommendation_service
from app.services.embedding_service import EMBEDDING_DIM, EmbeddingUnavailableError
from app.services.tmdb_service import tmdb_service


pytestmark = pytest.mark.asyncio


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _FakeEnrichment:
    tmdb_id: int
    imdb_id: str | None
    overview: str | None
    genres: str | None


def _basis_vec(idx: int) -> list[float]:
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    arr[idx % EMBEDDING_DIM] = 1.0
    return arr.tolist()


# ──────────────────────────────────────────────────────────────────────────────
# Lightweight client fixture (no engine needed for the dependency-failure cases)
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def bare_client(monkeypatch, tmp_path) -> AsyncIterator[AsyncClient]:
    """ASGI client with default settings; tests override AI_API_KEY / _VEC_LOADED."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ──────────────────────────────────────────────────────────────────────────────
# Full engine fixture (for the model-unavailable case which must reach hydrate)
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ai_test_engine(tmp_path):
    db_path = tmp_path / "ai_503_test.db"
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
async def hydrate_client(
    ai_test_engine,
    ai_test_factory,
    monkeypatch,
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", "secret-test-key")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    from app.main import app
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", ai_test_factory)
    from app.api import ai as ai_mod
    monkeypatch.setattr(ai_mod, "async_session_factory", ai_test_factory)

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

async def test_rank_503_when_ai_api_key_empty(bare_client, monkeypatch):
    """POST /rank with AI_API_KEY="" -> 503 "AI service not configured"."""
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    resp = await bare_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "whatever"},
        json={"ref": {"tmdbId": 1}, "candidates": [{"tmdbId": 2}]},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "AI service not configured"


async def test_rank_multi_503_when_vec_unavailable(bare_client, monkeypatch):
    """POST /rank-multi with _VEC_LOADED["ok"]=False -> 503 "AI vector storage unavailable"."""
    monkeypatch.setattr(settings, "AI_API_KEY", "secret-test-key")
    monkeypatch.setitem(_VEC_LOADED, "ok", False)

    resp = await bare_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [{"tmdbId": 1}],
            "candidates": [{"tmdbId": 2}],
        },
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "AI vector storage unavailable"


async def test_rank_503_when_embedding_unavailable(hydrate_client, monkeypatch):
    """If embed_passages raises EmbeddingUnavailableError during hydrate, the
    endpoint must return 503 "AI model unavailable"."""

    async def fake_get_movie_details(tmdb_id: int):
        return _FakeEnrichment(tmdb_id, f"tt{tmdb_id}", "some overview", "Drama")

    async def fake_embed_passages(texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("model down")

    monkeypatch.setattr(tmdb_service, "get_movie_details", fake_get_movie_details)
    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(recommendation_service, "embed_passages", fake_embed_passages)

    resp = await hydrate_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 555},
            "candidates": [{"tmdbId": 556}],
        },
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "AI model unavailable"


async def test_rank_multi_camelcase_alias_excludeRefs(hydrate_client, monkeypatch):
    """Body in camelCase (`excludeRefs`) must be parsed correctly (populate_by_name)."""

    async def fake_get_movie_details(tmdb_id: int):
        return _FakeEnrichment(tmdb_id, f"tt{tmdb_id}", "overview", "Drama")

    async def fake_embed_passages(texts: list[str]) -> list[list[float]]:
        # All same vector -> ranking still well-defined (scores all equal-ish)
        return [_basis_vec(0) for _ in texts]

    monkeypatch.setattr(tmdb_service, "get_movie_details", fake_get_movie_details)
    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(recommendation_service, "embed_passages", fake_embed_passages)

    resp = await hydrate_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [{"tmdbId": 700}],
            "candidates": [{"tmdbId": 700}, {"tmdbId": 701}],
            "limit": 5,
            "mediaType": "movie",
            "excludeRefs": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked_ids = [item["tmdbId"] for item in body["ranked"]]
    # excludeRefs=False -> 700 (which is both ref and candidate) may appear
    assert 700 in ranked_ids
    assert 701 in ranked_ids
