"""Guard test for migration 020: the `media.file_size` column (extension
"download Xtream granulaire + taille", Tâche X1a fondation).

`file_size` (BIGINT, bytes, nullable) is purely additive and never
backfilled by the migration itself — it stays NULL until the health-check
worker (Tâche X1b) captures the HEAD request's `Content-Length` header.

Covers: a fresh DB (create_all already has the column -> migration is a
no-op), idempotency (double run), an upgraded DB that predates the column
(migration ADD COLUMNs it), the CR-C05 invariant (create_all THEN
run_migrations on a fresh DB must not raise), and a write/read round-trip
proving the column is usable (nullable by default, storable when set).
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import (
    _column_exists,
    _migration_020_add_media_file_size,
    run_migrations,
)
from app.models.database import Base


async def _media_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(media)"))).fetchall()
    return {row[1] for row in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    """A brand-new DB built the same way ``init_db()`` builds one in
    production: ``Base.metadata.create_all`` first, so `file_size` already
    exists before any migration runs (CR-C05)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def upgraded_engine(tmp_path):
    """A `media` table with every current ORM column EXCEPT `file_size` —
    stand-in for a database that predates migration 020. Built via
    create_all + DROP COLUMN (SQLite 3.35+) so every other column/index
    matches production exactly; migration 020 itself only ever ADDs.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE media DROP COLUMN file_size"))
    yield engine
    await engine.dispose()


async def test_migration_020_noop_on_fresh_db_and_idempotent(fresh_engine):
    # create_all already added file_size; the migration must run cleanly
    # (no-op) and be safely re-runnable.
    await _migration_020_add_media_file_size(fresh_engine)
    await _migration_020_add_media_file_size(fresh_engine)

    async with fresh_engine.connect() as conn:
        cols = await _media_columns(conn)
    assert "file_size" in cols


async def test_migration_020_backfills_on_upgraded_db(upgraded_engine):
    async with upgraded_engine.connect() as conn:
        assert "file_size" not in await _media_columns(conn)

    await _migration_020_add_media_file_size(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert "file_size" in await _media_columns(conn)

    # Re-running after the column has been added must not raise either.
    await _migration_020_add_media_file_size(upgraded_engine)
    async with upgraded_engine.connect() as conn:
        assert "file_size" in await _media_columns(conn)


async def test_column_exists_helper_detects_file_size(fresh_engine, upgraded_engine):
    async with fresh_engine.connect() as conn:
        assert await _column_exists(conn, "media", "file_size") is True
    async with upgraded_engine.connect() as conn:
        assert await _column_exists(conn, "media", "file_size") is False


async def test_run_migrations_full_chain_backfills_file_size_on_upgraded_db(upgraded_engine):
    """The full run_migrations() chain (001->020) must add file_size to an
    upgraded DB that predates migration 020, without raising, mirroring how
    a real upgraded deployment boots."""
    await run_migrations(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert "file_size" in await _media_columns(conn)


async def test_run_migrations_on_fresh_create_all_db_is_noop_for_file_size(fresh_engine):
    """CR-C05 invariant: Base.metadata.create_all() THEN run_migrations() on
    a brand-new DB must not raise (migration 020 sees file_size already
    present via create_all and no-ops)."""
    await run_migrations(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert "file_size" in await _media_columns(conn)


async def test_file_size_write_and_read_roundtrip(fresh_engine):
    """`file_size` must be writable/readable (the health-check worker,
    Tâche X1b, writes the HEAD Content-Length here) and nullable when unset
    (unvalidated rows)."""
    media_table = Base.metadata.tables["media"]

    async with fresh_engine.begin() as conn:
        await conn.execute(media_table.insert(), {
            "rating_key": "rk_size",
            "server_id": "s1",
            "filter": "all",
            "sort_order": "default",
            "library_section_id": "lib1",
            "title": "Sized Movie",
            "type": "movie",
            "file_size": 123456789,
        })
        await conn.execute(media_table.insert(), {
            "rating_key": "rk_null",
            "server_id": "s1",
            "filter": "all",
            "sort_order": "default",
            "library_section_id": "lib2",
            "title": "Unvalidated Movie",
            "type": "movie",
            # file_size intentionally omitted -> must default to NULL.
        })

    async with fresh_engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT file_size FROM media WHERE rating_key='rk_size'")
        )).fetchone()
        assert row[0] == 123456789

        row_null = (await conn.execute(
            text("SELECT file_size FROM media WHERE rating_key='rk_null'")
        )).fetchone()
        assert row_null[0] is None
