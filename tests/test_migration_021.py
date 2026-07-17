"""Guard test for migration 021: the `plex_media_item.genres` column
(feature "écran de téléchargement unifié Plex+Xtream", Vague W1).

`genres` (TEXT, comma-separated Plex `Genre[].tag`, nullable) is purely
additive and never backfilled by the migration itself — it stays NULL until
the next Plex catalogue sync re-upserts each row with the value captured by
`plex_api_service.parse_genres`. Powers genre-filter parity with the Xtream
`media.genres` column on the unified download screen.

Covers: a fresh DB (create_all already has the column -> migration is a
no-op), idempotency (double run), an upgraded DB that predates the column
(migration ADD COLUMNs it), the CR-C05 invariant (create_all THEN
run_migrations on a fresh DB must not raise), and a write/read round-trip.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import (
    _column_exists,
    _migration_021_add_plex_media_item_genres,
    run_migrations,
)
from app.models.database import Base


async def _plex_item_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(plex_media_item)"))).fetchall()
    return {row[1] for row in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    """A brand-new DB built the same way ``init_db()`` builds one: create_all
    first, so `genres` already exists before any migration runs (CR-C05)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def upgraded_engine(tmp_path):
    """A `plex_media_item` table with every current ORM column EXCEPT
    `genres` — stand-in for a DB that predates migration 021. Built via
    create_all + DROP COLUMN so every other column/index matches production."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE plex_media_item DROP COLUMN genres"))
    yield engine
    await engine.dispose()


async def test_migration_021_noop_on_fresh_db_and_idempotent(fresh_engine):
    await _migration_021_add_plex_media_item_genres(fresh_engine)
    await _migration_021_add_plex_media_item_genres(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert "genres" in await _plex_item_columns(conn)


async def test_migration_021_backfills_on_upgraded_db(upgraded_engine):
    async with upgraded_engine.connect() as conn:
        assert "genres" not in await _plex_item_columns(conn)

    await _migration_021_add_plex_media_item_genres(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert "genres" in await _plex_item_columns(conn)

    # Re-running after the column has been added must not raise either.
    await _migration_021_add_plex_media_item_genres(upgraded_engine)
    async with upgraded_engine.connect() as conn:
        assert "genres" in await _plex_item_columns(conn)


async def test_column_exists_helper_detects_genres(fresh_engine, upgraded_engine):
    async with fresh_engine.connect() as conn:
        assert await _column_exists(conn, "plex_media_item", "genres") is True
    async with upgraded_engine.connect() as conn:
        assert await _column_exists(conn, "plex_media_item", "genres") is False


async def test_run_migrations_full_chain_backfills_genres_on_upgraded_db(upgraded_engine):
    """The full run_migrations() chain (001->021) must add genres to an
    upgraded DB that predates migration 021, without raising."""
    await run_migrations(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert "genres" in await _plex_item_columns(conn)


async def test_run_migrations_on_fresh_create_all_db_is_noop_for_genres(fresh_engine):
    """CR-C05 invariant: create_all() THEN run_migrations() on a brand-new DB
    must not raise (migration 021 sees genres already present and no-ops)."""
    await run_migrations(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert "genres" in await _plex_item_columns(conn)


async def test_genres_write_and_read_roundtrip(fresh_engine):
    """`genres` must be writable/readable (the Plex sync writes the joined
    `Genre[].tag` here) and nullable when unset (rows synced before M021)."""
    table = Base.metadata.tables["plex_media_item"]

    async with fresh_engine.begin() as conn:
        await conn.execute(table.insert(), {
            "server_id": "plex_cid", "rating_key": "1", "type": "movie",
            "title": "Genred", "unification_id": "tt1", "genres": "Action, Sci-Fi",
            "synced_at": 1,
        })
        await conn.execute(table.insert(), {
            "server_id": "plex_cid", "rating_key": "2", "type": "movie",
            "title": "No genre", "unification_id": "tt2", "synced_at": 1,
        })

    async with fresh_engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT genres FROM plex_media_item WHERE rating_key='1'")
        )).fetchone()
        assert row[0] == "Action, Sci-Fi"
        row_null = (await conn.execute(
            text("SELECT genres FROM plex_media_item WHERE rating_key='2'")
        )).fetchone()
        assert row_null[0] is None
