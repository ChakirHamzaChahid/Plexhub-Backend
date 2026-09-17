"""Guard test for migration 025: the provider-outage columns on
`xtream_accounts` (`outage_strikes` / `outage_since`, feature "panne
fournisseur prolongée").

Covers a fresh DB (create_all already made them -> the migration is a no-op),
idempotency (double run), an upgraded DB that predates the columns (the
migration backfills them, existing rows keep working), and the CR-C05
invariant: create_all THEN run_migrations on a fresh DB must not raise.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import (
    _migration_025_add_account_outage_tracking,
    run_migrations,
)
from app.models.database import Base

EXPECTED_COLUMNS = {"outage_strikes", "outage_since"}

# The pre-025 shape of the table, as an upgraded production DB still has it.
LEGACY_DDL = """
CREATE TABLE xtream_accounts (
    id TEXT NOT NULL PRIMARY KEY,
    label TEXT NOT NULL,
    base_url TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    status TEXT NOT NULL,
    expiration_date BIGINT,
    max_connections INTEGER NOT NULL,
    allowed_formats TEXT NOT NULL,
    server_url TEXT,
    https_port INTEGER,
    last_synced_at BIGINT NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at BIGINT NOT NULL,
    category_filter_mode TEXT NOT NULL
)
"""


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text('PRAGMA table_info("xtream_accounts")'))).fetchall()
    return {r[1] for r in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'o.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def legacy_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}", future=True)
    async with engine.begin() as conn:
        await conn.execute(text(LEGACY_DDL))
        await conn.execute(text(
            "INSERT INTO xtream_accounts VALUES "
            "('acc1', 'Acc 1', 'http://acc1.test', 80, 'u', 'p', 'Active', "
            " NULL, 1, '', NULL, NULL, 0, 1, 0, 'all')"
        ))
    yield engine
    await engine.dispose()


async def test_migration_025_noop_on_fresh_db_and_idempotent(fresh_engine):
    await _migration_025_add_account_outage_tracking(fresh_engine)
    await _migration_025_add_account_outage_tracking(fresh_engine)
    async with fresh_engine.begin() as conn:
        assert EXPECTED_COLUMNS <= await _columns(conn)


async def test_migration_025_backfills_an_upgraded_db(legacy_engine):
    async with legacy_engine.begin() as conn:
        assert not (EXPECTED_COLUMNS & await _columns(conn))

    await _migration_025_add_account_outage_tracking(legacy_engine)

    async with legacy_engine.begin() as conn:
        assert EXPECTED_COLUMNS <= await _columns(conn)
        # Existing rows land in the neutral state — never "in outage" just
        # because the column appeared.
        row = (await conn.execute(text(
            "SELECT outage_strikes, outage_since FROM xtream_accounts WHERE id='acc1'"
        ))).fetchone()
        assert row == (0, None)


async def test_migration_025_leaves_the_table_writable_by_raw_sql(legacy_engine):
    """`outage_strikes` is NOT NULL, so it must carry a SQL-level DEFAULT:
    raw INSERTs that enumerate columns (scripts, tests, ops one-shots) predate
    this feature and must keep working without knowing about it."""
    await _migration_025_add_account_outage_tracking(legacy_engine)

    async with legacy_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO xtream_accounts "
            "(id, label, base_url, port, username, password, status, "
            " max_connections, allowed_formats, last_synced_at, is_active, "
            " created_at, category_filter_mode) "
            "VALUES ('acc2', 'Acc 2', 'http://acc2.test', 80, 'u', 'p', "
            " 'Active', 1, '', 0, 1, 0, 'all')"
        ))
        strikes = (await conn.execute(text(
            "SELECT outage_strikes FROM xtream_accounts WHERE id='acc2'"
        ))).scalar()
    assert strikes == 0


async def test_run_migrations_full_chain_on_fresh_db(tmp_path):
    """CR-C05: create_all THEN the whole 001->025 chain must not raise (and
    must not warn about duplicate columns)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chain.db'}", future=True)
    register_sqlite_vec_listener(engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await run_migrations(engine)
        await run_migrations(engine)  # rejouable
        async with engine.begin() as conn:
            assert EXPECTED_COLUMNS <= await _columns(conn)
    finally:
        await engine.dispose()
