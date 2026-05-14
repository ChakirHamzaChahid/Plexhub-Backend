"""Integration tests for POST /api/ai/rank-multi (J4).

Uses ASGI in-process client with a per-test sqlite-vec engine, just like
test_ai_rank.py. TMDB and embedding services are monkeypatched offline.
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
# Helpers / fixtures
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


@pytest_asyncio.fixture
async def ai_test_engine(tmp_path):
    db_path = tmp_path / "ai_rank_multi_test.db"
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


def _setup_basis_world(monkeypatch, mapping: dict[int, int]) -> None:
    """Wire tmdb_service + embed_passages so each tmdb_id maps to a basis vector.

    `mapping`: {tmdb_id: basis_index}. Each tmdb_id resolves to a fake
    enrichment whose overview encodes its basis index in plain text.
    """

    async def fake_get_movie_details(tmdb_id: int):
        if tmdb_id not in mapping:
            return None
        return _FakeEnrichment(
            tmdb_id=tmdb_id,
            imdb_id=f"tt{tmdb_id}",
            overview=f"tmdb-{tmdb_id} basis-{mapping[tmdb_id]}",
            genres="Drama",
        )

    monkeypatch.setattr(tmdb_service, "get_movie_details", fake_get_movie_details)

    async def fake_embed_passages(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            # Recover basis index from synthetic overview "tmdb-X basis-N ..."
            idx = 0
            for token in t.split():
                if token.startswith("basis-"):
                    try:
                        idx = int(token.split("-", 1)[1])
                    except ValueError:
                        idx = 0
                    break
            out.append(_basis_vec(idx))
        return out

    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(recommendation_service, "embed_passages", fake_embed_passages)


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

async def test_401_without_header(rank_client):
    resp = await rank_client.post(
        "/api/ai/rank-multi",
        json={
            "refs": [{"tmdbId": 1}],
            "candidates": [{"tmdbId": 2}],
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"


async def test_centroid_excludes_targets(rank_client, monkeypatch):
    """refs=[1,2,3], candidates include id=2 → id=2 must be absent (excludeRefs=true default)."""
    _setup_basis_world(monkeypatch, {1: 0, 2: 1, 3: 2, 10: 0, 11: 1, 12: 2})

    resp = await rank_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [{"tmdbId": 1}, {"tmdbId": 2}, {"tmdbId": 3}],
            "candidates": [
                {"tmdbId": 2},   # collides with a ref → must be filtered
                {"tmdbId": 10},
                {"tmdbId": 11},
                {"tmdbId": 12},
            ],
            "limit": 10,
            "mediaType": "movie",
            "excludeRefs": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked_ids = [item["tmdbId"] for item in body["ranked"]]
    assert 2 not in ranked_ids
    assert set(ranked_ids) == {10, 11, 12}


async def test_exclude_refs_disabled(rank_client, monkeypatch):
    """Same payload with excludeRefs=false → id=2 may appear in the ranking."""
    _setup_basis_world(monkeypatch, {1: 0, 2: 1, 3: 2, 10: 0, 11: 1, 12: 2})

    resp = await rank_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [{"tmdbId": 1}, {"tmdbId": 2}, {"tmdbId": 3}],
            "candidates": [
                {"tmdbId": 2},
                {"tmdbId": 10},
                {"tmdbId": 11},
                {"tmdbId": 12},
            ],
            "limit": 10,
            "mediaType": "movie",
            "excludeRefs": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked_ids = [item["tmdbId"] for item in body["ranked"]]
    assert 2 in ranked_ids


async def test_weight_decay_dominance(rank_client, monkeypatch):
    """2 orthogonal refs (basis 0, basis 1) with decay 1.0/0.9 → candidate aligned
    on basis-0 must outrank candidate aligned on basis-1 (centroid is closer to e0).
    """
    _setup_basis_world(monkeypatch, {1: 0, 2: 1, 100: 0, 101: 1})

    resp = await rank_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [{"tmdbId": 1}, {"tmdbId": 2}],
            "candidates": [{"tmdbId": 100}, {"tmdbId": 101}],
            "limit": 10,
            "mediaType": "movie",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]
    assert len(ranked) == 2
    assert ranked[0]["tmdbId"] == 100
    assert ranked[1]["tmdbId"] == 101
    assert ranked[0]["score"] > ranked[1]["score"]


async def test_empty_refs_422(rank_client):
    resp = await rank_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [],
            "candidates": [{"tmdbId": 99}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "refs cannot be empty"
