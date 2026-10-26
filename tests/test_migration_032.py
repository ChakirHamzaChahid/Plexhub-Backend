"""Guard test for migration 032: the lot-B4 scrape columns on `media` (ADR 0013).

Fresh DB (create_all already made them -> no-op), idempotency, an upgraded DB that
predates them (rows keep working, new columns NULL), DDL parity between the migration
and the ORM, and the full chain on a fresh DB.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import _migration_032_media_scrape_fields, run_migrations
from app.models.database import Base, Media

NEW_COLUMNS = (
    "poster_url", "fanart_url", "poster_gallery_json", "fanart_gallery_json",
    "available_trailers_json", "directors", "writers", "spoken_languages",
    "metacritic_rating", "metacritic_votes", "rt_rating", "rt_votes",
)


async def _media_table_info(conn) -> dict[str, tuple]:
    rows = (await conn.execute(text("PRAGMA table_info(media)"))).fetchall()
    # type (SQLAlchemy `Float` is created as FLOAT, the migrations say REAL: same affinity), notnull, default, pk
    return {r[1]: (r[2].upper().replace("FLOAT", "REAL"), r[3], r[4], r[5]) for r in rows}


@pytest_asyncio.fixture
async def fresh_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def upgraded_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for name in NEW_COLUMNS:
            await conn.execute(text(f"ALTER TABLE media DROP COLUMN {name}"))
    yield engine
    await engine.dispose()


async def test_noop_on_fresh_db_and_idempotent(fresh_engine):
    await _migration_032_media_scrape_fields(fresh_engine)
    await _migration_032_media_scrape_fields(fresh_engine)
    async with fresh_engine.connect() as conn:
        assert set(NEW_COLUMNS) <= set(await _media_table_info(conn))


async def test_upgraded_db_gets_the_columns_with_null_and_existing_rows_survive(upgraded_engine):
    async with upgraded_engine.begin() as conn:
        # Core insert: Python-side defaults fill the NOT NULL columns, the dropped ones are omitted.
        await conn.execute(Media.__table__.insert().values(
            rating_key="k", server_id="s", filter="all", sort_order="default", library_section_id="l",
            title="T", title_sortable="t", page_offset=1, type="movie",
        ))
    await _migration_032_media_scrape_fields(upgraded_engine)
    await _migration_032_media_scrape_fields(upgraded_engine)       # re-run is a no-op
    async with upgraded_engine.connect() as conn:
        info = await _media_table_info(conn)
        assert set(NEW_COLUMNS) <= set(info)
        row = (await conn.execute(text(f"SELECT {', '.join(NEW_COLUMNS)} FROM media"))).one()
        assert all(v is None for v in row)


async def test_ddl_is_identical_to_create_all(fresh_engine, upgraded_engine):
    await _migration_032_media_scrape_fields(upgraded_engine)
    async with fresh_engine.connect() as a, upgraded_engine.connect() as b:
        fresh, migrated = await _media_table_info(a), await _media_table_info(b)
    for name in NEW_COLUMNS:
        assert fresh[name] == migrated[name], name
        assert fresh[name][1] == 0 and fresh[name][2] is None      # nullable, no DEFAULT


async def test_full_chain_after_create_all_does_not_raise(fresh_engine):
    await run_migrations(fresh_engine)
