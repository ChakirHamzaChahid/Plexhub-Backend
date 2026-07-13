"""Tests for CR-A02: the shared Plex-generation orchestration service.

Before this refactor, "build a DatabaseSource -> PlexLibraryGenerator ->
LocalStorage/DryRunStorage -> generate()" was independently reconstructed in
app/main.py, app/api/plex.py and app/cli.py, and app/api/sync.py imported the
private app.main._auto_generate_plex_library coroutine directly (a router
reaching into the app entrypoint). These tests pin:
  1. app.services.plex_generation_service.generate_plex_library produces the
     same wiring/report as the old inline code (real DB, real generator).
  2. generate_plex_library_auto()'s gating (skip if unconfigured / no active
     accounts) — the behaviour app.main and app.api.sync's /full-pipeline both
     rely on.
  3. app.main._auto_generate_plex_library is now a thin delegator.
  4. app.api.sync no longer imports anything from app.main (layering fix).
  5. app.api.plex's CR-S01 path confinement still runs BEFORE the service is
     ever invoked (no regression from the refactor).
"""
from __future__ import annotations

import inspect
import re

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import Media, XtreamAccount
from app.plex_generator.models import SyncReport
from app.utils.server_id import build_server_id

# pytest-asyncio runs in auto mode (pyproject.toml) — async tests need no mark.

API_KEY = "test-master-key-plex-generation-service"
API_HEADERS = {"X-API-Key": API_KEY}


def _account(id_: str, active: bool = True) -> XtreamAccount:
    return XtreamAccount(
        id=id_, label=f"Account {id_}", base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=active, created_at=0,
    )


def _movie_row(account_id: str, rating_key: str, title: str) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=2020, unification_id="",
        is_in_allowed_categories=True, is_broken=False,
    )


# ─── generate_plex_library (raw wiring) ──────────────────────────────────


class TestGeneratePlexLibraryWiring:
    @pytest_asyncio.fixture
    async def seeded_factory(self, db_engine, monkeypatch):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add_all([
                _account("a"),
                _movie_row("a", "vod_1.mp4", "Test Movie"),
            ])
            await s.commit()

        import app.plex_generator.source as source_mod
        monkeypatch.setattr(source_mod, "async_session_factory", factory)
        return factory

    async def test_writes_expected_strm_and_reports_created(self, seeded_factory, tmp_path):
        from app.services.plex_generation_service import generate_plex_library

        report = await generate_plex_library(output_dir=tmp_path)

        assert isinstance(report, SyncReport)
        assert report.created == 1
        strm = tmp_path / "Films" / "Test Movie (2020)" / "Test Movie (2020).strm"
        assert strm.exists()

    async def test_strm_only_skips_nfo(self, seeded_factory, tmp_path):
        from app.services.plex_generation_service import generate_plex_library

        await generate_plex_library(output_dir=tmp_path, strm_only=True)

        assert not (tmp_path / "Films" / "Test Movie (2020)" / "movie.nfo").exists()

    async def test_dry_run_writes_no_files_but_reports_created(self, seeded_factory, tmp_path):
        from app.services.plex_generation_service import generate_plex_library

        report = await generate_plex_library(output_dir=tmp_path, dry_run=True)

        assert report.created == 1
        # DryRunStorage never touches disk (the generator's MappingStore still
        # persists its small `.plex_mapping.json` bookkeeping file regardless
        # of storage backend, so assert on the actual media artifact instead
        # of "nothing at all was written").
        assert not (tmp_path / "Films").exists()

    async def test_account_ids_restricts_aggregation(self, db_engine, monkeypatch, tmp_path):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add_all([
                _account("a"),
                _account("b"),
                _movie_row("a", "vod_1.mp4", "Movie A"),
                _movie_row("b", "vod_2.mp4", "Movie B"),
            ])
            await s.commit()

        import app.plex_generator.source as source_mod
        monkeypatch.setattr(source_mod, "async_session_factory", factory)

        from app.services.plex_generation_service import generate_plex_library

        report = await generate_plex_library(account_ids=["a"], output_dir=tmp_path)

        assert report.created == 1
        assert (tmp_path / "Films" / "Movie A (2020)").exists()
        assert not (tmp_path / "Films" / "Movie B (2020)").exists()

    async def test_missing_output_dir_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", "")
        from app.services.plex_generation_service import generate_plex_library

        with pytest.raises(ValueError):
            await generate_plex_library()


# ─── generate_plex_library_auto (boot/schedule/full-pipeline gating) ─────


