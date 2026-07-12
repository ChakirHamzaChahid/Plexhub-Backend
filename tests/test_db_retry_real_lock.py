"""CR-T08: `db_retry.py` exercised against a REAL SQLite lock, not a
synthetic `OperationalError`.

`tests/test_db_retry.py` drives `run_with_retry`'s control flow with
hand-built `OperationalError("database is locked")` instances, and
`tests/conftest.py`'s `db_engine` fixture uses `:memory:` — where
`PRAGMA journal_mode=WAL` is a documented SQLite no-op (WAL requires a
real file; `:memory:` silently stays in `memory` journal mode). So the
production lock/retry path (`app/db/database.py` WAL + `busy_timeout`,
`app/utils/db_retry.py` retry) has never been exercised under genuine
writer-vs-writer contention.

This module creates a real file-backed (`tmp_path`) SQLite DB in WAL
mode with a deliberately short `busy_timeout`, holds a *real* write
lock from a second connection (`BEGIN IMMEDIATE` on a plain
background-thread `sqlite3` connection), and drives the retry helpers
against that genuine contention.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

import pytest
from sqlalchemy import Column, Integer, String, event
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.utils.db_retry import commit_with_retry, run_with_retry


class _RetryProbeBase(DeclarativeBase):
    """Isolated metadata for this module only — a throwaway table, not
    app.models.database.Base (we hand-build the file's schema below with a
    plain sqlite3 connection so the WAL header is set before any SQLAlchemy
    engine ever touches the file)."""


class _RetryProbe(_RetryProbeBase):
    __tablename__ = "retry_probe"
    id = Column(Integer, primary_key=True, autoincrement=True)
    value = Column(String)


def _init_wal_db(db_path: str) -> None:
    """Create the file, table, and WAL journal mode with a plain sqlite3
    connection (a real file — unlike `:memory:`, `PRAGMA journal_mode=WAL`
    actually sticks here and is read back by every later connection)."""
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal", f"WAL mode did not stick: {mode}"
        conn.execute("CREATE TABLE retry_probe (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)")
        conn.commit()
    finally:
        conn.close()


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    """Runs on a plain background thread with its OWN sqlite3 connection to
    the same file: opens a real write transaction (`BEGIN IMMEDIATE`
    acquires SQLite's RESERVED lock immediately, before any statement
    executes) and holds it for `hold_seconds` before committing. While
    held, any other writer against this file — including our
    aiosqlite/SQLAlchemy engine — gets a genuine `database is locked` raised
    by SQLite itself once its (short) `busy_timeout` elapses.
    """
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO retry_probe (value) VALUES ('blocker')")
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


class _ListHandler(logging.Handler):
    """Attached directly to `plexhub.db.retry` (not the root logger).

    `app/main.py:66` sets `logging.getLogger("plexhub").propagate = False`
    once `app.main` has been imported anywhere in the process (true for the
    full suite, since many other test modules import it) — after that, a
    root-attached `caplog` handler never sees records from any
    `plexhub.*` child logger (propagation stops at the "plexhub" node, one
    level below root). A handler added directly on the originating logger
    always fires regardless of that ancestor's `propagate` flag (Python's
    `Logger.callHandlers` invokes a logger's own handlers unconditionally
    before consulting `propagate` to decide whether to continue upward), so
    this is robust to running standalone or inside the full suite.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _make_engine(db_path: str, busy_timeout_ms: int = 50):
    """A file-based async engine with a deliberately SHORT `busy_timeout`.

    Production uses a 60s pool-level `busy_timeout` (CLAUDE.md piège #8);
    a short one here forces SQLite to surface `database is locked` quickly
    instead of silently absorbing the whole contention window inside its
    own internal wait, which would defeat the point of exercising the
    app-level retry within a fast test.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

    return engine


class TestRunWithRetryRealLock:
    """Real writer-vs-writer contention on a file-backed WAL database."""

    async def test_recovers_once_the_real_lock_is_released(self, tmp_path):
        """A second connection holds a genuine write lock for `hold_seconds`
        while `run_with_retry` is driven with a fresh-session-per-attempt
        factory — the calling convention documented on `run_with_retry`
        itself ("func must be a zero-arg coroutine factory so it can be
        re-invoked", app/utils/db_retry.py) — so each retry attempt is a
        clean, independent write/commit.

        Asserts: (1) a REAL `database is locked` was actually hit and
        logged by db_retry (not synthetic), (2) the retry genuinely waited
        (elapsed time reflects real contention, not an instant no-op),
        (3) the write ultimately committed once the lock was released,
        (4) the blocking thread finished cleanly.
        """
        db_path = str(tmp_path / "retry_lock.db")
        _init_wal_db(db_path)

        hold_seconds = 0.35
        lock_acquired = threading.Event()
        blocker = threading.Thread(
            target=_hold_write_lock,
            args=(db_path, lock_acquired, hold_seconds),
            daemon=True,
        )
        blocker.start()
        assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

        engine = _make_engine(db_path)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        attempts = {"n": 0}

        async def _write_once():
            attempts["n"] += 1
            async with factory() as session:
                session.add(_RetryProbe(value="writer"))
                await session.commit()

        retry_logger = logging.getLogger("plexhub.db.retry")
        list_handler = _ListHandler()
        retry_logger.addHandler(list_handler)
        try:
            start = time.monotonic()
            # Short delays (well below production's 60s busy_timeout budget) keep this hermetic and fast.
            await run_with_retry(_write_once, delays=(0.1, 0.2, 0.4), op="test-write")
            elapsed = time.monotonic() - start
        finally:
            retry_logger.removeHandler(list_handler)

        blocker.join(timeout=5)
        await engine.dispose()

        assert not blocker.is_alive()
        assert attempts["n"] >= 2, "expected at least one real retry, not an immediate first-try success"
        # Real contention takes real wall-clock time — a no-op/synthetic retry would return near-instantly.
        assert elapsed >= hold_seconds * 0.5

        locked_warnings = [
            r for r in list_handler.records if "database is locked" in r.getMessage().lower()
        ]
        assert locked_warnings, "expected db_retry to log a REAL 'database is locked' warning"

        check = sqlite3.connect(db_path)
        try:
            rows = check.execute("SELECT value FROM retry_probe ORDER BY id").fetchall()
        finally:
            check.close()
        assert ("writer",) in rows
        assert ("blocker",) in rows

    async def test_no_spurious_retry_without_contention(self, tmp_path):
        """Control case: with no concurrent writer, the same file-based WAL
        engine + `run_with_retry` succeeds on the very first attempt — the
        file-based setup itself does not manufacture false contention."""
        db_path = str(tmp_path / "retry_no_contention.db")
        _init_wal_db(db_path)

        engine = _make_engine(db_path)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        attempts = {"n": 0}

        async def _write_once():
            attempts["n"] += 1
            async with factory() as session:
                session.add(_RetryProbe(value="solo"))
                await session.commit()

        await run_with_retry(_write_once, delays=(0.1,), op="test-write-solo")
        await engine.dispose()

        assert attempts["n"] == 1


class TestCommitWithRetrySameSessionBoundary:
    """Documents a real, verified boundary discovered while building this
    real-lock coverage for CR-T08 (not itself part of CR-T08's ask, and NOT
    fixed here — app/ is out of scope for this pass; flagged for a
    follow-up finding).

    Every production call site uses the pattern `db.add(...)` followed by a
    bare `await commit_with_retry(db)` on the SAME `AsyncSession` (e.g.
    `app/api/accounts.py:42`, `app/workers/sync_worker.py:1085`). Under a
    REAL lock this behaves differently from the fresh-session-per-attempt
    pattern above: SQLAlchemy invalidates a Session's transaction after its
    first failed flush/commit, so retry attempt #2 on the *same* session
    raises `PendingRollbackError` (a session-state guard, not another
    `OperationalError`) — and `run_with_retry`'s `except OperationalError`
    does not catch it, so the retry loop aborts after exactly one real lock
    hit instead of recovering. This is deterministic (it does not depend on
    whether the lock is still held by the time the retry fires) so it is
    locked in here as CURRENT, observed behaviour rather than left as an
    invisible gap.
    """

    async def test_same_session_retry_does_not_survive_a_real_lock(self, tmp_path):
        db_path = str(tmp_path / "retry_same_session.db")
        _init_wal_db(db_path)

        hold_seconds = 0.35
        lock_acquired = threading.Event()
        blocker = threading.Thread(
            target=_hold_write_lock,
            args=(db_path, lock_acquired, hold_seconds),
            daemon=True,
        )
        blocker.start()
        assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

        engine = _make_engine(db_path)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with factory() as session:
            session.add(_RetryProbe(value="writer"))
            with pytest.raises(PendingRollbackError):
                await commit_with_retry(session, delays=(0.1, 0.2, 0.4))

        blocker.join(timeout=5)
        await engine.dispose()
        assert not blocker.is_alive()
