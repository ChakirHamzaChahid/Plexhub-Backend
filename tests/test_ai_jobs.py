"""Integration tests for POST /api/ai/embed/rebuild and GET /api/ai/embed/jobs/{job_id}.

Uses ASGI in-process client. The embedding worker is monkeypatched so the job
completes quickly and offline.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import _VEC_LOADED, register_sqlite_vec_listener
from app.db.migrations import _migration_008_ai_embeddings
from app.services.embedding_service import EMBEDDING_DIM


pytestmark = pytest.mark.asyncio


def _unit_vec(idx: int) -> list[float]:
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    arr[idx % EMBEDDING_DIM] = 1.0
    return arr.tolist()


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ai_test_engine(tmp_path):
    db_path = tmp_path / "ai_jobs_test.db"
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
async def jobs_client(
    ai_test_engine,
    ai_test_factory,
    monkeypatch,
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with AI_API_KEY set, sqlite-vec marked OK, and worker deps overridden."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", "secret-test-key")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    from app.main import app
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", ai_test_factory)
    # Patch the worker's reference too
    from app.workers import embedding_worker
    monkeypatch.setattr(embedding_worker, "async_session_factory", ai_test_factory)

    # Stub embed_passages so background job completes offline & quickly
    async def fake_embed(texts):
        return [_unit_vec(i) for i, _ in enumerate(texts)]
    monkeypatch.setattr(embedding_worker, "embed_passages", fake_embed)

    # Reset job dict (module-level state)
    embedding_worker._ai_jobs.clear()

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
        embedding_worker._ai_jobs.clear()


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

async def test_rebuild_returns_202_and_jobid(jobs_client):
    resp = await jobs_client.post(
        "/api/ai/embed/rebuild",
        headers={"X-API-Key": "secret-test-key"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "jobId" in body
    job_id = body["jobId"]
    assert job_id.startswith("ai_rebuild_")
    suffix = job_id.removeprefix("ai_rebuild_")
    assert suffix.isdigit()


async def test_rebuild_auth_inherited_401(jobs_client):
    resp = await jobs_client.post("/api/ai/embed/rebuild")
    assert resp.status_code == 401


async def test_job_status_404_unknown(jobs_client):
    resp = await jobs_client.get(
        "/api/ai/embed/jobs/unknownid",
        headers={"X-API-Key": "secret-test-key"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "job not found"


async def test_job_status_polling(jobs_client, ai_test_factory):
    # Pre-seed a few pending rows so the worker has actual work
    now = int(time.time() * 1000)
    async with ai_test_factory() as s:
        for tid in range(1, 4):
            await s.execute(
                text(
                    "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, overview, genres, fetched_at, embedded_at) "
                    "VALUES(:t, NULL, 'movie', NULL, 'overview', 'Action', :n, NULL)"
                ),
                {"t": tid, "n": now},
            )
        await s.commit()

    resp = await jobs_client.post(
        "/api/ai/embed/rebuild",
        headers={"X-API-Key": "secret-test-key"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["jobId"]

    # Poll with 30s wall budget
    deadline = time.monotonic() + 30.0
    final = None
    while time.monotonic() < deadline:
        s_resp = await jobs_client.get(
            f"/api/ai/embed/jobs/{job_id}",
            headers={"X-API-Key": "secret-test-key"},
        )
        assert s_resp.status_code == 200
        body = s_resp.json()
        # camelCase aliases
        assert "jobId" in body
        assert "lastError" in body
        assert "startedAt" in body
        assert "finishedAt" in body
        if body["status"] in ("done", "failed"):
            final = body
            break
        await asyncio.sleep(0.1)

    assert final is not None, "job did not finish within 30s"
    assert final["status"] == "done", final
    assert final["processed"] == 3
    assert final["errors"] == 0


async def test_startup_no_ai_job(monkeypatch, tmp_path):
    """R5: never auto-run at boot — importing app.main must not enqueue any job."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    from app.workers import embedding_worker
    embedding_worker._ai_jobs.clear()

    # Import app.main fresh — it's fine if it was already imported by other tests,
    # the invariant is that nothing should have auto-scheduled a rebuild job.
    import app.main  # noqa: F401

    assert embedding_worker._ai_jobs == {}
