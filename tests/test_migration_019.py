"""Guard test for migration 019: the Plex shared-servers catalogue tables
(plex_server / plex_media_item / plex_sync_status — feature "Télécharger
Plex", Tâche C1 fondations).

Covers a fresh DB (create_all already made them -> migration is a no-op),
idempotency (double run), an upgraded DB that lacks the tables (migration
backfills them), and the CR-C05 invariant: create_all THEN run_migrations on
a fresh DB must not raise.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import _migration_019_create_plex_tables, run_migrations
from app.models.database import Base

EXPECTED_TABLES = {"plex_server", "plex_media_item", "plex_sync_status"}
EXPECTED_INDEXES = {
    "ix_plex_item_type_unif",
    "ix_plex_item_show",
    "ix_plex_item_type_added",
    "ix_plex_item_type_title",
}


async def _table_names(conn) -> set[str]:
    rows = (await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    )).fetchall()
    return {r[0] for r in rows}


async def _index_names(conn) -> set[str]:
    rows = (await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' "
             "AND tbl_name IN ('plex_server','plex_media_item','plex_sync_status')")
    )).fetchall()
    return {r[0] for r in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_migration_019_noop_on_fresh_db_and_idempotent(fresh_engine):
    # create_all already built the tables; the migration must run cleanly
    # (no-op) and be safely re-runnable.
    await _migration_019_create_plex_tables(fresh_engine)
    await _migration_019_create_plex_tables(fresh_engine)
    async with fresh_engine.begin() as conn:
        tables = await _table_names(conn)
        indexes = await _index_names(conn)
    assert EXPECTED_TABLES <= tables
    assert EXPECTED_INDEXES <= indexes


async def test_migration_019_backfills_on_upgraded_db(tmp_path):
    """A DB that predates the Plex tables (they don't exist) must get them
    created by the migration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'up.db'}", future=True)
    try:
        # Build every table EXCEPT the three Plex tables to simulate an old DB.
        async with engine.begin() as conn:
            for name, table in Base.metadata.tables.items():
                if name in EXPECTED_TABLES:
                    continue
                await conn.run_sync(table.create)
        async with engine.begin() as conn:
            assert not (EXPECTED_TABLES <= await _table_names(conn))

        await _migration_019_create_plex_tables(engine)

        async with engine.begin() as conn:
            tables = await _table_names(conn)
            indexes = await _index_names(conn)
        assert EXPECTED_TABLES <= tables
        assert EXPECTED_INDEXES <= indexes
    finally:
        await engine.dispose()


async def test_migration_019_plex_media_item_composite_pk_and_index_column(tmp_path):
    """Spot-check the composite PK (server_id, rating_key) and the reserved
    word "index" column survive a raw-DDL run (not just the ORM's create_all)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'raw.db'}", future=True)
    try:
        await _migration_019_create_plex_tables(engine)

        async with engine.begin() as conn:
            cols = (await conn.execute(text('PRAGMA table_info(plex_media_item)'))).fetchall()
            col_names = {row[1] for row in cols}
            assert "index" in col_names
            assert "server_id" in col_names
            assert "rating_key" in col_names
            pk_cols = {row[1] for row in cols if row[5] > 0}  # pk column index (>0 means part of PK)
            assert pk_cols == {"server_id", "rating_key"}

            # Insert with the quoted reserved-word column to prove the DDL
            # actually created it usable (not just present in table_info).
            await conn.execute(text(
                'INSERT INTO plex_media_item '
                '(server_id, rating_key, type, title, "index", synced_at) '
                "VALUES ('plex_abc', '123', 'episode', 'Ep 1', 4, 1000)"
            ))
            row = (await conn.execute(text(
                'SELECT "index" FROM plex_media_item WHERE server_id=\'plex_abc\' AND rating_key=\'123\''
            ))).fetchone()
            assert row[0] == 4
    finally:
        await engine.dispose()


async def test_migration_019_plex_sync_status_singleton_check_constraint(tmp_path):
    """The id CHECK (id = 1) must reject a second row (singleton invariant)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'singleton.db'}", future=True)
    try:
        await _migration_019_create_plex_tables(engine)

        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO plex_sync_status (id, state) VALUES (1, 'idle')"
            ))

        raised = False
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO plex_sync_status (id, state) VALUES (2, 'idle')"
                ))
        except Exception:
            raised = True
        assert raised, "CHECK (id = 1) should reject a non-1 primary key"
    finally:
        await engine.dispose()


async def test_run_migrations_full_chain_creates_plex_tables_on_upgraded_db(tmp_path):
    """The full run_migrations() chain (001->019) must create the Plex
    tables on a DB that predates migration 019, without raising, mirroring
    how a real upgraded deployment boots."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chain.db'}", future=True)
    register_sqlite_vec_listener(engine)
    try:
        async with engine.begin() as conn:
            for name, table in Base.metadata.tables.items():
                if name in EXPECTED_TABLES:
                    continue
                await conn.run_sync(table.create)

        await run_migrations(engine)

        async with engine.begin() as conn:
            tables = await _table_names(conn)
        assert EXPECTED_TABLES <= tables
    finally:
        await engine.dispose()


async def test_run_migrations_on_fresh_create_all_db_is_noop_for_plex_tables(tmp_path):
    """CR-C05 invariant: Base.metadata.create_all() THEN run_migrations() on
    a brand-new DB must not raise (migration 019 sees the tables already
    present via create_all and no-ops)."""
    db_path = tmp_path / "fresh_full.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        await run_migrations(engine)

        async with engine.begin() as conn:
            tables = await _table_names(conn)
            indexes = await _index_names(conn)
        assert EXPECTED_TABLES <= tables
        assert EXPECTED_INDEXES <= indexes
    finally:
        await engine.dispose()
