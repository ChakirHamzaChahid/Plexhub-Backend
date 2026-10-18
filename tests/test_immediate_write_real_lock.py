"""B2-01 (risque R1, ADR 0011 § D2.4) — `immediate_write_session` sous un VRAI
verrou d'écriture SQLite.

Le moteur de test reprend la configuration de production (`timeout=60` sur la
connexion). Le point à prouver : l'échéance de l'API (secondes) l'emporte sur ce
`busy_timeout` de 60 s *pour cette transaction seulement*, et la connexion rendue
au pool retrouve sa valeur d'origine.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.immediate_tx import DatabaseBusy, immediate_write_session

PROD_BUSY_MS = 60_000


def _init_wal_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
        conn.execute("INSERT INTO counter (id, n) VALUES (1, 0)")
        conn.commit()
    finally:
        conn.close()


def _hold_write_lock(db_path: str, acquired: threading.Event, hold_seconds: float) -> None:
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        acquired.set()
        time.sleep(hold_seconds)
        conn.rollback()
    finally:
        conn.close()


@pytest.fixture
async def factory(tmp_path):
    db_path = str(tmp_path / "imm.db")
    _init_wal_db(db_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 60},
    )
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), db_path
    await engine.dispose()


def _hold(db_path: str, seconds: float) -> threading.Thread:
    acquired = threading.Event()
    t = threading.Thread(target=_hold_write_lock, args=(db_path, acquired, seconds), daemon=True)
    t.start()
    assert acquired.wait(timeout=5), "blocker never acquired the write lock"
    return t


async def _n(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT n FROM counter WHERE id = 1"))).scalar_one()


async def test_commit_persists_and_busy_timeout_is_restored(factory):
    f, _ = factory
    async with immediate_write_session(f, deadline_s=5) as tx:
        await tx.session.execute(text("UPDATE counter SET n = n + 1 WHERE id = 1"))
        inside = (await tx.session.execute(text("PRAGMA busy_timeout"))).scalar_one()
    assert inside == PROD_BUSY_MS  # restored as soon as the lock is held
    async with f() as s:
        assert await _n(s) == 1
        assert (await s.execute(text("PRAGMA busy_timeout"))).scalar_one() == PROD_BUSY_MS


async def test_exception_rolls_back(factory):
    f, _ = factory
    with pytest.raises(RuntimeError):
        async with immediate_write_session(f, deadline_s=5) as tx:
            await tx.session.execute(text("UPDATE counter SET n = 99 WHERE id = 1"))
            raise RuntimeError("boom")
    async with f() as s:
        assert await _n(s) == 0


async def test_gives_up_at_the_deadline_despite_the_60s_connection_timeout(factory):
    f, db_path = factory
    blocker = _hold(db_path, 3.0)
    started = time.monotonic()
    with pytest.raises(DatabaseBusy):
        async with immediate_write_session(f, deadline_s=0.4) as tx:
            await tx.session.execute(text("UPDATE counter SET n = 7 WHERE id = 1"))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, elapsed  # NOT 60 s
    blocker.join(timeout=6)
    async with f() as s:
        assert await _n(s) == 0
        # the pooled connection did not keep the short deadline
        assert (await s.execute(text("PRAGMA busy_timeout"))).scalar_one() == PROD_BUSY_MS


async def test_waits_for_a_lock_released_before_the_deadline(factory):
    f, db_path = factory
    blocker = _hold(db_path, 0.4)
    async with immediate_write_session(f, deadline_s=5) as tx:
        await tx.session.execute(text("UPDATE counter SET n = 5 WHERE id = 1"))
    blocker.join(timeout=5)
    async with f() as s:
        assert await _n(s) == 5


async def test_two_writers_serialise_and_the_second_reads_the_first(factory):
    """No lost update: B's read happens AFTER it obtained the lock, hence sees A's commit."""
    f, _ = factory
    entered = asyncio.Event()

    async def writer(slow: bool) -> int:
        async with immediate_write_session(f, deadline_s=10) as tx:
            seen = await _n(tx.session)
            if slow:
                entered.set()
                await asyncio.sleep(0.4)
            await tx.session.execute(text("UPDATE counter SET n = :n WHERE id = 1"), {"n": seen + 1})
            return seen

    a = asyncio.create_task(writer(True))
    await entered.wait()
    b = asyncio.create_task(writer(False))
    seen_a, seen_b = await asyncio.gather(a, b)
    assert (seen_a, seen_b) == (0, 1)
    async with f() as s:
        assert await _n(s) == 2


async def test_check_deadline_raises_and_rolls_back(factory):
    f, _ = factory
    with pytest.raises(DatabaseBusy):
        async with immediate_write_session(f, deadline_s=0.2) as tx:
            await tx.session.execute(text("UPDATE counter SET n = 3 WHERE id = 1"))
            await asyncio.sleep(0.35)
            tx.check_deadline()
    async with f() as s:
        assert await _n(s) == 0


async def test_a_reader_is_not_blocked_by_the_write_transaction(factory):
    f, _ = factory
    async with immediate_write_session(f, deadline_s=5) as tx:
        await tx.session.execute(text("UPDATE counter SET n = 8 WHERE id = 1"))
        async with f() as reader:
            assert await _n(reader) == 0  # WAL: the reader sees the committed state
    async with f() as s:
        assert await _n(s) == 8
