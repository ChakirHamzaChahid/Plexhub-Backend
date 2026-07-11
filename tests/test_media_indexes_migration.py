"""Guard test for CR-P02: migration 015 backfills the media indexes that
Base.metadata.create_all builds for free on a FRESH database but that the
hand-rolled migration chain never issued CREATE INDEX for on an upgraded one.

Fixture simulates a long-lived DB: the `media` table exists with every
current ORM column, but none of the composite/simple indexes below (only
the implicit PK index SQLite creates automatically).
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable

from app.db.migrations import _migration_015_add_missing_media_indexes
from app.models.database import Base

# Every index CR-P02 requires migration 015 to backfill, matching the ORM's
# Media.__table_args__ (app/models/database.py) name-for-name. Deliberately
# excludes ix_media_category_visible/ix_media_adult/ix_media_tvdb — those are
# already created by migrations 003/013/014 respectively and out of scope here.
EXPECTED_INDEXES = {
    "uix_media_pagination",
    "ix_media_guid",
    "ix_media_type_added",
    "ix_media_imdb",
    "ix_media_tmdb",
    "ix_media_server_lib",
    "ix_media_unification",
    "ix_media_type_rating",
    "ix_media_parent",
    "ix_media_title_sort",
    "ix_media_broken",
    "ix_media_updated",
    "ix_media_server_type",
    "ix_media_server_visible",
    "ix_media_parent_visible",
    "ix_media_grandparent",
}


async def _media_index_names(conn) -> set[str]:
    rows = (
        await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='media'")
        )
    ).fetchall()
    return {row[0] for row in rows}


@pytest_asyncio.fixture
async def upgraded_media_engine(tmp_path):
    """`media` table with all current ORM columns, but none of its indexes —
    stand-in for a database that predates the composite Index() declarations.
    """
    db_path = tmp_path / "upgraded_media.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    media_table = Base.metadata.tables["media"]

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: sync_conn.execute(CreateTable(media_table)))

    yield engine
    await engine.dispose()


async def test_migration_015_creates_all_missing_indexes(upgraded_media_engine):
    async with upgraded_media_engine.begin() as conn:
        before = await _media_index_names(conn)
    assert not (EXPECTED_INDEXES & before), "fixture must start without the target indexes"

    await _migration_015_add_missing_media_indexes(upgraded_media_engine)

    async with upgraded_media_engine.begin() as conn:
        after = await _media_index_names(conn)
    missing = EXPECTED_INDEXES - after
    assert not missing, f"migration 015 did not create: {missing}"


async def test_migration_015_idempotent(upgraded_media_engine):
    """Re-running migration 015 on a DB where it already ran must not raise,
    and the full index set must still be present afterwards."""
    await _migration_015_add_missing_media_indexes(upgraded_media_engine)
    await _migration_015_add_missing_media_indexes(upgraded_media_engine)

    async with upgraded_media_engine.begin() as conn:
        after = await _media_index_names(conn)
    assert EXPECTED_INDEXES <= after


async def test_migration_015_index_columns_match_orm(upgraded_media_engine):
    """Spot-check that a composite index and the unique index carry the
    exact column tuple/order declared in Media.__table_args__."""
    await _migration_015_add_missing_media_indexes(upgraded_media_engine)

    async with upgraded_media_engine.begin() as conn:
        # ix_media_type_rating -> (type, display_rating)
        info = (await conn.execute(text("PRAGMA index_info(ix_media_type_rating)"))).fetchall()
        cols = [row[2] for row in sorted(info, key=lambda r: r[0])]
        assert cols == ["type", "display_rating"]

        # uix_media_pagination -> unique, (server_id, library_section_id, filter, sort_order, page_offset)
        idx_list = (await conn.execute(text("PRAGMA index_list(media)"))).fetchall()
        pagination_row = next(row for row in idx_list if row[1] == "uix_media_pagination")
        assert pagination_row[2] == 1  # `unique` flag

        info = (await conn.execute(text("PRAGMA index_info(uix_media_pagination)"))).fetchall()
        cols = [row[2] for row in sorted(info, key=lambda r: r[0])]
        assert cols == ["server_id", "library_section_id", "filter", "sort_order", "page_offset"]


async def test_migration_015_survives_duplicate_pagination_rows(tmp_path):
    """If an upgraded DB already has rows violating the pagination unique
    constraint, migration 015 must not raise — it should create every other
    index and only skip (with a warning) the unique one it can't add."""
    db_path = tmp_path / "dup_pagination.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    media_table = Base.metadata.tables["media"]

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: sync_conn.execute(CreateTable(media_table)))

        # Two rows sharing (server_id, library_section_id, filter, sort_order,
        # page_offset) — only rating_key differs, so the composite PK allows
        # it, but it violates the would-be unique pagination index. Insert via
        # Core (not raw text SQL) so the ORM's client-side column defaults
        # (NOT NULL columns like page_offset/media_parts/etc.) are applied.
        def _insert_rows(sync_conn):
            sync_conn.execute(
                media_table.insert(),
                [
                    {
                        "rating_key": "rk1",
                        "server_id": "s1",
                        "filter": "all",
                        "sort_order": "default",
                        "library_section_id": "lib1",
                        "title": "Title",
                        "type": "movie",
                    },
                    {
                        "rating_key": "rk2",
                        "server_id": "s1",
                        "filter": "all",
                        "sort_order": "default",
                        "library_section_id": "lib1",
                        "title": "Title",
                        "type": "movie",
                    },
                ],
            )

        await conn.run_sync(_insert_rows)

    # Must not raise despite the duplicate pagination slot.
    await _migration_015_add_missing_media_indexes(engine)

    async with engine.begin() as conn:
        after = await _media_index_names(conn)

    assert "uix_media_pagination" not in after
    assert (EXPECTED_INDEXES - {"uix_media_pagination"}) <= after

    await engine.dispose()
