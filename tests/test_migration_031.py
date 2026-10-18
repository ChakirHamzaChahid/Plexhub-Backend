"""Guard test for migration 031: the `metadata_edits` table (ADR 0011 § D2.5,
lot B2 — journal d'édition de l'API `/api/metadata/v1`).

Covers a fresh DB (create_all already made it -> the migration is a no-op),
idempotency, an upgraded DB that predates the table, DDL convergence between
`create_all` (ORM `MetadataEdit`) and the migration, the unique
`(key_id, idempotency_key)` index, a raw INSERT, and the full chain on a fresh
DB without any WARNING.
"""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import _migration_031_metadata_edits, run_migrations
from app.models.database import Base

EXPECTED_COLUMNS = {
    "edit_id", "kind", "at_ms", "server_id", "rating_key", "item_type", "key_id",
    "request_id", "idempotency_key", "body_sha256", "reason", "revision_before",
    "revision_after", "before_json", "after_json", "response_json", "reverts",
    "reverted_by",
}
ITEM_INDEX = "ix_metadata_edits_item"
IDEM_INDEX = "ux_metadata_edits_idempotency"


async def _table_info(conn) -> list[tuple]:
    rows = (await conn.execute(text('PRAGMA table_info("metadata_edits")'))).fetchall()
    return [tuple(r) for r in rows]


async def _index_list(conn) -> list[tuple]:
    rows = (await conn.execute(text('PRAGMA index_list("metadata_edits")'))).fetchall()
    return sorted((r[1], r[2], r[3], r[4]) for r in rows)


async def _index_columns(conn, name: str) -> list[str]:
    rows = (await conn.execute(text(f'PRAGMA index_info("{name}")'))).fetchall()
    return [r[2] for r in sorted(rows, key=lambda r: r[0])]


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'f.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def legacy_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}", future=True)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE placeholder (id INTEGER PRIMARY KEY)"))
    yield engine
    await engine.dispose()


async def test_migration_031_noop_on_fresh_db_and_idempotent(fresh_engine):
    async with fresh_engine.begin() as conn:
        before = (await _table_info(conn), await _index_list(conn))
    await _migration_031_metadata_edits(fresh_engine)
    await _migration_031_metadata_edits(fresh_engine)
    async with fresh_engine.begin() as conn:
        after = (await _table_info(conn), await _index_list(conn))
    assert before == after
    assert EXPECTED_COLUMNS == {c[1] for c in after[0]}


async def test_migration_031_creates_table_and_indexes_on_an_upgraded_db(legacy_engine):
    await _migration_031_metadata_edits(legacy_engine)
    await _migration_031_metadata_edits(legacy_engine)  # rejouable
    async with legacy_engine.begin() as conn:
        assert EXPECTED_COLUMNS == {c[1] for c in await _table_info(conn)}
        names = {i[0] for i in await _index_list(conn)}
        assert {ITEM_INDEX, IDEM_INDEX} <= names
        assert await _index_columns(conn, ITEM_INDEX) == ["server_id", "rating_key", "at_ms"]
        assert await _index_columns(conn, IDEM_INDEX) == ["key_id", "idempotency_key"]


async def test_migration_031_ddl_identical_to_create_all(fresh_engine, legacy_engine):
    await _migration_031_metadata_edits(legacy_engine)
    async with fresh_engine.begin() as conn:
        fresh_info, fresh_idx = await _table_info(conn), await _index_list(conn)
    async with legacy_engine.begin() as conn:
        legacy_info, legacy_idx = await _table_info(conn), await _index_list(conn)
    assert fresh_info == legacy_info
    assert {i[0] for i in fresh_idx if not i[0].startswith("sqlite_")} == {
        i[0] for i in legacy_idx if not i[0].startswith("sqlite_")
    }
    unique = {i[0]: i[1] for i in legacy_idx}
    assert unique[IDEM_INDEX] == 1 and unique[ITEM_INDEX] == 0


async def test_migration_031_json_columns_carry_a_sql_default(legacy_engine):
    """Leçon des migrations 025/027 : un NOT NULL sans DEFAULT SQL casse les
    INSERT bruts qui n'énumèrent pas toutes les colonnes."""
    await _migration_031_metadata_edits(legacy_engine)
    async with legacy_engine.begin() as conn:
        info = {c[1]: c for c in await _table_info(conn)}
    for col in ("before_json", "after_json", "response_json"):
        assert info[col][3] == 1  # NOT NULL
        assert info[col][4] == "'{}'"  # SQL DEFAULT


_INSERT = (
    "INSERT INTO metadata_edits (edit_id, kind, at_ms, server_id, rating_key, item_type, "
    "key_id, idempotency_key, revision_before, revision_after) "
    "VALUES (:eid, 'patch', 1, 'xtream_a', 'vod_1.mkv', 'movie', 'k1', :idem, 'r0', 'r1')"
)


async def test_migration_031_raw_insert_without_json_columns(legacy_engine):
    await _migration_031_metadata_edits(legacy_engine)
    async with legacy_engine.begin() as conn:
        await conn.execute(text(_INSERT), {"eid": "E1", "idem": "i1"})
        row = (await conn.execute(text("SELECT before_json, response_json FROM metadata_edits"))).one()
    assert tuple(row) == ("{}", "{}")


async def test_idempotency_key_is_unique_per_key_id_but_null_is_free(legacy_engine):
    await _migration_031_metadata_edits(legacy_engine)
    async with legacy_engine.begin() as conn:
        await conn.execute(text(_INSERT), {"eid": "E1", "idem": "i1"})
        # same idempotency key under ANOTHER key id is a distinct edit
        await conn.execute(
            text(_INSERT.replace("'k1'", "'k2'")), {"eid": "E2", "idem": "i1"}
        )
        # several edits without an idempotency key (lock/revert via master, etc.)
        await conn.execute(text(_INSERT), {"eid": "E3", "idem": None})
        await conn.execute(text(_INSERT), {"eid": "E4", "idem": None})
    with pytest.raises(IntegrityError):
        async with legacy_engine.begin() as conn:
            await conn.execute(text(_INSERT), {"eid": "E5", "idem": "i1"})


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
    warnings = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("duplicate column" in r.getMessage().lower() or "Migration 031" in r.getMessage())
    ]
    assert not warnings, warnings
