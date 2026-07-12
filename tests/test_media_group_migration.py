"""Guard test for migration 017: the CR-P01 media_group snapshot tables.

Covers a fresh DB (create_all already made them -> migration is a no-op),
idempotency (double run), and an upgraded DB that lacks the tables (migration
backfills them).
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrations import _migration_017_create_media_group_snapshot
from app.models.database import Base

EXPECTED_TABLES = {"media_group", "media_group_member"}
# Only media_group carries an explicit secondary index; media_group_member
# relies on its composite-PK autoindex for the page lookup.
EXPECTED_INDEXES = {"ix_media_group_type_sort"}


async def _table_names(conn) -> set[str]:
    rows = (await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    )).fetchall()
    return {r[0] for r in rows}


async def _index_names(conn) -> set[str]:
    rows = (await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' "
             "AND tbl_name IN ('media_group','media_group_member')")
    )).fetchall()
    return {r[0] for r in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'g.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_migration_017_noop_on_fresh_db_and_idempotent(fresh_engine):
    # create_all already built the tables; the migration must run cleanly (no-op)
    # and be safely re-runnable.
    await _migration_017_create_media_group_snapshot(fresh_engine)
    await _migration_017_create_media_group_snapshot(fresh_engine)
    async with fresh_engine.begin() as conn:
        tables = await _table_names(conn)
        indexes = await _index_names(conn)
    assert EXPECTED_TABLES <= tables
    assert EXPECTED_INDEXES <= indexes


async def test_migration_017_backfills_on_upgraded_db(tmp_path):
    """A DB that predates the snapshot tables (they don't exist) must get them
    created by the migration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'up.db'}", future=True)
    try:
        # Build every table EXCEPT the two snapshot tables to simulate an old DB.
        async with engine.begin() as conn:
            for name, table in Base.metadata.tables.items():
                if name in EXPECTED_TABLES:
                    continue
                await conn.run_sync(table.create)
        async with engine.begin() as conn:
            assert not (EXPECTED_TABLES <= await _table_names(conn))

        await _migration_017_create_media_group_snapshot(engine)

        async with engine.begin() as conn:
            tables = await _table_names(conn)
            indexes = await _index_names(conn)
        assert EXPECTED_TABLES <= tables
        assert EXPECTED_INDEXES <= indexes
    finally:
        await engine.dispose()
