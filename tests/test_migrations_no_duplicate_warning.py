"""Guard tests for CR-C05 (duplicate-column WARNING noise on fresh boot).

On a FRESH database, ``Base.metadata.create_all`` (db/database.py:92) already
creates every ORM-declared column (``is_adult``, the 13 NFO columns,
``cast``, ``category_filter_mode``, ``existing_tmdb_id``/``existing_imdb_id``,
...). Before this fix, migrations 002/003/004/005/010/013/014 then each ran
a plain ``ALTER TABLE ... ADD COLUMN`` for the *same* columns, which always
raised "duplicate column name" on that fresh DB — caught by the per-column
try/except but logged as a WARNING on every cold start, masking real
migration failures behind expected noise.

These tests assert the fix: probing column existence first (``PRAGMA
table_info``) makes the fresh-DB case a silent no-op, while an upgraded DB
(column genuinely missing) still gets the column added — verified by
tests/test_adult_classification.py::TestMigration013 and
tests/test_media_indexes_migration.py already covering the "column really
gets added" half; this file covers the "no noise on fresh boot" half.
"""
from __future__ import annotations

import logging

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import (
    _column_exists,
    _migration_002_add_category_filter_mode,
    _migration_003_add_media_category_visibility,
    _migration_004_add_enrichment_existing_ids,
    _migration_005_add_media_cast,
    _migration_010_scrape_cache,
    _migration_013_add_media_is_adult,
    _migration_014_add_nfo_metadata,
    run_migrations,
)
from app.models.database import Base

MIGRATIONS_LOGGER = "app.db.migrations"


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    """A brand-new DB built the same way ``init_db()`` builds one in
    production: ``Base.metadata.create_all`` first (this is exactly what
    makes every ADD COLUMN migration redundant on a fresh boot), with
    sqlite-vec registered so migration 008 (vec0 virtual table) can run
    inside the full ``run_migrations`` chain.
    """
    db_path = tmp_path / "fresh.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


def _duplicate_column_warnings(records) -> list[str]:
    return [
        r.getMessage()
        for r in records
        if r.levelno >= logging.WARNING and "duplicate column" in r.getMessage().lower()
    ]


async def test_run_migrations_on_fresh_db_logs_no_duplicate_column_warning(fresh_engine, caplog):
    """CR-C05: the full chain on a fresh (create_all-built) DB must not log
    a single 'duplicate column' WARNING."""
    with caplog.at_level(logging.WARNING, logger=MIGRATIONS_LOGGER):
        await run_migrations(fresh_engine)

    dupes = _duplicate_column_warnings(caplog.records)
    assert not dupes, f"unexpected duplicate-column warnings on fresh boot: {dupes}"


async def test_run_migrations_idempotent_rerun_still_no_warning(fresh_engine, caplog):
    """Re-running the full chain a second time (simulating a second worker
    process's init_db(), cf. CLAUDE.md piège 7) must not raise and must
    still be silent."""
    await run_migrations(fresh_engine)

    with caplog.at_level(logging.WARNING, logger=MIGRATIONS_LOGGER):
        await run_migrations(fresh_engine)

    dupes = _duplicate_column_warnings(caplog.records)
    assert not dupes, f"unexpected duplicate-column warnings on re-run: {dupes}"


async def test_column_exists_helper_detects_present_and_missing_columns(fresh_engine):
    async with fresh_engine.connect() as conn:
        assert await _column_exists(conn, "media", "is_adult") is True
        assert await _column_exists(conn, "media", "cast_json") is True
        assert await _column_exists(conn, "media", "not_a_real_column") is False


async def test_formerly_noisy_add_column_migrations_skip_silently_on_fresh_db(fresh_engine, caplog):
    """Each migration CR-C05 called out (002/003/004/005/010/013/014) must
    take the column-exists short-circuit path (no ALTER attempted, no
    WARNING) when run directly against a fresh create_all DB."""
    with caplog.at_level(logging.DEBUG, logger=MIGRATIONS_LOGGER):
        await _migration_002_add_category_filter_mode(fresh_engine)
        await _migration_003_add_media_category_visibility(fresh_engine)
        await _migration_004_add_enrichment_existing_ids(fresh_engine)
        await _migration_005_add_media_cast(fresh_engine)
        await _migration_010_scrape_cache(fresh_engine)
        await _migration_013_add_media_is_adult(fresh_engine)
        await _migration_014_add_nfo_metadata(fresh_engine)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"unexpected warnings: {warnings}"

    # One DEBUG "already present, skipping" per formerly-noisy column:
    # category_filter_mode, is_in_allowed_categories, existing_tmdb_id,
    # existing_imdb_id, cast, existing_summary, is_adult, + 13 NFO columns.
    skip_messages = [
        r.getMessage() for r in caplog.records if "already present, skipping" in r.getMessage()
    ]
    assert len(skip_messages) >= 18, skip_messages
