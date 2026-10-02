"""Guard test for migration 028: de-duplicate `epg_entries` and create the
UNIQUE `uix_epg_dedup (server_id, stream_id, start_time)` index (FR-BE-554,
Lot 10 sub-lot D).

Migration 006 created `epg_entries` with only plain indexes, and
`Base.metadata.create_all` never adds an index to an existing table -- so a
production DB can hold duplicate programmes and lack the unique index the ORM
model declares. 028 must succeed on exactly that state, be replayable, and be
all-or-nothing.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import (
    _migration_006_create_live_tables,
    _migration_028_epg_dedup_unique_index,
    run_migrations,
)
from app.models.database import Base


async def _index_info(conn) -> dict[str, tuple[bool, list[str]]]:
    """name -> (is_unique, ordered column list) for every epg_entries index."""
    out: dict[str, tuple[bool, list[str]]] = {}
    for row in (await conn.execute(text('PRAGMA index_list("epg_entries")'))).fetchall():
        name, unique = row[1], bool(row[2])
        cols = [
            r[2] for r in (await conn.execute(text(f'PRAGMA index_info("{name}")'))).fetchall()
        ]
        out[name] = (unique, cols)
    return out


async def _insert(conn, server_id, stream_id, start, title, *, fetched_at=0):
    await conn.execute(
        text(
            "INSERT INTO epg_entries "
            "(server_id, epg_channel_id, stream_id, title, start_time, end_time, fetched_at) "
            "VALUES (:s, 'ch', :st, :t, :start, :end, :f)"
        ),
        {"s": server_id, "st": stream_id, "t": title, "start": start,
         "end": start + 1000, "f": fetched_at},
    )


@pytest_asyncio.fixture
async def legacy_engine(tmp_path):
    """A prod-shaped DB: `epg_entries` as migration 006 made it (NO unique
    index), loaded with duplicates."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}", future=True)
    await _migration_006_create_live_tables(engine)
    async with engine.begin() as conn:
        # triplet A: 3 copies (ids 1,2,3) -> id 3 must survive
        await _insert(conn, "xtream_a", 10, 1_000, "A v1", fetched_at=1)
        await _insert(conn, "xtream_a", 10, 1_000, "A v2", fetched_at=2)
        await _insert(conn, "xtream_a", 10, 1_000, "A v3", fetched_at=3)
        # triplet B: unique row
        await _insert(conn, "xtream_a", 10, 2_000, "B only")
        # triplet C: same (stream, start) on ANOTHER server -> not a duplicate
        await _insert(conn, "xtream_b", 10, 1_000, "C other server")
        # triplet D: 2 copies
        await _insert(conn, "xtream_a", 11, 1_000, "D v1")
        await _insert(conn, "xtream_a", 11, 1_000, "D v2")
        # NULL stream_id rows are never duplicates for a UNIQUE index: kept.
        for title in ("null1", "null2"):
            await conn.execute(
                text(
                    "INSERT INTO epg_entries (server_id, epg_channel_id, stream_id, "
                    "title, start_time, end_time) "
                    "VALUES ('xtream_a', 'ch', NULL, :t, 5, 6)"
                ),
                {"t": title},
            )
    yield engine
    await engine.dispose()


async def test_migration_006_alone_leaves_no_unique_index(legacy_engine):
    """Documents the production state 028 exists to repair."""
    async with legacy_engine.begin() as conn:
        info = await _index_info(conn)
    assert "uix_epg_dedup" not in info
    assert not any(unique for unique, _ in info.values())


async def test_migration_028_dedups_keeping_highest_id_and_creates_unique_index(legacy_engine):
    await _migration_028_epg_dedup_unique_index(legacy_engine)

    async with legacy_engine.begin() as conn:
        titles = {
            r[0] for r in (await conn.execute(text("SELECT title FROM epg_entries"))).fetchall()
        }
        info = await _index_info(conn)

    # one survivor per triplet, the highest id (= last inserted)
    assert titles == {"A v3", "B only", "C other server", "D v2", "null1", "null2"}
    assert info["uix_epg_dedup"] == (True, ["server_id", "stream_id", "start_time"])


async def test_migration_028_is_replayable(legacy_engine):
    await _migration_028_epg_dedup_unique_index(legacy_engine)
    async with legacy_engine.begin() as conn:
        before = (await conn.execute(text("SELECT id, title FROM epg_entries ORDER BY id"))).fetchall()

    await _migration_028_epg_dedup_unique_index(legacy_engine)  # no-op
    await _migration_028_epg_dedup_unique_index(legacy_engine)

    async with legacy_engine.begin() as conn:
        after = (await conn.execute(text("SELECT id, title FROM epg_entries ORDER BY id"))).fetchall()
        info = await _index_info(conn)
    assert before == after
    assert info["uix_epg_dedup"][0] is True


