"""Unit + integration tests for recommendation_service.

Uses isolated ai_db_session / ai_sessionmaker fixtures (J1) and monkeypatches
tmdb_service + embed_passages to keep tests offline and deterministic.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import numpy as np
import pytest
from sqlalchemy import text
from unittest.mock import AsyncMock

from app.services import embedding_service, recommendation_service
from app.services.embedding_service import EMBEDDING_DIM, EmbeddingUnavailableError
from app.services.recommendation_service import (
    HydrateStats,
    _deserialize_vec,
    _serialize_vec,
    cosine_rank,
    hydrate_misses,
    load_cached_vectors,
)


pytestmark = pytest.mark.asyncio
pytest_plugins = ["tests.conftest_ai"]


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


@pytest.fixture(autouse=True)
def _reset_model_singleton():
    embedding_service._model = None
    yield
    embedding_service._model = None


async def _insert_cached_embedding(session, tmdb_id: int, vec: list[float], media_type: str = "movie"):
    now = int(time.time() * 1000)
    await session.execute(
        text(
            "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, overview, genres, fetched_at, embedded_at) "
            "VALUES(:t, NULL, :mt, NULL, 'o', 'g', :n, :n)"
        ),
        {"t": tmdb_id, "mt": media_type, "n": now},
    )
    await session.execute(
        text("INSERT INTO ai_embeddings(tmdb_id, embedding) VALUES(:t, :v)"),
        {"t": tmdb_id, "v": _serialize_vec(vec)},
    )
    await session.commit()


async def test_load_cached_vectors_hits_only(ai_db_session):
    await _insert_cached_embedding(ai_db_session, 1, _unit_vec(0))
    await _insert_cached_embedding(ai_db_session, 2, _unit_vec(1))

    result = await load_cached_vectors(ai_db_session, [1, 2, 999])
    assert set(result.keys()) == {1, 2}
    assert len(result[1]) == EMBEDDING_DIM


async def test_load_cached_vectors_empty():
    # No DB session needed for empty path
    from sqlalchemy.ext.asyncio import AsyncSession
    fake = AsyncMock(spec=AsyncSession)
    result = await load_cached_vectors(fake, [])
    assert result == {}
    fake.execute.assert_not_called()


async def test_cosine_rank_orders_and_limits():
    q = _unit_vec(0)
    candidates = {
        1: _unit_vec(0),   # score ≈ 1.0
        2: _unit_vec(1),   # score ≈ 0.0
        3: [-x for x in _unit_vec(0)],  # score ≈ -1.0
    }
    ranked = cosine_rank(q, candidates, limit=2)
    assert [tid for tid, _ in ranked] == [1, 2]
    assert ranked[0][1] > ranked[1][1]


async def test_cosine_rank_exclude():
    q = _unit_vec(0)
    candidates = {1: _unit_vec(0), 2: _unit_vec(1)}
    ranked = cosine_rank(q, candidates, limit=10, exclude={1})
    assert [tid for tid, _ in ranked] == [2]


async def test_hydrate_misses_cap_at_20(monkeypatch, ai_sessionmaker):
    """25 misses → 20 hydrated, 5 dropped (cap excess)."""
    async def fake_get_movie_details(tmdb_id):
        return _FakeEnrichment(tmdb_id=tmdb_id, imdb_id=None, overview=f"plot {tmdb_id}", genres="Action")

    monkeypatch.setattr(
        "app.services.recommendation_service.tmdb_service.get_movie_details",
        fake_get_movie_details,
    )
    # Bypass real embedding: feed deterministic vectors
    async def fake_embed(texts):
        return [_unit_vec(i) for i, _ in enumerate(texts)]

    monkeypatch.setattr("app.services.recommendation_service.embed_passages", fake_embed)

    ids = list(range(100, 125))  # 25 ids
    vectors, stats = await hydrate_misses(ids, "movie", ai_sessionmaker)
    assert stats.hydrated == 20
    assert stats.dropped == 5  # 25 - 20 cap excess
    assert len(vectors) == 20


async def test_hydrate_misses_timeout_counts_dropped(monkeypatch, ai_sessionmaker):
    """Per-task timeout 10s → timeout becomes dropped without blocking siblings."""
    # Override timeout to keep test fast
    monkeypatch.setattr(recommendation_service, "HYDRATE_PER_TASK_TIMEOUT_S", 0.2)

    async def slow_get(tmdb_id):
        await asyncio.sleep(2.0)
        return _FakeEnrichment(tmdb_id=tmdb_id, imdb_id=None, overview="x", genres="y")

    monkeypatch.setattr(
        "app.services.recommendation_service.tmdb_service.get_movie_details",
        slow_get,
    )
    async def fake_embed(texts):
        return [_unit_vec(0) for _ in texts]
    monkeypatch.setattr("app.services.recommendation_service.embed_passages", fake_embed)

    start = time.monotonic()
    vectors, stats = await hydrate_misses([1, 2], "movie", ai_sessionmaker)
    elapsed = time.monotonic() - start
    assert elapsed < 1.5  # both timed out roughly in parallel
    assert vectors == {}
    assert stats.dropped == 2


async def test_hydrate_misses_propagates_embedding_unavailable(monkeypatch, ai_sessionmaker):
    async def fake_get(tmdb_id):
        return _FakeEnrichment(tmdb_id=tmdb_id, imdb_id=None, overview="x", genres="y")
    monkeypatch.setattr(
        "app.services.recommendation_service.tmdb_service.get_movie_details",
        fake_get,
    )
    async def bomb(texts):
        raise EmbeddingUnavailableError("model offline")
    monkeypatch.setattr("app.services.recommendation_service.embed_passages", bomb)

    with pytest.raises(EmbeddingUnavailableError):
        await hydrate_misses([1], "movie", ai_sessionmaker)
