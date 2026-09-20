"""Guard test for migration 026: the manual-scrape lock columns on `media`
(`match_locked` / `match_source`, ADR 0005 D7 "verrou").

Covers a fresh DB (create_all already made them -> the migration is a no-op),
idempotency (double run), an upgraded DB that predates the columns (the
migration backfills them, existing rows keep working), the migration-025
lesson (a raw INSERT enumerating columns must not break on a NOT NULL column
without a SQL DEFAULT), and the CR-C05 invariant: create_all THEN
run_migrations on a fresh DB must not raise.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import (
    _column_exists,
    _migration_026_add_media_match_lock,
    run_migrations,
)
from app.models.database import Base

EXPECTED_COLUMNS = {"match_locked", "match_source"}


async def _media_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(media)"))).fetchall()
    return {row[1] for row in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    """A brand-new DB built the same way ``init_db()`` builds one: create_all
    first, so the lock columns already exist before any migration runs
    (CR-C05)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def upgraded_engine(tmp_path):
    """A `media` table with every current ORM column EXCEPT the lock columns
    — stand-in for a DB that predates migration 026. Built via create_all +
    DROP COLUMN so every other column/index matches production."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE media DROP COLUMN match_locked"))
        await conn.execute(text("ALTER TABLE media DROP COLUMN match_source"))
    yield engine
    await engine.dispose()


async def test_migration_026_noop_on_fresh_db_and_idempotent(fresh_engine):
    await _migration_026_add_media_match_lock(fresh_engine)
    await _migration_026_add_media_match_lock(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert EXPECTED_COLUMNS <= await _media_columns(conn)


async def test_migration_026_backfills_an_upgraded_db(upgraded_engine):
    async with upgraded_engine.connect() as conn:
        assert not (EXPECTED_COLUMNS & await _media_columns(conn))

    await _migration_026_add_media_match_lock(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert EXPECTED_COLUMNS <= await _media_columns(conn)

    # Re-running after the columns have been added must not raise either.
    await _migration_026_add_media_match_lock(upgraded_engine)
    async with upgraded_engine.connect() as conn:
        assert EXPECTED_COLUMNS <= await _media_columns(conn)


async def test_column_exists_helper_detects_match_locked(fresh_engine, upgraded_engine):
    async with fresh_engine.connect() as conn:
        assert await _column_exists(conn, "media", "match_locked") is True
    async with upgraded_engine.connect() as conn:
        assert await _column_exists(conn, "media", "match_locked") is False


async def test_run_migrations_full_chain_backfills_lock_columns_on_upgraded_db(upgraded_engine):
    """The full run_migrations() chain (001->026) must add the lock columns
    to an upgraded DB that predates migration 026, without raising."""
    await run_migrations(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert EXPECTED_COLUMNS <= await _media_columns(conn)


async def test_run_migrations_on_fresh_create_all_db_is_noop_for_lock_columns(fresh_engine):
    """CR-C05 invariant: create_all() THEN run_migrations() on a brand-new DB
    must not raise (migration 026 sees the columns already present and
    no-ops), and the full chain is rejouable."""
    await run_migrations(fresh_engine)
    await run_migrations(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert EXPECTED_COLUMNS <= await _media_columns(conn)


async def test_match_locked_leaves_the_table_writable_by_raw_sql(upgraded_engine):
    """`match_locked` is NOT NULL, so it must carry a SQL-level DEFAULT
    (migration-025 lesson, CLAUDE.md piège 6): a raw INSERT enumerating
    columns but omitting `match_locked` must not break."""
    await _migration_026_add_media_match_lock(upgraded_engine)

    async with upgraded_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO media "
            "(rating_key, server_id, filter, sort_order, library_section_id, "
            " title, title_sortable, type, page_offset, view_offset, "
            " view_count, last_viewed_at, media_parts, unification_id, "
            " history_group_key, added_at, updated_at, display_rating, "
            " stream_error_count, is_broken, is_in_allowed_categories, is_adult) "
            "VALUES ('vod_1.mp4', 'xtream_a', 'all', 'default', 'xtream_vod', "
            " 'No Lock Given', 'no lock given', 'movie', 0, 0, "
            " 0, 0, '[]', '', "
            " '', 0, 0, 0.0, "
            " 0, 0, 1, 0)"
        ))
        locked = (await conn.execute(text(
            "SELECT match_locked FROM media WHERE rating_key='vod_1.mp4'"
        ))).scalar()
    assert locked == 0


async def test_match_locked_write_and_read_roundtrip(fresh_engine):
    """`match_locked`/`match_source` must be writable/readable and default to
    the neutral (unlocked) state for existing rows."""
    table = Base.metadata.tables["media"]

    async with fresh_engine.begin() as conn:
        await conn.execute(table.insert(), {
            "rating_key": "vod_1.mp4", "server_id": "xtream_a", "filter": "all",
            "sort_order": "default", "library_section_id": "xtream_vod",
            "title": "Locked", "type": "movie", "page_offset": 0,
            "match_locked": True, "match_source": "manual",
        })
        await conn.execute(table.insert(), {
            "rating_key": "vod_2.mp4", "server_id": "xtream_a", "filter": "all",
            "sort_order": "default", "library_section_id": "xtream_vod",
            "title": "Unlocked", "type": "movie", "page_offset": 1,
        })

    async with fresh_engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT match_locked, match_source FROM media WHERE rating_key='vod_1.mp4'")
        )).fetchone()
        assert row == (1, "manual")
        row_default = (await conn.execute(
            text("SELECT match_locked, match_source FROM media WHERE rating_key='vod_2.mp4'")
        )).fetchone()
        assert row_default == (0, None)
