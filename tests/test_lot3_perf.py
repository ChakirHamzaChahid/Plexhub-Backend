"""Tests for Lot 3 (P1-A): PK tuple index usage, single-query count, SQLite PRAGMAs, Ollama pool, NFO to_thread."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from sqlalchemy import event, select, text, tuple_
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import (
    _PRAGMA_REGISTERED_ENGINES,
    engine,
    register_sqlite_pragmas_listener,
    worker_engine,
)
from app.models.database import Base, Media, XtreamAccount
from app.services import metadata_service, nfo_import_service, ollama_service
from app.services.media_service import media_service
from app.plex_generator.mapping import MappingEntry, MappingStore


@pytest.mark.asyncio
async def test_media_pk_tuple_uses_index_in_explain_query_plan(tmp_path: Path):
    db_path = tmp_path / "pk_plan.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    stmt = select(Media).where(
        tuple_(Media.rating_key, Media.server_id).in_([("rk_1", "srv_1")])
    )
    compiled = stmt.compile(
        dialect=sqlite_dialect.dialect(),
        compile_kwargs={"literal_binds": True},
    )

    async with eng.connect() as conn:
        plan_rows = (await conn.execute(text(f"EXPLAIN QUERY PLAN {compiled}"))).all()
    await eng.dispose()

    plan_text = " | ".join(str(r[-1]) for r in plan_rows).upper()
    assert "SEARCH" in plan_text
    assert "SCAN MEDIA" not in plan_text


@pytest.mark.asyncio
async def test_metadata_load_items_uses_pk_order_and_loads_items(tmp_path: Path):
    db_path = tmp_path / "load_items.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        db.add(
            Media(
                rating_key="rk_42",
                server_id="xtream_acc1",
                filter="all",
                sort_order="default",
                library_section_id="movies",
                type="movie",
                title="Test Movie",
            )
        )
        await db.commit()

        items = await metadata_service.load_items(db, [("xtream_acc1", "rk_42")])
        assert ("xtream_acc1", "rk_42") in items
        assert items[("xtream_acc1", "rk_42")].variants[0].rating_key == "rk_42"

    await eng.dispose()


@pytest.mark.asyncio
async def test_count_movies_missing_external_executes_single_query(tmp_path: Path):
    db_path = tmp_path / "count_movies.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        # Empty DB returns (0, 0, 0) cleanly
        assert await media_service.count_movies_missing_external(db) == (0, 0, 0)

        db.add_all(
            [
                Media(rating_key="m1", server_id="s1", filter="all", sort_order="d", page_offset=1, library_section_id="movies", type="movie", title="A", imdb_id="tt1", tmdb_id=1),
                Media(rating_key="m2", server_id="s1", filter="all", sort_order="d", page_offset=2, library_section_id="movies", type="movie", title="B", imdb_id=None, tmdb_id=2),
                Media(rating_key="m3", server_id="s1", filter="all", sort_order="d", page_offset=3, library_section_id="movies", type="movie", title="C", imdb_id="", tmdb_id=None),
                Media(rating_key="sh1", server_id="s1", filter="all", sort_order="d", page_offset=4, library_section_id="shows", type="show", title="Show", imdb_id=None, tmdb_id=None),
            ]
        )
        await db.commit()

        queries: list[str] = []

        def _count_sql(_conn, _cursor, statement, _params, _ctx, _executemany):
            queries.append(statement)

        event.listen(eng.sync_engine, "before_cursor_execute", _count_sql)
        try:
            counts = await media_service.count_movies_missing_external(db)
        finally:
            event.remove(eng.sync_engine, "before_cursor_execute", _count_sql)

        assert counts == (3, 2, 1)
        assert len(queries) == 1

    await eng.dispose()


@pytest.mark.asyncio
async def test_sqlite_pragmas_connect_listener_applies_to_connections(tmp_path: Path):
    assert id(engine.sync_engine) in _PRAGMA_REGISTERED_ENGINES
    assert id(worker_engine.sync_engine) in _PRAGMA_REGISTERED_ENGINES

    db_path = tmp_path / "pragmas.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    register_sqlite_pragmas_listener(eng)

    async with eng.connect() as conn:
        sync_val = (await conn.execute(text("PRAGMA synchronous"))).scalar()
        cache_val = (await conn.execute(text("PRAGMA cache_size"))).scalar()
        temp_val = (await conn.execute(text("PRAGMA temp_store"))).scalar()
        mmap_val = (await conn.execute(text("PRAGMA mmap_size"))).scalar()

    await eng.dispose()

    assert sync_val == 1  # NORMAL
    assert cache_val == -64000
    assert temp_val == 2  # MEMORY
    assert mmap_val == 268435456


@pytest.mark.asyncio
async def test_ollama_service_reuses_pooled_client():
    await ollama_service.close_client()
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://khoj-ollama:11434/api/generate").mock(
                return_value=httpx.Response(200, json={"response": "hello"})
            )
            mock.post("http://khoj-ollama:11434/api/chat").mock(
                return_value=httpx.Response(200, json={"message": {"content": "world"}})
            )
            mock.get("http://khoj-ollama:11434/api/tags").mock(
                return_value=httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]})
            )

            c1 = ollama_service._get_client()
            assert await ollama_service.generate("hi") == "hello"
            c2 = ollama_service._get_client()
            assert await ollama_service.chat([{"role": "user", "content": "hi"}]) == "world"
            ok, detail = await ollama_service.is_healthy()
            c3 = ollama_service._get_client()

            assert ok is True
            assert detail == "ok"
            assert c1 is c2 is c3
    finally:
        await ollama_service.close_client()


@pytest.mark.asyncio
async def test_nfo_import_offloads_probe_and_parse_to_thread(tmp_path: Path, monkeypatch):
    root = tmp_path / "plex"
    movie_dir = root / "Films" / "Inception (2010)"
    movie_dir.mkdir(parents=True)
    nfo_file = movie_dir / "movie.nfo"
    nfo_file.write_text(
        "<movie><uniqueid type=\"imdb\">tt1375666</uniqueid></movie>",
        encoding="utf-8",
    )

    mapping = MappingStore(root / ".plex_mapping.json")
    mapping._data["xtream_a1:100"] = MappingEntry(
        path="Films/Inception (2010)/Inception (2010).strm",
        stream_url="http://x.example/movie/u/p/100.mkv",
        updated_at="2026-11-15T00:00:00Z",
    )

    called_funcs: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, /, *args, **kwargs):
        called_funcs.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(nfo_import_service.asyncio, "to_thread", _spy_to_thread)

    db = AsyncMock()
    scalar_res = MagicMock()
    scalar_res.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=scalar_res)

    account = XtreamAccount(id="a1", label="Main", base_url="http://x.example", username="u", password="p")
    report = await nfo_import_service._import_movies_for_account(
        db=db,
        account=account,
        root=root,
        mapping=mapping,
        server_id="xtream_a1",
        overwrite=False,
        dry_run=True,
    )

    assert "_probe_and_parse_nfo" in called_funcs
    assert report.scanned == 1
