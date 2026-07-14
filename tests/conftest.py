"""Shared fixtures: async DB factory, FastAPI test client, TMDB/Xtream HTTP mocks."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.database import Base


# ─── Async DB ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_engine():
    """Fresh in-memory SQLite engine for one test, with all tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncIterator[AsyncSession]:
    """One AsyncSession per test, rolled back at teardown for isolation."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def db_factory(db_engine):
    """Sessionmaker for tests that want to open multiple short sessions."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


# ─── FastAPI client ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def api_client(monkeypatch, tmp_path) -> AsyncIterator[AsyncClient]:
    """ASGI in-process AsyncClient against `app.main.app`.

    Skips the master-election lifespan (uses `fcntl` which doesn't exist on
    Windows and isn't relevant for unit-level API tests).
    """
    # Redirect data/log dirs into tmp to avoid touching real filesystem.
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg.settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ─── External HTTP mocks ─────────────────────────────────────────────────


@pytest.fixture
def tmdb_mock():
    """respx context that mocks api.themoviedb.org for the test duration.

    Usage::

        async def test_x(tmdb_mock):
            tmdb_mock.get("/3/search/movie").respond(200, json={"results": [...]})
            ...
    """
    with respx.mock(base_url="https://api.themoviedb.org", assert_all_called=False) as r:
        yield r


@pytest.fixture
def xtream_mock():
    """respx context with no base_url — caller registers full URLs."""
    with respx.mock(assert_all_called=False) as r:
        yield r


# ─── Physical media download (PH-DL-07) ─────────────────────────────────


@pytest.fixture
def download_dir(tmp_path, monkeypatch):
    """A tmp_path-backed `settings.DOWNLOAD_DIR` for download-feature tests.

    Never the real filesystem — every download test that writes bytes does so
    under this fixture's directory (F-007: 0 write outside DOWNLOAD_DIR is the
    blocking invariant, docs/40-testplan-media-download.md §3 F-007).
    """
    from app.config import settings

    d = tmp_path / "downloads"
    d.mkdir()
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(d))
    return d
