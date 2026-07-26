"""AUDIT-P1-001 (S3.3): real-lock contention (ADR 0004, Decision 4) for the
two ai.py cache-persist call sites converted to write_with_retry — the
subtitle-translation cache write (POST /api/ai/subtitles/translate) and the
blurb cache write (POST /api/ai/blurb).

Mirrors the harness in tests/test_db_retry_real_lock.py /
tests/test_accounts_retry.py / tests/test_tv_auth.py: a real cross-connection
SQLite write lock, held by an independent plain sqlite3 connection, on a
dedicated file-backed engine with a short busy_timeout (set BEFORE any
connection is opened) so the lock surfaces within the test's time budget.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import _VEC_LOADED, register_sqlite_vec_listener
from app.db.migrations import (
    _migration_008_ai_embeddings,
    _migration_011_create_subtitle_cache,
    _migration_012_create_media_blurb,
)
from app.models.database import Base
from app.services import ollama_service

_API_KEY = "test-key-lock"

SRT_2_CUES = """\
1
00:00:01,000 --> 00:00:02,000
Hello world

2
00:00:03,000 --> 00:00:04,000
Goodbye world
"""


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS _lock_probe (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO _lock_probe DEFAULT VALUES")
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def ai_client_lockable(monkeypatch, tmp_path) -> AsyncIterator[tuple[AsyncClient, str]]:
    """Authenticated ASGI client wired to a dedicated file-backed DB with a
    short busy_timeout, registered before any connection opens."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", _API_KEY)
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    db_path = tmp_path / "ai_lock_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_short_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA busy_timeout=50")

    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)
    await _migration_011_create_subtitle_cache(engine)
    await _migration_012_create_media_blurb(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.main import app
    from app.db import database as db_module
    from app.api import ai as ai_mod

    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(ai_mod, "async_session_factory", factory)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
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
            yield client, str(db_path)
    finally:
        app.dependency_overrides.pop(db_module.get_db, None)
        await engine.dispose()


async def test_subtitle_cache_persist_survives_real_lock(ai_client_lockable, monkeypatch):
    """AUDIT-P1-001 (S3.3): the subtitle-cache write in POST
    /api/ai/subtitles/translate must succeed (and the freshly-translated
    content must still be returned) despite a genuine SQLite write lock held
    by an independent connection."""
    client, db_path = ai_client_lockable

    async def fake_generate(prompt: str) -> str:
        return "1. [fr] ligne 1\n2. [fr] ligne 2"

    monkeypatch.setattr(ollama_service, "generate", fake_generate)

    hold_seconds = 0.35
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

    resp = await client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": _API_KEY},
        json={"content": SRT_2_CUES, "targetLang": "fr"},
    )

    blocker.join(timeout=5)
    assert not blocker.is_alive()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cached"] is False
    assert body["cueCount"] == 2


async def test_blurb_cache_persist_survives_real_lock(ai_client_lockable, monkeypatch):
    """AUDIT-P1-001 (S3.3): the blurb cache write (delete+insert) in POST
    /api/ai/blurb must succeed despite a genuine SQLite write lock held by
    an independent connection."""
    client, db_path = ai_client_lockable

    async def fake_generate(prompt: str) -> str:
        return json.dumps({"summary": "Synopsis sous contention.", "tags": ["action"]})

    monkeypatch.setattr(ollama_service, "generate", fake_generate)

    hold_seconds = 0.35
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

    resp = await client.post(
        "/api/ai/blurb",
        headers={"X-API-Key": _API_KEY},
        json={
            "tmdbId": 4242,
            "mediaType": "movie",
            "title": "Film Sous Contention",
            "overview": "Un test de verrou réel.",
        },
    )

    blocker.join(timeout=5)
    assert not blocker.is_alive()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cached"] is False
    assert body["summary"] == "Synopsis sous contention."
