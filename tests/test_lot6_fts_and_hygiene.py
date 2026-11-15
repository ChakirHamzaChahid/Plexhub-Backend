"""Targeted tests for Lot 6 (CR-P03 FTS5 trigram, CR-A05 startup_tasks, CR-C02/T01)."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.migrations import _migration_033_media_fts5
from app.main import (
    APP_VERSION,
    _auto_generate_plex_library,
    _auto_provision_xtream_account,
    _cleanup_stale_epg,
    _rebuild_unified_groups,
)
from app.models.database import Base, Media
from app.services import startup_tasks
from app.services.media_service import media_service


@pytest.mark.asyncio
async def test_migration_033_fts5_backfill_triggers_and_search(tmp_path: Path):
    db_path = tmp_path / "fts5.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    # Seed one movie BEFORE migration 033 to test backfill
    async with factory() as db:
        db.add(
            Media(
                rating_key="vod_1",
                server_id="xtream_1",
                filter="all",
                sort_order=0,
                library_section_id="movies",
                type="movie",
                title="Blade Runner 2049",
                genres="Sci-Fi, Thriller",
                is_in_allowed_categories=True,
            )
        )
        await db.commit()

    await _migration_033_media_fts5(eng)
    # Idempotency check
    await _migration_033_media_fts5(eng)

    async with factory() as db:
        # Insert via ORM after triggers exist (including quotes/hyphens in title)
        db.add(
            Media(
                rating_key="vod_2",
                server_id="xtream_1",
                filter="all",
                sort_order=1,
                library_section_id="movies",
                type="movie",
                title='Spider-Man: "No Way" Home',
                genres="Action, Adventure",
                is_in_allowed_categories=True,
            )
        )
        await db.commit()

        # >= 3 chars uses FTS5 trigram
        items, total = await media_service.get_media_list(
            db, media_type="movie", search="Runner"
        )
        assert total == 1
        assert items[0].rating_key == "vod_1"

        # Special FTS5 characters ('"' / '-') are safely phrase-escaped
        items_q, total_q = await media_service.get_media_list(
            db, media_type="movie", search='"No Way"'
        )
        assert total_q == 1
        assert items_q[0].rating_key == "vod_2"

        # Genre trigram search + unified path
        groups, total_u = await media_service.get_unified_list(
            db, media_type="movie", genre="Thriller"
        )
        assert total_u == 1
        assert groups[0].best.title == "Blade Runner 2049"

        # < 3 chars falls back gracefully to ILIKE
        items_short, total_short = await media_service.get_media_list(
            db, media_type="movie", search="No"
        )
        assert total_short == 1
        assert items_short[0].rating_key == "vod_2"

        # Verify EXPLAIN QUERY PLAN uses FTS5 virtual table index
        plan_rows = (
            await db.execute(
                text(
                    "EXPLAIN QUERY PLAN "
                    "SELECT rating_key, server_id FROM media_fts "
                    "WHERE title MATCH '\"Runner\"'"
                )
            )
        ).fetchall()
        plan_text = " ".join(str(r) for r in plan_rows).upper()
        assert "MEDIA_FTS" in plan_text

        # Trigger sync on UPDATE and DELETE
        await db.execute(
            text("UPDATE media SET title = 'Arrival' WHERE rating_key = 'vod_1'")
        )
        await db.commit()
        items_upd, total_upd = await media_service.get_media_list(
            db, media_type="movie", search="Arrival"
        )
        assert total_upd == 1

        await db.execute(text("DELETE FROM media WHERE rating_key = 'vod_1'"))
        await db.commit()
        _, total_del = await media_service.get_media_list(
            db, media_type="movie", search="Arrival"
        )
        assert total_del == 0

    await eng.dispose()


def test_startup_tasks_extracted_and_pyproject_version():
    assert _auto_generate_plex_library is startup_tasks.auto_generate_plex_library
    assert _rebuild_unified_groups is startup_tasks.rebuild_unified_groups
    assert _auto_provision_xtream_account is startup_tasks.auto_provision_xtream_account
    assert _cleanup_stale_epg is startup_tasks.cleanup_stale_epg

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["version"] == APP_VERSION
