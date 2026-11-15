"""Tests for app/cli.py (CR-T02): generate & recompute-unification CLI commands."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from app import cli
from app.models.database import Base, Media

runner = CliRunner()


def test_cli_generate_requires_account_id_or_all(tmp_path: Path):
    res = runner.invoke(cli.app, ["generate", "--output", str(tmp_path)])
    assert res.exit_code == 1
    assert "provide --account-id or --all" in res.output


def test_cli_generate_single_account_and_report_truncation(tmp_path: Path, monkeypatch):
    import app.db.database as db_mod
    import app.services.plex_generation_service as pgs

    monkeypatch.setattr(db_mod, "init_db", AsyncMock())
    fake_report = SimpleNamespace(
        created=3,
        updated=1,
        deleted=0,
        unchanged=5,
        errors=[f"err-{i}" for i in range(12)],
        duration_seconds=0.42,
    )
    gen_mock = AsyncMock(return_value=fake_report)
    monkeypatch.setattr(pgs, "generate_plex_library", gen_mock)

    res = runner.invoke(
        cli.app,
        ["generate", "--account-id", "acc1", "--output", str(tmp_path), "--dry-run", "--strm-only"],
    )
    assert res.exit_code == 0, res.output
    gen_mock.assert_awaited_once_with(
        account_ids=["acc1"],
        output_dir=tmp_path,
        strm_only=True,
        dry_run=True,
    )
    assert "DRY-RUN" in res.output
    assert "Created:   3" in res.output
    assert "... and 2 more" in res.output


def test_cli_generate_all_accounts(tmp_path: Path, monkeypatch):
    import app.db.database as db_mod
    import app.services.plex_generation_service as pgs

    monkeypatch.setattr(db_mod, "init_db", AsyncMock())
    fake_report = SimpleNamespace(
        created=0, updated=0, deleted=0, unchanged=2, errors=[], duration_seconds=0.1
    )
    gen_mock = AsyncMock(return_value=fake_report)
    monkeypatch.setattr(pgs, "generate_plex_library", gen_mock)

    res = runner.invoke(cli.app, ["generate", "--all", "--output", str(tmp_path)])
    assert res.exit_code == 0, res.output
    gen_mock.assert_awaited_once_with(
        account_ids=None,
        output_dir=tmp_path,
        strm_only=False,
        dry_run=False,
    )
    assert "account: all" in res.output


def test_cli_recompute_unification_dry_run_and_live(tmp_path: Path, monkeypatch):
    import asyncio
    import app.db.database as db_mod

    db_path = tmp_path / "cli_recompute.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _seed():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            db.add_all(
                [
                    Media(
                        rating_key="m1",
                        server_id="s1",
                        filter="all",
                        sort_order="d",
                        page_offset=1,
                        library_section_id="movies",
                        type="movie",
                        title="Inception",
                        year=2010,
                        imdb_id="tt1375666",
                        unification_id="title_stale",
                        match_locked=False,
                    ),
                    Media(
                        rating_key="m2",
                        server_id="s1",
                        filter="all",
                        sort_order="d",
                        page_offset=2,
                        library_section_id="movies",
                        type="movie",
                        title="Locked",
                        year=2020,
                        imdb_id="tt9999999",
                        unification_id="title_locked",
                        match_locked=True,
                    ),
                ]
            )
            await db.commit()
        return factory

    factory = asyncio.run(_seed())
    monkeypatch.setattr(db_mod, "init_db", AsyncMock())
    monkeypatch.setattr(db_mod, "async_session_factory", factory)

    res_dry = runner.invoke(cli.app, ["recompute-unification", "--dry-run"])
    assert res_dry.exit_code == 0, res_dry.output
    assert "Would update: 1" in res_dry.output

    res_live = runner.invoke(cli.app, ["recompute-unification"])
    assert res_live.exit_code == 0, res_live.output
    assert "Updated: 1" in res_live.output

    async def _verify():
        async with factory() as db:
            m1 = (await db.execute(select(Media).where(Media.rating_key == "m1"))).scalar_one()
            m2 = (await db.execute(select(Media).where(Media.rating_key == "m2"))).scalar_one()
            assert m1.unification_id == "imdb://tt1375666"
            assert m2.unification_id == "title_locked"
        await eng.dispose()

    asyncio.run(_verify())