async def test_unique_index_really_rejects_a_duplicate_afterwards(legacy_engine):
    await _migration_028_epg_dedup_unique_index(legacy_engine)
    with pytest.raises(IntegrityError):
        async with legacy_engine.begin() as conn:
            await _insert(conn, "xtream_a", 10, 1_000, "dup")


async def test_migration_028_rebuilds_a_same_named_non_unique_index(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nu.db'}", future=True)
    try:
        await _migration_006_create_live_tables(engine)
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE INDEX uix_epg_dedup ON epg_entries(server_id, stream_id, start_time)"
            ))
            await _insert(conn, "xtream_a", 1, 100, "x1")
            await _insert(conn, "xtream_a", 1, 100, "x2")
        await _migration_028_epg_dedup_unique_index(engine)
        async with engine.begin() as conn:
            info = await _index_info(conn)
            n = (await conn.execute(text("SELECT COUNT(*) FROM epg_entries"))).scalar()
        assert info["uix_epg_dedup"][0] is True
        assert n == 1
    finally:
        await engine.dispose()


async def test_migration_028_noop_when_table_absent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'none.db'}", future=True)
    try:
        await _migration_028_epg_dedup_unique_index(engine)  # must not raise
    finally:
        await engine.dispose()


async def test_migration_028_failure_is_loud_and_atomic(tmp_path):
    """A table the DELETE cannot run against (no `id` column) must raise, and
    leave NO partial schema change behind (no index created)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bad.db'}", future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE epg_entries (server_id TEXT, stream_id INTEGER, start_time INTEGER)"
            ))
        with pytest.raises(OperationalError):
            await _migration_028_epg_dedup_unique_index(engine)
        async with engine.begin() as conn:
            assert "uix_epg_dedup" not in await _index_info(conn)
    finally:
        await engine.dispose()


async def test_fresh_db_has_the_unique_index_from_create_all_and_028_is_a_noop(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.begin() as conn:
            assert (await _index_info(conn))["uix_epg_dedup"][0] is True
        await _migration_028_epg_dedup_unique_index(engine)
        await _migration_028_epg_dedup_unique_index(engine)
    finally:
        await engine.dispose()


async def test_run_migrations_full_chain_repairs_a_legacy_db_and_is_replayable(tmp_path):
    """CR-C05: the whole 001->028 chain, on a DB whose epg table is
    prod-shaped (duplicates, no unique index), twice."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chain.db'}", future=True)
    register_sqlite_vec_listener(engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Reproduce the 006-era table: drop the model's unique index.
            await conn.execute(text("DROP INDEX uix_epg_dedup"))
            await _insert(conn, "xtream_a", 10, 1_000, "old")
            await _insert(conn, "xtream_a", 10, 1_000, "new")
        await run_migrations(engine)
        await run_migrations(engine)
        async with engine.begin() as conn:
            titles = [
                r[0] for r in (await conn.execute(text("SELECT title FROM epg_entries"))).fetchall()
            ]
            info = await _index_info(conn)
        assert titles == ["new"]
        assert info["uix_epg_dedup"][0] is True
    finally:
        await engine.dispose()


async def test_failure_after_a_successful_delete_rolls_the_delete_back(legacy_engine):
    """The DELETE itself succeeds, then CREATE UNIQUE INDEX fails: the whole
    transaction must be undone -- the duplicates are still there (nothing was
    half-migrated) and the error is raised."""
    from sqlalchemy import event

    async with legacy_engine.begin() as conn:
        before = (await conn.execute(text("SELECT COUNT(*) FROM epg_entries"))).scalar()
    assert before == 9  # 3 + 1 + 1 + 2 + 2 NULL-stream rows from the fixture

    deleted: list[bool] = []

    @event.listens_for(legacy_engine.sync_engine, "before_cursor_execute")
    def _fail_on_create_index(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("DELETE FROM EPG_ENTRIES"):
            deleted.append(True)
        if statement.lstrip().upper().startswith("CREATE UNIQUE INDEX"):
            raise RuntimeError("simulated failure of CREATE UNIQUE INDEX")

    with pytest.raises(Exception, match="simulated failure"):
        await _migration_028_epg_dedup_unique_index(legacy_engine)
    assert deleted, "the DELETE must have been executed before the failure"

    event.remove(legacy_engine.sync_engine, "before_cursor_execute", _fail_on_create_index)
    async with legacy_engine.begin() as conn:
        after = (await conn.execute(text("SELECT COUNT(*) FROM epg_entries"))).scalar()
        info = await _index_info(conn)
    assert after == before, "the successful DELETE must have been rolled back"
    assert "uix_epg_dedup" not in info
