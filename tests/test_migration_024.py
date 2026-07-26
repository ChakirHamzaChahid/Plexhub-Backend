"""Guard test for migration 024: the `media.youtube_trailer` column (feature
"trailers Home overlay pour les médias non-Plex", Lot A).

`youtube_trailer` (TEXT, a bare YouTube video id, nullable) is purely
additive and never backfilled by the migration itself — it stays NULL until
the next Xtream sync captures it (`sync_worker.map_vod_to_media` /
`fetch_series_episodes`) or the TMDB enrichment fill-missing write resolves
one (`enrichment_worker._apply_enrichment_results`). Consumed by
`GET /api/media/trailer/resolve` (`app/services/trailer_service.py`) to skip
a live TMDB lookup when a value is already on the row.

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
    _migration_024_add_media_youtube_trailer,
    run_migrations,
)
from app.models.database import Base


async def _media_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(media)"))).fetchall()
    return {row[1] for row in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    """A brand-new DB built the same way ``init_db()`` builds one: create_all
    first, so `youtube_trailer` already exists before any migration runs
    (CR-C05)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def upgraded_engine(tmp_path):
    """A `media` table with every current ORM column EXCEPT
    `youtube_trailer` — stand-in for a DB that predates migration 024. Built
    via create_all + DROP COLUMN so every other column/index matches
    production."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE media DROP COLUMN youtube_trailer"))
    yield engine
    await engine.dispose()


async def test_migration_024_noop_on_fresh_db_and_idempotent(fresh_engine):
    await _migration_024_add_media_youtube_trailer(fresh_engine)
    await _migration_024_add_media_youtube_trailer(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert "youtube_trailer" in await _media_columns(conn)


async def test_migration_024_backfills_on_upgraded_db(upgraded_engine):
    async with upgraded_engine.connect() as conn:
        assert "youtube_trailer" not in await _media_columns(conn)

    await _migration_024_add_media_youtube_trailer(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert "youtube_trailer" in await _media_columns(conn)

    # Re-running after the column has been added must not raise either.
    await _migration_024_add_media_youtube_trailer(upgraded_engine)
    async with upgraded_engine.connect() as conn:
        assert "youtube_trailer" in await _media_columns(conn)


async def test_column_exists_helper_detects_youtube_trailer(fresh_engine, upgraded_engine):
    async with fresh_engine.connect() as conn:
        assert await _column_exists(conn, "media", "youtube_trailer") is True
    async with upgraded_engine.connect() as conn:
        assert await _column_exists(conn, "media", "youtube_trailer") is False


async def test_run_migrations_full_chain_backfills_youtube_trailer_on_upgraded_db(upgraded_engine):
    """The full run_migrations() chain (001->024) must add youtube_trailer to
    an upgraded DB that predates migration 024, without raising."""
    await run_migrations(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert "youtube_trailer" in await _media_columns(conn)


async def test_run_migrations_on_fresh_create_all_db_is_noop_for_youtube_trailer(fresh_engine):
    """CR-C05 invariant: create_all() THEN run_migrations() on a brand-new DB
    must not raise (migration 024 sees youtube_trailer already present and
    no-ops)."""
    await run_migrations(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert "youtube_trailer" in await _media_columns(conn)


async def test_youtube_trailer_write_and_read_roundtrip(fresh_engine):
    """`youtube_trailer` must be writable/readable (the Xtream sync + TMDB
    enrichment write here) and nullable when unset (rows synced before
    M024)."""
    table = Base.metadata.tables["media"]

    async with fresh_engine.begin() as conn:
        await conn.execute(table.insert(), {
            "rating_key": "vod_1.mp4", "server_id": "xtream_a", "filter": "all",
            "sort_order": "default", "library_section_id": "xtream_vod",
            "title": "Trailered", "type": "movie", "page_offset": 0,
            "youtube_trailer": "dQw4w9WgXcQ",
        })
        await conn.execute(table.insert(), {
            "rating_key": "vod_2.mp4", "server_id": "xtream_a", "filter": "all",
            "sort_order": "default", "library_section_id": "xtream_vod",
            "title": "No trailer", "type": "movie", "page_offset": 1,
        })

    async with fresh_engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT youtube_trailer FROM media WHERE rating_key='vod_1.mp4'")
        )).fetchone()
        assert row[0] == "dQw4w9WgXcQ"
        row_null = (await conn.execute(
            text("SELECT youtube_trailer FROM media WHERE rating_key='vod_2.mp4'")
        )).fetchone()
        assert row_null[0] is None
