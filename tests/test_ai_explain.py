"""Tests for F5 – "why recommended" explanations on /rank and /rank-multi.

Verifies:
  - explain=true -> top results carry a non-null explanation (mocked Ollama).
  - explain omitted/false -> explanation is null AND ollama_service.generate is NOT called.
  - Ollama down + explain=true -> still 200, ranking intact, explanations null (graceful).
  - EXPLAIN_CAP is respected: generate is called at most EXPLAIN_CAP times.
  - camelCase response shape is preserved.
  - Pre-existing rank tests are unaffected (separate module; this file just confirms
    the additive nature by exercising the same fixture setup).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator

import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.ai import EXPLAIN_CAP
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
# Fixtures — mirror the setup from test_ai_rank.py
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ai_test_engine(tmp_path):
    db_path = tmp_path / "ai_explain_test.db"
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


# ──────────────────────────────────────────────────────────────────────────────
# Shared setup helpers
# ──────────────────────────────────────────────────────────────────────────────

def _setup_fake_world(monkeypatch, fake_data: dict) -> None:
    """Wire TMDB + embedding mocks so that the ranking layer works offline."""

    async def fake_get_movie_details(tmdb_id: int):
        return fake_data.get(tmdb_id)

    monkeypatch.setattr(tmdb_service, "get_movie_details", fake_get_movie_details)

    async def fake_embed_passages(texts: list[str]) -> list[list[float]]:
        # Deterministic: hash first char of text to pick a unit vector index.
        return [_unit_vec(abs(hash(t)) % EMBEDDING_DIM) for t in texts]

    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(recommendation_service, "embed_passages", fake_embed_passages)


# ──────────────────────────────────────────────────────────────────────────────
# Tests: /rank with explain
# ──────────────────────────────────────────────────────────────────────────────

async def test_explain_true_rank_populates_explanations(rank_client, monkeypatch):
    """explain=true -> top results receive a non-null explanation string."""
    fake_data = {
        1: _FakeEnrichment(1, "tt001", "overview ref", "Drama"),
        2: _FakeEnrichment(2, "tt002", "overview cand2", "Action"),
        3: _FakeEnrichment(3, "tt003", "overview cand3", "Sci-Fi"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    generate_calls: list[str] = []

    async def fake_generate(prompt: str) -> str:
        generate_calls.append(prompt)
        return "Car les deux partagent une atmosphère similaire."

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 1},
            "candidates": [{"tmdbId": 2}, {"tmdbId": 3}],
            "limit": 10,
            "mediaType": "movie",
            "explain": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]
    assert len(ranked) == 2

    # All items in top-EXPLAIN_CAP should have a non-null explanation.
    for item in ranked:
        assert item["explanation"] is not None, f"tmdbId={item['tmdbId']} missing explanation"

    # generate was called once per candidate (2 candidates, both within EXPLAIN_CAP).
    assert len(generate_calls) == 2


async def test_explain_capped_at_explain_cap(rank_client, monkeypatch):
    """When there are more candidates than EXPLAIN_CAP, generate is called at most EXPLAIN_CAP times."""
    # Build more candidates than EXPLAIN_CAP.
    n_cands = EXPLAIN_CAP + 3
    fake_data = {0: _FakeEnrichment(0, "tt000", "overview ref", "Drama")}
    for i in range(1, n_cands + 1):
        fake_data[i] = _FakeEnrichment(i, f"tt{i:03d}", f"overview cand{i}", "Action")
    _setup_fake_world(monkeypatch, fake_data)

    generate_calls: list[str] = []

    async def fake_generate(prompt: str) -> str:
        generate_calls.append(prompt)
        return "Car l'ambiance est identique."

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate)

    candidates = [{"tmdbId": i} for i in range(1, n_cands + 1)]
    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 0},
            "candidates": candidates,
            "limit": 50,
            "mediaType": "movie",
            "explain": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]

    # generate should have been called at most EXPLAIN_CAP times.
    assert len(generate_calls) <= EXPLAIN_CAP

    # Top EXPLAIN_CAP items should have explanation; the rest should be None.
    for item in ranked[:EXPLAIN_CAP]:
        assert item["explanation"] is not None
    for item in ranked[EXPLAIN_CAP:]:
        assert item["explanation"] is None


async def test_explain_false_no_llm_call(rank_client, monkeypatch):
    """explain omitted (default false) -> explanation is null, generate NOT called."""
    fake_data = {
        10: _FakeEnrichment(10, "tt010", "overview ref", "Drama"),
        11: _FakeEnrichment(11, "tt011", "overview cand", "Action"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    generate_calls: list[str] = []

    async def fake_generate(prompt: str) -> str:
        generate_calls.append(prompt)
        return "This should never be called."

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 10},
            "candidates": [{"tmdbId": 11}],
            "limit": 5,
            "mediaType": "movie",
            # explain field omitted → defaults to false
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]
    assert len(ranked) == 1
    assert ranked[0]["explanation"] is None
    assert len(generate_calls) == 0, "generate should NOT be called when explain=false"


async def test_explain_explicit_false_no_llm_call(rank_client, monkeypatch):
    """explain=false explicitly -> explanation is null, generate NOT called."""
    fake_data = {
        20: _FakeEnrichment(20, "tt020", "overview ref", "Drama"),
        21: _FakeEnrichment(21, "tt021", "overview cand", "Thriller"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    generate_calls: list[str] = []

    async def fake_generate(prompt: str) -> str:
        generate_calls.append(prompt)
        return "Should not be called."

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 20},
            "candidates": [{"tmdbId": 21}],
            "limit": 5,
            "mediaType": "movie",
            "explain": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ranked"][0]["explanation"] is None
    assert len(generate_calls) == 0


async def test_explain_ollama_down_graceful(rank_client, monkeypatch):
    """Ollama unreachable + explain=true -> still 200, ranking intact, explanations null."""
    fake_data = {
        30: _FakeEnrichment(30, "tt030", "overview ref", "Comedy"),
        31: _FakeEnrichment(31, "tt031", "overview cand", "Comedy"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    async def fake_generate_raises(prompt: str) -> str:
        raise ConnectionError("Ollama is down")

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate_raises)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 30},
            "candidates": [{"tmdbId": 31}],
            "limit": 5,
            "mediaType": "movie",
            "explain": True,
        },
    )
    # Must still be 200 — Ollama failure must not become a 503.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]
    assert len(ranked) == 1
    assert ranked[0]["tmdbId"] == 31
    # explanation should be None (best-effort degraded gracefully)
    assert ranked[0]["explanation"] is None


async def test_explain_ollama_timeout_graceful(rank_client, monkeypatch):
    """Ollama timeout + explain=true -> 200, ranking intact, explanations null."""
    fake_data = {
        40: _FakeEnrichment(40, "tt040", "overview ref", "Horror"),
        41: _FakeEnrichment(41, "tt041", "overview cand", "Horror"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    async def fake_generate_timeout(prompt: str) -> str:
        # Simulate a very long delay that will be cancelled via asyncio.wait_for.
        await asyncio.sleep(9999)
        return "never"

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate_timeout)

    # Lower the explain timeout so the test doesn't wait 20s.
    from app.api import ai as ai_mod
    monkeypatch.setattr(ai_mod, "_EXPLAIN_TIMEOUT_S", 0.05)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 40},
            "candidates": [{"tmdbId": 41}],
            "limit": 5,
            "mediaType": "movie",
            "explain": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]
    assert len(ranked) == 1
    assert ranked[0]["explanation"] is None


# ──────────────────────────────────────────────────────────────────────────────
# Tests: /rank-multi with explain
# ──────────────────────────────────────────────────────────────────────────────

async def test_explain_true_rank_multi(rank_client, monkeypatch):
    """explain=true on rank-multi -> top results receive non-null explanations."""
    fake_data = {
        50: _FakeEnrichment(50, "tt050", "overview ref1", "Drama"),
        51: _FakeEnrichment(51, "tt051", "overview ref2", "Drama"),
        52: _FakeEnrichment(52, "tt052", "overview cand1", "Action"),
        53: _FakeEnrichment(53, "tt053", "overview cand2", "Sci-Fi"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    generate_calls: list[str] = []

    async def fake_generate(prompt: str) -> str:
        generate_calls.append(prompt)
        return "Car les thèmes se recoupent parfaitement."

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate)

    resp = await rank_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [{"tmdbId": 50}, {"tmdbId": 51}],
            "candidates": [{"tmdbId": 52}, {"tmdbId": 53}],
            "limit": 10,
            "mediaType": "movie",
            "explain": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]
    assert len(ranked) == 2

    for item in ranked:
        assert item["explanation"] is not None

    # 2 candidates, both within EXPLAIN_CAP -> generate called twice.
    assert len(generate_calls) == 2


async def test_explain_false_rank_multi_no_llm_call(rank_client, monkeypatch):
    """explain omitted on rank-multi -> explanation null, generate NOT called."""
    fake_data = {
        60: _FakeEnrichment(60, "tt060", "overview ref", "Thriller"),
        61: _FakeEnrichment(61, "tt061", "overview cand", "Thriller"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    generate_calls: list[str] = []

    async def fake_generate(prompt: str) -> str:
        generate_calls.append(prompt)
        return "Should not be called."

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate)

    resp = await rank_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [{"tmdbId": 60}],
            "candidates": [{"tmdbId": 61}],
            "limit": 5,
            "mediaType": "movie",
            # explain omitted -> defaults to false
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ranked"][0]["explanation"] is None
    assert len(generate_calls) == 0


async def test_rank_multi_ollama_down_graceful(rank_client, monkeypatch):
    """Ollama down + explain=true on rank-multi -> 200, ranking intact, explanations null."""
    fake_data = {
        70: _FakeEnrichment(70, "tt070", "overview ref", "Fantasy"),
        71: _FakeEnrichment(71, "tt071", "overview cand", "Fantasy"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    async def fake_generate_raises(prompt: str) -> str:
        raise OSError("Ollama connection refused")

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate_raises)

    resp = await rank_client.post(
        "/api/ai/rank-multi",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "refs": [{"tmdbId": 70}],
            "candidates": [{"tmdbId": 71}],
            "limit": 5,
            "mediaType": "movie",
            "explain": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]
    assert len(ranked) == 1
    assert ranked[0]["tmdbId"] == 71
    assert ranked[0]["explanation"] is None


# ──────────────────────────────────────────────────────────────────────────────
# Tests: camelCase serialization + response shape
# ──────────────────────────────────────────────────────────────────────────────

async def test_camelcase_explanation_field(rank_client, monkeypatch):
    """explanation field serializes with camelCase alias (explanation, not Explanation)."""
    fake_data = {
        80: _FakeEnrichment(80, "tt080", "overview ref", "Drama"),
        81: _FakeEnrichment(81, "tt081", "overview cand", "Drama"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    async def fake_generate(prompt: str) -> str:
        return "Car les ambiances se ressemblent."

    from app.services import ollama_service as _ollama
    monkeypatch.setattr(_ollama, "generate", fake_generate)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 80},
            "candidates": [{"tmdbId": 81}],
            "limit": 5,
            "mediaType": "movie",
            "explain": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ranked = body["ranked"]
    assert len(ranked) == 1

    item = ranked[0]
    # camelCase keys present
    assert "tmdbId" in item
    assert "score" in item
    # explanation is the field name (no transformation since it's a single word)
    assert "explanation" in item
    assert item["explanation"] is not None

    # Ensure existing camelCase top-level keys are intact
    for key in ("ranked", "cacheHits", "cacheMisses", "cacheMissesDropped", "resolutionFailed"):
        assert key in body


async def test_explain_response_shape_unchanged_when_off(rank_client, monkeypatch):
    """When explain=false, the response shape is identical to pre-F5 (explanation is null)."""
    fake_data = {
        90: _FakeEnrichment(90, "tt090", "overview ref", "Romance"),
        91: _FakeEnrichment(91, "tt091", "overview cand", "Romance"),
    }
    _setup_fake_world(monkeypatch, fake_data)

    resp = await rank_client.post(
        "/api/ai/rank",
        headers={"X-API-Key": "secret-test-key"},
        json={
            "ref": {"tmdbId": 90},
            "candidates": [{"tmdbId": 91}],
            "limit": 5,
            "mediaType": "movie",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    item = body["ranked"][0]

    # All expected keys are present.
    assert "tmdbId" in item
    assert "score" in item
    # explanation is present but null (additive optional field).
    assert "explanation" in item
    assert item["explanation"] is None