class TestGeneratePlexLibraryAutoGating:
    async def test_skips_when_not_configured(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", "")
        from app.services.plex_generation_service import generate_plex_library_auto

        result = await generate_plex_library_auto()
        assert result is None

    async def test_skips_when_no_active_accounts(self, db_engine, monkeypatch, tmp_path):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add(_account("a", active=False))
            await s.commit()

        import app.db.database as database_mod
        monkeypatch.setattr(database_mod, "async_session_factory", factory)
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", str(tmp_path))

        from app.services.plex_generation_service import generate_plex_library_auto

        result = await generate_plex_library_auto()
        assert result is None
        # No accidental destructive generation attempt against an empty source.
        assert not any(tmp_path.iterdir())

    async def test_runs_generation_when_configured_and_active_accounts_exist(
        self, db_engine, monkeypatch, tmp_path
    ):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add_all([
                _account("a"),
                _movie_row("a", "vod_1.mp4", "Auto Movie"),
            ])
            await s.commit()

        import app.db.database as database_mod
        import app.plex_generator.source as source_mod
        monkeypatch.setattr(database_mod, "async_session_factory", factory)
        monkeypatch.setattr(source_mod, "async_session_factory", factory)
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", str(tmp_path))

        from app.services.plex_generation_service import generate_plex_library_auto

        report = await generate_plex_library_auto()
        assert report is not None
        assert report.created == 1
        assert (tmp_path / "Films" / "Auto Movie (2020)").exists()

    async def test_swallows_generation_exception_and_returns_none(
        self, db_engine, monkeypatch, tmp_path
    ):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add(_account("a"))
            await s.commit()

        import app.db.database as database_mod
        monkeypatch.setattr(database_mod, "async_session_factory", factory)
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", str(tmp_path))

        import app.services.plex_generation_service as service_mod

        async def _boom(*args, **kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(service_mod, "generate_plex_library", _boom)

        result = await service_mod.generate_plex_library_auto()
        assert result is None


# ─── app.main thin-wrapper delegation ─────────────────────────────────────


async def test_main_auto_generate_plex_library_delegates_to_service(monkeypatch):
    """app.main._auto_generate_plex_library must be a thin wrapper around the
    shared service (CR-A02), not its own reconstructed wiring."""
    import app.main as main_module
    import app.services.plex_generation_service as service_mod

    called = {"n": 0}

    async def _fake_auto():
        called["n"] += 1
        return None

    monkeypatch.setattr(service_mod, "generate_plex_library_auto", _fake_auto)
    # main.py does a lazy `from ... import generate_plex_library_auto` inside
    # the function body, so patching the source module's attribute is enough
    # for the next call to pick up the fake.
    await main_module._auto_generate_plex_library()

    assert called["n"] == 1


# ─── app.api.sync no longer reaches into app.main (layering fix) ─────────


def test_sync_module_does_not_import_app_main():
    import app.api.sync as sync_module

    source = inspect.getsource(sync_module)
    # Match actual import statements only (a comment may legitimately mention
    # "app.main" for context on why this changed — see CR-A02).
    forbidden = re.compile(r"^\s*(from app\.main import|import app\.main\b)", re.MULTILINE)
    assert not forbidden.search(source), (
        "app/api/sync.py must not import from app.main (CR-A02 layering "
        "inversion) — it should use app.services.plex_generation_service."
    )
    assert "plex_generation_service" in source


# ─── CR-S01 confinement must still short-circuit before the service call ──


async def test_generate_endpoint_confinement_runs_before_service_call(
    api_client, tmp_path, monkeypatch
):
    """CR-A02 regression guard: refactoring app/api/plex.py to delegate to the
    shared service must not move (or drop) the CR-S01 path-confinement check.
    A traversal outputDir must be rejected before the service is ever called."""
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    base = tmp_path / "library"
    base.mkdir()
    monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", str(base))

    import app.services.plex_generation_service as service_mod

    called = {"n": 0}

    async def _fake_generate(*args, **kwargs):
        called["n"] += 1
        return SyncReport()

    monkeypatch.setattr(service_mod, "generate_plex_library", _fake_generate)

    resp = await api_client.post(
        "/api/plex/generate",
        json={"outputDir": str(base / ".." / "escaped"), "dryRun": True},
        headers=API_HEADERS,
    )

    assert resp.status_code == 400
    assert "outputDir" in resp.json()["detail"]
    assert called["n"] == 0
