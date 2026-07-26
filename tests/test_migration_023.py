"""Guard tests for migration 023 (one-shot `ANALYZE`, AUDIT-P3-001) and
`app.db.maintenance`.

Covers: `sqlite_stat1` present after `run_migrations()` on a fresh DB,
idempotent re-run (twice in a row, no error/warning), and the "never
fatal" contract of `run_analyze`/`run_sqlite_maintenance` under a REAL
SQLite write lock (mirrors the harness in `tests/test_db_retry_real_lock.py`
— a plain `sqlite3` connection on a background thread holds a genuine
`BEGIN IMMEDIATE` lock so `ANALYZE` actually hits `database is locked`,
not a synthetic exception).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.maintenance import run_analyze, run_sqlite_maintenance
from app.db.migrations import _migration_023_analyze, run_migrations
from app.models.database import Base

MIGRATIONS_LOGGER = "app.db.migrations"
MAINTENANCE_LOGGER = "plexhub.db.maintenance"


async def _table_exists(conn, table: str) -> bool:
    row = (await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    )).fetchone()
    return row is not None


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    """A brand-new DB built the same way ``init_db()`` builds one: create_all
    first (mirrors production boot order — migration 023 always runs after
    the table set already exists)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_migration_023_creates_sqlite_stat1_on_fresh_db(fresh_engine):
    await _migration_023_analyze(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert await _table_exists(conn, "sqlite_stat1") is True


async def test_migration_023_idempotent_rerun_no_error(fresh_engine, caplog):
    await _migration_023_analyze(fresh_engine)

    with caplog.at_level(logging.WARNING, logger=MAINTENANCE_LOGGER):
        await _migration_023_analyze(fresh_engine)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"unexpected warnings on ANALYZE re-run: {warnings}"

    async with fresh_engine.connect() as conn:
        assert await _table_exists(conn, "sqlite_stat1") is True


async def test_run_migrations_full_chain_populates_stat1_on_fresh_db(fresh_engine):
    """The full 001->023 chain must run end to end on a fresh (create_all)
    DB and leave sqlite_stat1 present."""
    await run_migrations(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert await _table_exists(conn, "sqlite_stat1") is True


async def test_run_migrations_rerun_twice_no_error_no_warning(fresh_engine, caplog):
    """CLAUDE.md piège 6: re-running the whole chain (simulating a second
    worker process's init_db()) must not raise, and migration 023 itself
    must not warn on its own idempotent re-run."""
    await run_migrations(fresh_engine)

    with caplog.at_level(logging.WARNING, logger=MIGRATIONS_LOGGER):
        await run_migrations(fresh_engine)

    dupes = [
        r.getMessage() for r in caplog.records
        if "Migration 023" in r.getMessage() and r.levelno >= logging.WARNING
    ]
    assert not dupes, f"unexpected migration 023 warnings on re-run: {dupes}"

    async with fresh_engine.connect() as conn:
        assert await _table_exists(conn, "sqlite_stat1") is True


def _init_wal_db(db_path: str) -> None:
    """Create the file + WAL journal mode with a plain sqlite3 connection —
    a real file (unlike `:memory:`) so `PRAGMA journal_mode=WAL` actually
    sticks and is honoured by every later connection, including our
    aiosqlite engine."""
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal", f"WAL mode did not stick: {mode}"
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)")
        conn.commit()
    finally:
        conn.close()


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    """Runs on a background thread with its OWN sqlite3 connection: opens a
    real write transaction (`BEGIN IMMEDIATE`, a genuine RESERVED lock) and
    holds it — any other writer against this file (our async engine's
    `ANALYZE`) gets a real `database is locked` once its (short)
    busy_timeout elapses."""
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO probe (value) VALUES ('blocker')")
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


def _make_short_timeout_engine(db_path: str, busy_timeout_ms: int = 50):
    """A file-based async engine with a deliberately SHORT `busy_timeout` —
    production uses 60s (CLAUDE.md piège 8); a short one here forces
    SQLite to surface `database is locked` quickly instead of absorbing the
    whole contention window internally."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

    return engine


class _ListHandler(logging.Handler):
    """Attached directly to the maintenance logger — `app.main` sets
    `plexhub`'s `propagate=False` once imported anywhere in the process, so
    a root-attached `caplog` handler would miss `plexhub.*` child records
    when the full suite runs. A handler on the logger itself always fires
    (`Logger.callHandlers` runs a logger's own handlers unconditionally
    before consulting `propagate`)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestMaintenanceNeverFatalUnderRealLock:
    """CLAUDE.md piège 8/11: a stats refresh must survive genuine SQLite
    write contention without ever raising — `busy_timeout` gives it a
    window to succeed, and if it still can't, the failure is swallowed and
    logged, never propagated."""

    async def test_run_analyze_does_not_raise_under_real_lock(self, tmp_path):
        db_path = str(tmp_path / "locked.db")
        _init_wal_db(db_path)

        hold_seconds = 0.35
        lock_acquired = threading.Event()
        blocker = threading.Thread(
            target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
        )
        blocker.start()
        assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

        engine = _make_short_timeout_engine(db_path)
        maint_logger = logging.getLogger(MAINTENANCE_LOGGER)
        list_handler = _ListHandler()
        maint_logger.addHandler(list_handler)
        try:
            # Must not raise, even though the short busy_timeout (50ms) is
            # well under the 350ms the blocker holds the lock for.
            await run_analyze(engine)
        finally:
            maint_logger.removeHandler(list_handler)
            await engine.dispose()
            blocker.join(timeout=5)

        assert any("ANALYZE failed" in r.getMessage() for r in list_handler.records), (
            "expected a logged warning for the real lock hit"
        )

    async def test_run_sqlite_maintenance_does_not_raise_under_real_lock(self, tmp_path):
        db_path = str(tmp_path / "locked2.db")
        _init_wal_db(db_path)

        hold_seconds = 0.35
        lock_acquired = threading.Event()
        blocker = threading.Thread(
            target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
        )
        blocker.start()
        assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

        engine = _make_short_timeout_engine(db_path)
        try:
            # Both ANALYZE and PRAGMA optimize must swallow the lock error.
            await run_sqlite_maintenance(engine)
        finally:
            await engine.dispose()
            blocker.join(timeout=5)


async def test_run_sqlite_maintenance_succeeds_without_contention(fresh_engine):
    """Happy path: no lock held -> both statements succeed, sqlite_stat1
    ends up populated."""
    await run_sqlite_maintenance(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert await _table_exists(conn, "sqlite_stat1") is True
