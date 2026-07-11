"""Tests for app.workers.embedding_worker.

Mocks embed_passages and uses isolated ai_db_session/ai_sessionmaker fixtures.
"""
from __future__ import annotations

import time

import numpy as np
import pytest
from sqlalchemy import text

from app.services.embedding_service import EMBEDDING_DIM, EmbeddingUnavailableError
from app.workers.embedding_worker import (
    JOBS_CAP,
    _ai_jobs,
    _make_job_id,
    get_job,
    register_job,
    run_embedding_rebuild,
)


pytest_plugins = ["tests.conftest_ai"]


def _unit_vec(idx: int) -> list[float]:
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    arr[idx % EMBEDDING_DIM] = 1.0
    return arr.tolist()


@pytest.fixture(autouse=True)
def _reset_jobs():
    _ai_jobs.clear()
    yield
    _ai_jobs.clear()


async def _insert_pending(session, tmdb_id: int, overview: str = "plot", genres: str = "Action") -> None:
    now = int(time.time() * 1000)
    await session.execute(
        text(
            "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, overview, genres, fetched_at, embedded_at) "
            "VALUES(:t, NULL, 'movie', NULL, :o, :g, :n, NULL)"
        ),
        {"t": tmdb_id, "o": overview, "g": genres, "n": now},
    )
    await session.commit()


def test_make_job_id_format():
    jid = _make_job_id()
    assert jid.startswith("ai_rebuild_")
    suffix = jid.removeprefix("ai_rebuild_")
    assert suffix.isdigit() and len(suffix) >= 10


def test_fifo_eviction_at_cap():
    """register_job evicts oldest when reaching JOBS_CAP."""
    for i in range(JOBS_CAP + 5):
        register_job(f"j{i}", {"status": "done", "processed": 0, "errors": 0,
                               "last_error": None, "started_at": 0, "finished_at": 0})
    assert len(_ai_jobs) == JOBS_CAP
    # j0..j4 evicted (5 over cap)
    assert get_job("j0") is None
    assert get_job("j4") is None
    assert get_job("j5") is not None
    assert get_job(f"j{JOBS_CAP + 4}") is not None


def test_register_job_in_place_update():
    register_job("only", {"status": "pending"})
    register_job("only", {"status": "running"})
    assert len(_ai_jobs) == 1
    assert get_job("only")["status"] == "running"


async def test_rebuild_processes_pending_rows(monkeypatch, ai_sessionmaker, ai_db_session):
    """Insert 120 pending rows, run rebuild, assert all embedded_at populated."""
    # Wire async_session_factory to the test ai_sessionmaker
    monkeypatch.setattr("app.workers.embedding_worker.async_session_factory", ai_sessionmaker)

    async def fake_embed(texts):
        return [_unit_vec(i) for i, _ in enumerate(texts)]
    monkeypatch.setattr("app.workers.embedding_worker.embed_passages", fake_embed)

    for tid in range(1, 121):
        await _insert_pending(ai_db_session, tid)

    job_id = "test-rebuild"
    register_job(job_id, {"status": "pending", "processed": 0, "errors": 0,
                          "last_error": None, "started_at": 0, "finished_at": None})
    await run_embedding_rebuild(job_id)

    job = get_job(job_id)
    assert job["status"] == "done"
    assert job["processed"] == 120
    assert job["errors"] == 0
    assert job["finished_at"] is not None

    # Verify no pending rows remain
    remaining = (await ai_db_session.execute(
        text("SELECT COUNT(*) FROM ai_tmdb_cache WHERE embedded_at IS NULL")
    )).scalar()
    assert remaining == 0
    embeddings_count = (await ai_db_session.execute(
        text("SELECT COUNT(*) FROM ai_embeddings")
    )).scalar()
    assert embeddings_count == 120


async def test_rebuild_no_offset_in_queries(monkeypatch, ai_sessionmaker, ai_db_session):
    """Verify pagination uses cursor (tmdb_id > :cursor), not OFFSET."""
    monkeypatch.setattr("app.workers.embedding_worker.async_session_factory", ai_sessionmaker)

    captured: list[str] = []

    async def fake_embed(texts):
        return [_unit_vec(0) for _ in texts]
    monkeypatch.setattr("app.workers.embedding_worker.embed_passages", fake_embed)

    for tid in range(1, 6):
        await _insert_pending(ai_db_session, tid)

    from sqlalchemy.ext.asyncio import AsyncSession
    real_execute = AsyncSession.execute

    async def spy_execute(self, statement, *args, **kwargs):
        captured.append(str(statement))
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", spy_execute)

    register_job("j", {"status": "pending", "processed": 0, "errors": 0,
                       "last_error": None, "started_at": 0, "finished_at": None})
    await run_embedding_rebuild("j")
    assert not any("OFFSET" in q.upper() for q in captured), captured


async def test_rebuild_aborts_on_embedding_unavailable(monkeypatch, ai_sessionmaker, ai_db_session):
    monkeypatch.setattr("app.workers.embedding_worker.async_session_factory", ai_sessionmaker)

    async def bomb(texts):
        raise EmbeddingUnavailableError("offline")
    monkeypatch.setattr("app.workers.embedding_worker.embed_passages", bomb)

    await _insert_pending(ai_db_session, 1)
    register_job("j", {"status": "pending", "processed": 0, "errors": 0,
                       "last_error": None, "started_at": 0, "finished_at": None})
    await run_embedding_rebuild("j")
    job = get_job("j")
    assert job["status"] == "failed"
    assert "embedding unavailable" in (job["last_error"] or "")
