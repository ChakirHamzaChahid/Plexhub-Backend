"""Guard test for migration 027: the `scrape_review` table (ADR 0005 D8,
manual-scraper "À vérifier" queue).

Covers a fresh DB (create_all already made it -> the migration is a no-op),
idempotency (double run), an upgraded DB that predates the table, the
CR-C05 invariant (create_all THEN the whole chain must not raise), and the
migration-025 lesson: `status`/`candidates_json` are NOT NULL, so they must
carry a SQL-level DEFAULT or every raw INSERT enumerating columns breaks.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import _migration_027_create_scrape_review, run_migrations
from app.models.database import Base

EXPECTED_COLUMNS = {
    "rating_key", "server_id", "media_type", "title", "year", "reason",
    "candidates_json", "best_confidence", "best_image_score", "status",
    "created_at", "resolved_at",
}


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text('PRAGMA table_info("scrape_review")'))).fetchall()
    return {r[1] for r in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def legacy_engine(tmp_path):
    """A DB that predates 027 entirely: no `scrape_review` at all."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}", future=True)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE placeholder (id INTEGER PRIMARY KEY)"))
    yield engine
    await engine.dispose()


async def test_migration_027_noop_on_fresh_db_and_idempotent(fresh_engine):
    await _migration_027_create_scrape_review(fresh_engine)
    await _migration_027_create_scrape_review(fresh_engine)
    async with fresh_engine.begin() as conn:
        assert EXPECTED_COLUMNS <= await _columns(conn)


async def test_migration_027_creates_the_table_on_an_upgraded_db(legacy_engine):
    async with legacy_engine.begin() as conn:
        assert not await _columns(conn)  # table absent

    await _migration_027_create_scrape_review(legacy_engine)
    await _migration_027_create_scrape_review(legacy_engine)  # rejouable

    async with legacy_engine.begin() as conn:
        assert EXPECTED_COLUMNS <= await _columns(conn)
        index_names = {
            r[1] for r in (await conn.execute(text(
                'PRAGMA index_list("scrape_review")'
            ))).fetchall()
        }
        assert "ix_scrape_review_status_type" in index_names


async def test_migration_027_defaults_let_raw_inserts_work(legacy_engine):
    """`status`/`candidates_json` are NOT NULL: a raw INSERT that predates
    them (or simply doesn't know about them) must still succeed — the
    migration-025 lesson (CLAUDE.md piège 6)."""
    await _migration_027_create_scrape_review(legacy_engine)

    async with legacy_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO scrape_review "
            "(rating_key, server_id, media_type, reason, created_at) "
            "VALUES ('vod_1.mp4', 'xtream_a', 'movie', 'no_candidates', 42)"
        ))
        row = (await conn.execute(text(
            "SELECT status, candidates_json FROM scrape_review "
            "WHERE rating_key='vod_1.mp4'"
        ))).fetchone()
    assert row == ("pending", "[]")


async def test_migration_027_pk_is_one_review_per_item(legacy_engine):
    await _migration_027_create_scrape_review(legacy_engine)
    async with legacy_engine.begin() as conn:
        pk = {
            r[1] for r in (await conn.execute(text(
                'PRAGMA table_info("scrape_review")'
            ))).fetchall() if r[5]
        }
    assert pk == {"rating_key", "server_id"}


async def test_run_migrations_full_chain_on_fresh_db(tmp_path):
    """CR-C05: create_all THEN the whole 001->027 chain must not raise, twice."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chain.db'}", future=True)
    register_sqlite_vec_listener(engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await run_migrations(engine)
        await run_migrations(engine)
        async with engine.begin() as conn:
            assert EXPECTED_COLUMNS <= await _columns(conn)
    finally:
        await engine.dispose()
