"""B1-10: the gateway cache writers against a REAL SQLite write lock.

CLAUDE.md section 9, trap 8: a writer is only proven by a genuine WAL lock
held by another connection (not a synthetic `OperationalError`). Same harness
as `tests/test_db_retry_real_lock.py` / `tests/test_worker_write_retry_real_lock.py`:
a plain `sqlite3` connection holds `BEGIN IMMEDIATE` for `_HOLD_SECONDS`, the
engine has a deliberately short `busy_timeout`, and we assert both that the
call completes AND that the rows are really in the file (no silent loss).
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.database import Base
from app.services import scrape_gateway_cache as cache
from app.services import scrape_keys

_HOLD_SECONDS = 0.35
T0 = 1_800_000_000_000


async def _init_wal_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal", f"WAL mode did not stick: {mode}"
    finally:
        conn.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _short_timeout_factory(db_path: str, busy_timeout_ms: int = 50):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO scrape_cache VALUES "
            "('movie', 1, 'fr-FR', 'movie|999999', 'found', 1, '{}', 1, 2)"
        )
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


def _start_blocker(db_path: str) -> threading.Thread:
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, _HOLD_SECONDS), daemon=True
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"
    return blocker


def _count(db_path: str, where: str = "1=1") -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM scrape_cache WHERE {where}").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
async def locked_db(tmp_path):
    db_path = str(tmp_path / "scrape_cache_lock.db")
    await _init_wal_schema(db_path)
    engine, factory = _short_timeout_factory(db_path)
    yield db_path, factory
    await engine.dispose()


async def test_put_many_survives_a_real_write_lock_and_writes_every_row(locked_db):
    db_path, factory = locked_db
    match = scrape_keys.match_key("movie", "Matrix", 1999, "fr-FR")
    entries = [
        (match, "matched", {"confidence": 0.95, "tmdbId": 603}, T0),
        (scrape_keys.movie_key(603, "fr-FR"), "found", {"title": "The Matrix"}, T0),
    ]
    blocker = _start_blocker(db_path)
    try:
        started = time.monotonic()
        out = await cache.put_many(entries, session_factory=factory)
        elapsed = time.monotonic() - started
    finally:
        blocker.join(timeout=5)

    assert len(out) == 2
    assert elapsed >= 0.1, "the write finished before the lock was released: no contention happened"
    # Not a silent no-op: both rows (and the blocker's own) are really in the file.
    assert _count(db_path, "family IN ('match','movie') AND cache_key != 'movie|999999'") == 2
    assert _count(db_path) == 3
    assert await cache.get(match, session_factory=factory, now=T0 + 1) is not None


async def test_invalidate_keys_survives_a_real_write_lock(locked_db):
    db_path, factory = locked_db
    keys = [scrape_keys.movie_key(i, "fr-FR") for i in (1, 2, 3)]
    await cache.put_many([(k, "found", {"x": 1}, T0) for k in keys], session_factory=factory)

    blocker = _start_blocker(db_path)
    try:
        deleted = await cache.invalidate_keys([k.to_text() for k in keys], session_factory=factory)
    finally:
        blocker.join(timeout=5)

    assert deleted == 3
    assert _count(db_path, "cache_key IN ('movie|1','movie|2','movie|3')") == 0


async def test_purge_survives_a_real_write_lock(locked_db):
    db_path, factory = locked_db
    k = scrape_keys.movie_key(7, "fr-FR")
    await cache.put(k, "found", {"x": 1}, T0, session_factory=factory)
    after_expiry = T0 + scrape_keys.ttl_success_ms() + 1

    blocker = _start_blocker(db_path)
    try:
        deleted, remaining = await cache.purge(10, now=after_expiry, session_factory=factory)
    finally:
        blocker.join(timeout=5)

    # The blocker's own row (expires_at=2) is purgeable too once it is committed.
    assert deleted >= 1
    assert _count(db_path, "cache_key = 'movie|7'") == 0
    assert remaining == 0
