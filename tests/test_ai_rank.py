"""Integration tests for POST /api/ai/rank.

Uses ASGI in-process client. TMDB & embedding services are monkeypatched so
tests stay offline. Mounts a per-test AI DB engine and overrides the
get_db / async_session_factory dependencies that /rank uses.
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
from app.services.embedding_service import EMBEDDING_DIM
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


def _unit_vec(idx: int) -> list[float]:
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    arr[idx % EMBEDDING_DIM] = 1.0
    return arr.tolist()


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ai_test_engine(tmp_path):
    """File-backed sqlite-vec engine, isolated per test."""
    db_path = tmp_path / "ai_rank_test.db"
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
async def rank_client(
    ai_test_engine,
    ai_test_factory,
    monkeypatch,
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with AI_API_KEY set, sqlite-vec marked OK, and AI deps overridden."""
    # Avoid touching real data/log dirs
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", "secret-test-key")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    from app.main import app
    from app.db import database as db_module

    # Override the async_session_factory used by /rank for hydrate_misses
    monkeypatch.setattr(db_module, "async_session_factory", ai_test_factory)
    # Also patch within app.api.ai (imported reference)
    from app.api import ai as ai_mod
    monkeypatch.setattr(ai_mod, "async_session_factory", ai_test_factory)

    # Override get_db dependency to use the test engine's sessionmaker
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

async def test_401_without_header(rank_client):
    resp = await rank_client.post(
        "/api/ai/rank",
        json={"ref": {"tmdbId": 1}, "candidates": [{"tmdbId": 2}]},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"


async def test_200_simple_ranking(rank_client, monkeypatch):
    """3 candidates by tmdb_id, mocks for TMDB + embed. Verify ranking + camelCase."""
    # Map tmdb_id -> orthogonal unit vector index so cosine = 1 for self, 0 otherwise.
    fake_data = {
        100: _FakeEnrichment(100, "tt100", "ref overview", "Drama"),
        101: _FakeEnrichment(101, "tt101", "cand1 overview", "Action"),
        102: _FakeEnrichment(102, "tt102", "cand2 overview", "Sci-Fi"),
        103: _FakeEnrichment(103, "tt103", "cand3 overview", "Horror"),
    }

    async def fake_get_movie_details(tmdb_id: int):
        return fake_data[tmdb_id]

    monkeypatch.setattr(tmdb_service, "get_movie_details", fake_get_movie_details)

    # Drive embeddings deterministically: assign a vector per ref/candidate
    # such that candidate 101 is most similar to ref 100.
    # Approach: ref vector = [1, 0, 0, ...] ; 101 = [0.9, 0.43..., ...] (close)
    # 102 = [0, 1, 0, ...] (orthogonal); 103 = [0, 0, 1, ...] (orthogonal).
    call_order: list[str] = []

    def _vec_for(text: str) -> list[float]:
        call_order.append(text)
        arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        if "ref overview" in text:
            arr[0] = 1.0
        elif "cand1 overview" in text:
            # near-parallel to ref
            arr[0] = 0.95
            arr[1] = float(np.sqrt(1.0 - 0.95 ** 2))
        elif "cand2 overview" in text:
            arr[2] = 1.0
        elif "cand3 overview" in text:
            arr[3] = 1.0
        else:
            arr[5] = 1.0
        # already unit norm
        return arr.tolist()

    async def fake_embed_passages(texts: list[str]) -> list[list[float]]:
        return [_vec_for(t) for t in texts]

    # Avoid touching real fastembed model.
    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(recommendation_service, "embed_passages", fake_embed_passages)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 100},
            "candidates": [
                {"tmdbId": 101},
                {"tmdbId": 102},
                {"tmdbId": 103},
            ],
            "limit": 10,
            "mediaType": "movie",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # camelCase aliases present
    assert "cacheHits" in body
    assert "cacheMisses" in body
    assert "cacheMissesDropped" in body
    assert "resolutionFailed" in body
    assert "ranked" in body

    # 4 unique ids were all misses → hydrated; cache_hits=0, cache_misses=4
    assert body["cacheHits"] == 0
    assert body["cacheMisses"] == 4
    assert body["cacheMissesDropped"] == 0
    assert body["resolutionFailed"] == 0

    # candidate 101 should rank first (highest cosine similarity)
    assert len(body["ranked"]) == 3
    assert body["ranked"][0]["tmdbId"] == 101
    assert body["ranked"][0]["score"] > body["ranked"][1]["score"]

    # camelCase on items
    assert "tmdbId" in body["ranked"][0]


async def test_422_when_ref_unresolvable(rank_client, monkeypatch):
    """imdb_id ref that TMDB cannot resolve -> 422."""

    async def fake_find(imdb_id: str, media_type: str):
        return None

    monkeypatch.setattr(tmdb_service, "find_by_imdb_id", fake_find)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"imdbId": "tt9999999"},
            "candidates": [{"tmdbId": 200}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "ref unresolvable"


async def test_camelcase_aliases_in_response(rank_client, monkeypatch):
    """Even with empty candidates we should get a well-formed camelCase response
    so long as the ref itself can be hydrated."""

    async def fake_get_movie_details(tmdb_id: int):
        return _FakeEnrichment(500, "tt500", "lonely ref", "Drama")

    async def fake_embed_passages(texts: list[str]) -> list[list[float]]:
        return [_unit_vec(0) for _ in texts]

    monkeypatch.setattr(tmdb_service, "get_movie_details", fake_get_movie_details)
    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(recommendation_service, "embed_passages", fake_embed_passages)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 500},
            "candidates": [],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    for key in ("ranked", "cacheHits", "cacheMisses", "cacheMissesDropped", "resolutionFailed"):
        assert key in body
    assert body["ranked"] == []
