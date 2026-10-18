"""Shared fixtures of the scrape gateway tests (B1-11, `tests/test_scrape_v1_*.py`).

- `gw_db`: a real file-backed SQLite (tmp_path, WAL, schema from `create_all`)
  wired into `app.db.database.async_session_factory` (read at call time by
  the gateway) and into `api_key_service` (per-user key resolution).
Registered as a plugin by `tests/conftest.py` (`pytest_plugins`): the
fixtures are available by name, without an import that ruff would flag.

- `gw_settings`: fake keys only — the TMDB AND OMDb keys are the canary
  `TESTKEY-LEAK-CANARY`, so any leak test can grep for it.
- `gw_providers`: fresh httpx clients on the `tmdb_service`/`omdb_service`
  singletons (their client binds the key at creation), counters reset.
- `gw_http`: ONE respx router for both providers (full URLs).
- `gw_client`: in-process ASGI client on `app.main.app` (no lifespan).
- `gw_flights_cleanup` (made autouse by each gateway test module): no flight task
  outlives its test.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db import database as db_module
from app.models.database import ApiKey, Base
from app.services import api_key_service
from app.services import scrape_gateway_service as gateway
from app.services.omdb_service import omdb_service
from app.services.tmdb_service import tmdb_service
from app.utils.time import now_ms

CANARY = "TESTKEY-LEAK-CANARY"
MASTER = "test-master-key-scrape-gateway"
MASTER_HEADERS = {"X-API-Key": MASTER}

TMDB = "https://api.themoviedb.org/3"
OMDB = "https://www.omdbapi.com/"
FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"
BASE = "/api/scrape/v1"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def search_hit(tmdb_id: int, title: str, date: str = "1999-03-31", **extra) -> dict:
    return {"id": tmdb_id, "title": title, "original_title": title, "release_date": date,
            "overview": f"Overview {tmdb_id}", "poster_path": f"/p{tmdb_id}.jpg",
            "vote_count": 100, **extra}


@pytest_asyncio.fixture
async def gw_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
def gw_settings(monkeypatch):
    for name, value in {
        "AI_API_KEY": MASTER,
        "TMDB_API_KEY": CANARY,
        "OMDB_API_KEY": CANARY,
        "TMDB_LANGUAGE": "fr-FR",
        "OMDB_DAILY_LIMIT": 100_000,
        "SCRAPE_TTL_SUCCESS_DAYS": 30,
        "SCRAPE_TTL_NEGATIVE_DAYS": 3,
        "SCRAPE_REQUEST_TIMEOUT_SECONDS": 25,
        "SCRAPE_RATE_LIMIT_PER_MINUTE": 300,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return settings


@pytest_asyncio.fixture
async def gw_providers(gw_settings):
    """Fresh clients: the singletons' httpx client binds the key at creation."""
    for svc in (tmdb_service, omdb_service):
        await svc.close()
        svc.reset_request_count()
    yield
    for svc in (tmdb_service, omdb_service):
        await svc.close()
        svc.reset_request_count()


@pytest.fixture
def gw_http():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def gw_no_sleep(monkeypatch):
    """Skip the providers' retry sleeps (ConnectError/503 retried 3 times).
    NOTE: patches `asyncio.sleep` globally; a test needing a real wait must
    use an Event, not `asyncio.sleep`."""
    recorded: list[float] = []

    async def _sleep(delay, *args, **kwargs):
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return recorded


@pytest_asyncio.fixture
async def gw_client(gw_db, gw_providers, gw_flights_cleanup, monkeypatch, tmp_path) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def gw_flights_cleanup():
    yield
    tasks = list(gateway._in_flight.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    gateway._in_flight.clear()


async def make_key(factory, *, scopes: str = "", revoked: bool = False, expired: bool = False) -> str:
    """Insert a per-user key (raw scopes column) and return its plaintext."""
    plaintext = api_key_service.generate_token()
    now = now_ms()
    async with factory() as db:
        db.add(ApiKey(
            id=uuid.uuid4().hex,
            key_hash=api_key_service._hash(plaintext),
            key_prefix=plaintext[:10],
            label="scrape-test",
            created_at=now - 10_000,
            expires_at=(now - 1_000) if expired else None,
            revoked_at=(now - 1_000) if revoked else None,
            scopes=scopes,
        ))
        await db.commit()
    return plaintext


@pytest.fixture
def gw_logs(caplog):
    """Capture EVERY logger (`plexhub` has propagate=False once app.main is
    imported). httpx/httpcore stay pinned at WARNING like production
    (`app/main.py`: at INFO httpx logs the full request URL)."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="httpx")
    caplog.set_level(logging.WARNING, logger="httpcore")
    plexhub = logging.getLogger("plexhub")
    plexhub.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        plexhub.removeHandler(caplog.handler)


def all_log_text(caplog) -> str:
    """Every captured record, message AND formatted traceback."""
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(formatter.format(r) for r in caplog.records)
