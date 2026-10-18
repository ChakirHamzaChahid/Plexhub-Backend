"""Guard test for migration 030: `api_keys.scopes` (ADR 0008 D2.1).

`scopes` is `TEXT NOT NULL DEFAULT ''`: the SQL-level DEFAULT matters (migration
025 lesson, CLAUDE.md piege 6) -- a raw INSERT that predates the column must
still succeed, and an existing key becomes "sans scope" ('').
"""
from __future__ import annotations

import logging

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import _migration_030_api_key_scopes, run_migrations
from app.models.database import Base

LEGACY_API_KEYS_DDL = """
    CREATE TABLE api_keys (
        id            TEXT    NOT NULL PRIMARY KEY,
        key_hash      TEXT    NOT NULL UNIQUE,
        key_prefix    TEXT    NOT NULL,
        label         TEXT    NOT NULL,
        created_at    BIGINT  NOT NULL,
        expires_at    BIGINT,
        revoked_at    BIGINT,
        last_used_at  BIGINT,
        last_used_ip  TEXT
    )
"""
RAW_INSERT = (
    "INSERT INTO api_keys (id, key_hash, key_prefix, label, created_at) "
    "VALUES (:id, :h, 'phk_abc', 'tv', 1)"
)


async def _info(conn) -> list[tuple]:
    rows = (await conn.execute(text('PRAGMA table_info("api_keys")'))).fetchall()
    return [tuple(r) for r in rows]


def _scopes_col(info):
    return next(c for c in info if c[1] == "scopes")


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'f.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def legacy_engine(tmp_path):
    """api_keys as it was before 030, with one existing key."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}", future=True)
    async with engine.begin() as conn:
        await conn.execute(text(LEGACY_API_KEYS_DDL))
        await conn.execute(text(RAW_INSERT), {"id": "old", "h": "h-old"})
    yield engine
    await engine.dispose()


async def test_migration_030_noop_on_fresh_db_and_idempotent(fresh_engine, caplog):
    mig = logging.getLogger("plexhub.db.migrations")
    mig.addHandler(caplog.handler)
    try:
        async with fresh_engine.begin() as conn:
            before = await _info(conn)
        with caplog.at_level(logging.WARNING, logger="plexhub.db.migrations"):
            await _migration_030_api_key_scopes(fresh_engine)
            await _migration_030_api_key_scopes(fresh_engine)
        async with fresh_engine.begin() as conn:
            assert await _info(conn) == before
    finally:
        mig.removeHandler(caplog.handler)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_migration_030_adds_column_on_upgraded_db(legacy_engine):
    async with legacy_engine.begin() as conn:
        assert not any(c[1] == "scopes" for c in await _info(conn))

    await _migration_030_api_key_scopes(legacy_engine)
    await _migration_030_api_key_scopes(legacy_engine)  # rejouable

    async with legacy_engine.begin() as conn:
        assert _scopes_col(await _info(conn))
        row = (await conn.execute(text("SELECT scopes FROM api_keys WHERE id='old'"))).fetchone()
    assert row == ("",)


async def test_migration_030_raw_insert_without_column_succeeds(legacy_engine):
    await _migration_030_api_key_scopes(legacy_engine)
    async with legacy_engine.begin() as conn:
        await conn.execute(text(RAW_INSERT), {"id": "new", "h": "h-new"})
        row = (await conn.execute(text("SELECT scopes FROM api_keys WHERE id='new'"))).fetchone()
    assert row == ("",)


async def test_migration_030_raw_insert_succeeds_on_fresh_db(fresh_engine):
    """The ORM server_default makes create_all emit DEFAULT '' too."""
    async with fresh_engine.begin() as conn:
        await conn.execute(text(RAW_INSERT), {"id": "new", "h": "h-new"})
        row = (await conn.execute(text("SELECT scopes FROM api_keys WHERE id='new'"))).fetchone()
    assert row == ("",)


async def test_migration_030_table_info_identical_fresh_vs_legacy(fresh_engine, legacy_engine):
    await _migration_030_api_key_scopes(legacy_engine)
    async with fresh_engine.begin() as conn:
        fresh = _scopes_col(await _info(conn))
    async with legacy_engine.begin() as conn:
        legacy = _scopes_col(await _info(conn))
    # (cid, name, type, notnull, dflt_value, pk) -- cid differs by position only
    assert fresh[1:] == legacy[1:]
    assert fresh[2] == "TEXT" and fresh[3] == 1 and fresh[4] == "''"


async def test_run_migrations_full_chain_on_fresh_db_no_warning(tmp_path, caplog):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chain.db'}", future=True)
    register_sqlite_vec_listener(engine)
    mig = logging.getLogger("plexhub.db.migrations")
    mig.addHandler(caplog.handler)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        with caplog.at_level(logging.WARNING, logger="plexhub.db.migrations"):
            await run_migrations(engine)
            await run_migrations(engine)
        async with engine.begin() as conn:
            assert _scopes_col(await _info(conn))
    finally:
        mig.removeHandler(caplog.handler)
        await engine.dispose()
    noisy = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("duplicate column" in r.getMessage().lower() or "Migration 030" in r.getMessage())
    ]
    assert not noisy, noisy


async def test_migration_030_does_not_swallow_a_real_failure(tmp_path):
    """Revue perf B1-01 : seule l'erreur « duplicate column » est tolérée.

    Toute autre erreur (verrou au-delà du busy_timeout, disque, table absente)
    doit faire échouer le démarrage : une colonne `scopes` absente casserait en
    silence toute lecture ORM de `ApiKey` (chaque clé utilisateur en 401).
    Ici, `api_keys` n'existe pas : l'ALTER lève « no such table ».
    """
    import pytest
    from sqlalchemy.exc import OperationalError

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}", future=True)
    try:
        with pytest.raises(OperationalError):
            await _migration_030_api_key_scopes(engine)
    finally:
        await engine.dispose()
