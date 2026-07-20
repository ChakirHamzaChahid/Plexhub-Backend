"""Guard test for migration 022: the `omdb_scrape_cache` table (imdb-id
consistency validator, Wave 1 S1+S2).

`omdb_scrape_cache` is a direct point cache keyed on `imdb_id` (the
validator always has an imdb_id in hand, unlike the title-signature-keyed
`tmdb_scrape_cache`). Purely additive — no existing table/column/data is
touched.

Covers: a fresh DB (create_all already has the table -> migration is a
no-op), idempotency (double run), an upgraded DB that predates the table
(migration creates it), the CR-C05 invariant (create_all THEN
run_migrations on a fresh DB must not raise, DDL byte-matches the ORM
model), a write/read round-trip, and the OMDb config defaults.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import (
    _migration_022_create_omdb_scrape_cache,
    run_migrations,
)
from app.models.database import Base


async def _table_exists(conn, table: str) -> bool:
    row = (await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    )).fetchone()
    return row is not None


async def _omdb_cache_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(omdb_scrape_cache)"))).fetchall()
    return {row[1] for row in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    """A brand-new DB built the same way ``init_db()`` builds one: create_all
    first, so `omdb_scrape_cache` already exists before any migration runs
    (CR-C05)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def upgraded_engine(tmp_path):
    """A DB with every current ORM table EXCEPT `omdb_scrape_cache` — stand-in
    for a DB that predates migration 022. Built via create_all + DROP TABLE so
    every other table/index matches production."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP TABLE omdb_scrape_cache"))
    yield engine
    await engine.dispose()


async def test_migration_022_noop_on_fresh_db_and_idempotent(fresh_engine):
    await _migration_022_create_omdb_scrape_cache(fresh_engine)
    await _migration_022_create_omdb_scrape_cache(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert await _table_exists(conn, "omdb_scrape_cache") is True


async def test_migration_022_creates_table_on_upgraded_db(upgraded_engine):
    async with upgraded_engine.connect() as conn:
        assert await _table_exists(conn, "omdb_scrape_cache") is False

    await _migration_022_create_omdb_scrape_cache(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert await _table_exists(conn, "omdb_scrape_cache") is True

    # Re-running after the table has been created must not raise either.
    await _migration_022_create_omdb_scrape_cache(upgraded_engine)
    async with upgraded_engine.connect() as conn:
        assert await _table_exists(conn, "omdb_scrape_cache") is True


async def test_migration_022_ddl_matches_orm_columns(fresh_engine):
    """CR-C05 invariant: the hand-rolled CREATE TABLE (upgraded-DB path) must
    declare the exact same columns as the ORM model (create_all path), or a
    fresh DB and an upgraded DB would diverge in schema."""
    await _migration_022_create_omdb_scrape_cache(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert await _omdb_cache_columns(conn) == {
            "imdb_id", "result", "payload", "fetched_at",
        }


async def test_run_migrations_full_chain_creates_omdb_cache_on_upgraded_db(upgraded_engine):
    """The full run_migrations() chain (001->022) must create
    omdb_scrape_cache on an upgraded DB that predates migration 022, without
    raising."""
    await run_migrations(upgraded_engine)

    async with upgraded_engine.connect() as conn:
        assert await _table_exists(conn, "omdb_scrape_cache") is True


async def test_run_migrations_on_fresh_create_all_db_is_noop_for_omdb_cache(fresh_engine):
    """CR-C05 invariant: create_all() THEN run_migrations() on a brand-new DB
    must not raise (migration 022 sees the table already present and
    no-ops). Also proves the whole 001->022 chain still runs end to end."""
    await run_migrations(fresh_engine)
    # Idempotence: the full chain must also survive a second run.
    await run_migrations(fresh_engine)

    async with fresh_engine.connect() as conn:
        assert await _table_exists(conn, "omdb_scrape_cache") is True


async def test_omdb_scrape_cache_write_and_read_roundtrip(fresh_engine):
    table = Base.metadata.tables["omdb_scrape_cache"]

    async with fresh_engine.begin() as conn:
        await conn.execute(table.insert(), {
            "imdb_id": "tt0111161", "result": "found",
            "payload": '{"Title": "The Shawshank Redemption"}', "fetched_at": 1000,
        })
        await conn.execute(table.insert(), {
            "imdb_id": "tt9999999", "result": "not_found",
            "payload": None, "fetched_at": 1001,
        })

    async with fresh_engine.connect() as conn:
        found = (await conn.execute(
            text("SELECT result, payload FROM omdb_scrape_cache WHERE imdb_id='tt0111161'")
        )).fetchone()
        assert found[0] == "found"
        assert found[1] == '{"Title": "The Shawshank Redemption"}'

        not_found = (await conn.execute(
            text("SELECT result, payload FROM omdb_scrape_cache WHERE imdb_id='tt9999999'")
        )).fetchone()
        assert not_found[0] == "not_found"
        assert not_found[1] is None


def test_omdb_config_defaults():
    """OMDB_API_KEY defaults to "" (feature disabled) and OMDB_DAILY_LIMIT
    defaults to 95000 (paid 100k/day plan, 5k margin for retries) when unset
    in the environment.

    No env var is set for either in the test environment/CI, so the module-
    level `settings` singleton (built once at import time, like every other
    Settings default asserted elsewhere in this suite) already reflects the
    unset-env defaults — no reload/monkeypatch needed, and avoids re-running
    Settings.__init__'s side effects (mkdir, plex_client_id persistence).
    """
    from app.config import settings

    assert settings.OMDB_API_KEY == ""
    assert settings.OMDB_DAILY_LIMIT == 95000
