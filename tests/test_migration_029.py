"""Guard test for migration 029: the `scrape_cache` table (ADR 0009 D2.1,
scrape gateway cache).

Covers a fresh DB (create_all already made it -> the migration is a no-op),
idempotency, an upgraded DB that predates the table, DDL convergence between
`create_all` (ORM `ScrapeCacheEntry`) and the migration, the composite PK,
and the full chain on a fresh DB without any WARNING.
"""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import _migration_029_scrape_cache, run_migrations
from app.models.database import Base

EXPECTED_COLUMNS = {
    "family", "key_version", "language", "cache_key", "result",
    "schema_version", "payload", "fetched_at", "expires_at",
}
INDEX_NAME = "ix_scrape_cache_expires_at"


async def _table_info(conn) -> list[tuple]:
    rows = (await conn.execute(text('PRAGMA table_info("scrape_cache")'))).fetchall()
    # (cid, name, type, notnull, dflt_value, pk)
    return [tuple(r) for r in rows]


async def _index_list(conn) -> list[tuple]:
    rows = (await conn.execute(text('PRAGMA index_list("scrape_cache")'))).fetchall()
    # (seq, name, unique, origin, partial) -- seq is positional, compare by name
    return sorted((r[1], r[2], r[3], r[4]) for r in rows)


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'f.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def legacy_engine(tmp_path):
    """A DB that predates 029 entirely: no `scrape_cache` at all."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}", future=True)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE placeholder (id INTEGER PRIMARY KEY)"))
    yield engine
    await engine.dispose()


async def test_migration_029_noop_on_fresh_db_and_idempotent(fresh_engine):
    async with fresh_engine.begin() as conn:
        before = (await _table_info(conn), await _index_list(conn))
    await _migration_029_scrape_cache(fresh_engine)
    await _migration_029_scrape_cache(fresh_engine)
    async with fresh_engine.begin() as conn:
        after = (await _table_info(conn), await _index_list(conn))
    assert before == after
    assert EXPECTED_COLUMNS == {c[1] for c in after[0]}


async def test_migration_029_creates_the_table_on_an_upgraded_db(legacy_engine):
    async with legacy_engine.begin() as conn:
        assert not await _table_info(conn)  # table absent

    await _migration_029_scrape_cache(legacy_engine)
    await _migration_029_scrape_cache(legacy_engine)  # rejouable

    async with legacy_engine.begin() as conn:
        assert EXPECTED_COLUMNS == {c[1] for c in await _table_info(conn)}
        assert INDEX_NAME in {i[0] for i in await _index_list(conn)}


async def test_migration_029_ddl_identical_to_create_all(fresh_engine, legacy_engine):
    """Fresh (ORM create_all) and legacy (migration only) must expose the
    same table_info (types, NOT NULL, defaults, PK order) and index_list."""
    await _migration_029_scrape_cache(legacy_engine)
    async with fresh_engine.begin() as conn:
        fresh_info, fresh_idx = await _table_info(conn), await _index_list(conn)
    async with legacy_engine.begin() as conn:
        legacy_info, legacy_idx = await _table_info(conn), await _index_list(conn)

    assert fresh_info == legacy_info
    # No column carries a SQL DEFAULT (ADR 0009 D2.1).
    assert all(c[4] is None for c in legacy_info)
    assert [i for i in fresh_idx if i[0] == INDEX_NAME] == [
        i for i in legacy_idx if i[0] == INDEX_NAME
    ]
    assert {i[0] for i in fresh_idx} == {i[0] for i in legacy_idx}


async def test_migration_029_pk_is_the_four_column_composite(legacy_engine):
    await _migration_029_scrape_cache(legacy_engine)
    async with legacy_engine.begin() as conn:
        info = await _table_info(conn)
    pk = [c[1] for c in sorted((c for c in info if c[5]), key=lambda c: c[5])]
    assert pk == ["family", "key_version", "language", "cache_key"]


async def test_migration_029_raw_insert_and_pk_conflict(legacy_engine):
    await _migration_029_scrape_cache(legacy_engine)
    insert = text(
        "INSERT INTO scrape_cache "
        "(family, key_version, language, cache_key, result, schema_version, "
        "payload, fetched_at, expires_at) "
        "VALUES ('movie', 1, 'fr-FR', '603', 'positive', 1, '{}', 10, 20)"
    )
    async with legacy_engine.begin() as conn:
        await conn.execute(insert)
        # same family/language/key, other key_version -> distinct row
        await conn.execute(text(
            "INSERT INTO scrape_cache "
            "(family, key_version, language, cache_key, result, schema_version, "
            "payload, fetched_at, expires_at) "
            "VALUES ('movie', 2, 'fr-FR', '603', 'positive', 1, NULL, 10, 20)"
        ))
    with pytest.raises(IntegrityError):
        async with legacy_engine.begin() as conn:
            await conn.execute(insert)


async def test_run_migrations_full_chain_on_fresh_db_no_warning(tmp_path, caplog):
    """CR-C05: create_all THEN the whole chain must not raise nor warn, twice."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chain.db'}", future=True)
    register_sqlite_vec_listener(engine)
    mig_logger = logging.getLogger("plexhub.db.migrations")
    mig_logger.addHandler(caplog.handler)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        with caplog.at_level(logging.WARNING, logger="plexhub.db.migrations"):
            await run_migrations(engine)
            await run_migrations(engine)
        async with engine.begin() as conn:
            assert EXPECTED_COLUMNS == {c[1] for c in await _table_info(conn)}
    finally:
        mig_logger.removeHandler(caplog.handler)
        await engine.dispose()
    # Migration 016 warns when no encryption key is configured (env-dependent,
    # unrelated): only schema-noise warnings matter here.
    warnings = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("duplicate column" in r.getMessage().lower() or "Migration 029" in r.getMessage())
    ]
    assert not warnings, warnings
