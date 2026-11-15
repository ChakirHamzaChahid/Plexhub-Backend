"""Targeted tests for Lot 5 (CR-A01, CR-A02, CR-A06, CR-C01, CR-T04, CR-O05, CR-O06)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.csrf import AdminCsrfMiddleware
from app.plex_generator.naming import sanitize_for_filesystem
from app.services.download_service import _sanitize_segment
from app.utils.path_naming import sanitize_path_segment
from app.utils.request_context import RequestIdMiddleware


def test_middlewares_are_pure_asgi_not_base_http():
    assert not issubclass(RequestIdMiddleware, BaseHTTPMiddleware)
    assert not issubclass(AdminCsrfMiddleware, BaseHTTPMiddleware)


def test_pure_asgi_request_id_and_admin_csrf():
    mini = FastAPI()

    @mini.get("/ping")
    async def ping():
        return PlainTextResponse("ok")

    @mini.post("/admin/action")
    async def admin_action():
        return PlainTextResponse("done")

    mini.add_middleware(RequestIdMiddleware)
    mini.add_middleware(AdminCsrfMiddleware)

    client = TestClient(mini)
    res = client.get("/ping", headers={"X-Request-ID": "req-lot5"})
    assert res.status_code == 200
    assert res.headers["X-Request-ID"] == "req-lot5"

    blocked = client.post("/admin/action", headers={"Sec-Fetch-Site": "cross-site"})
    assert blocked.status_code == 403

    allowed = client.post("/admin/action", headers={"Sec-Fetch-Site": "same-origin"})
    assert allowed.status_code == 200


def test_settings_init_has_no_fs_side_effects(tmp_path: Path, monkeypatch):
    from app.config import Settings

    data_dir = tmp_path / "ro_data"
    log_dir = tmp_path / "ro_logs"
    trailer_dir = tmp_path / "ro_trailers"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    monkeypatch.setenv("TRAILER_CACHE_DIR", str(trailer_dir))

    with patch.object(Settings, "DATA_DIR", data_dir), patch.object(
        Settings, "LOG_DIR", log_dir
    ):
        cfg = Settings()
        assert not data_dir.exists()
        assert not log_dir.exists()
        assert not trailer_dir.exists()

        cfg.ensure_dirs()
        assert data_dir.exists()
        assert log_dir.exists()
        assert trailer_dir.exists()


@pytest.mark.asyncio
async def test_configure_engines_and_init_db_temp_file(tmp_path: Path):
    from sqlalchemy import text

    from app.db import database

    orig_url = database.DATABASE_URL
    db_file = tmp_path / "lot5_init.db"
    try:
        await database.init_db(db_url=f"sqlite+aiosqlite:///{db_file}")
        async with database.engine.connect() as conn:
            row = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
            assert str(row).lower() == "wal"
    finally:
        await database.engine.dispose()
        await database.worker_engine.dispose()
        database.configure_engines(orig_url)


def test_shared_path_naming_sanitization():
    # Reserved Windows names are neutralized on both Plex & Download paths
    assert sanitize_for_filesystem("CON") == "CON_"
    assert _sanitize_segment("nul", fallback="Unknown") == "nul_"
    # Control chars & traversal markers stripped
    assert ".." not in sanitize_path_segment("../secret\u202etest")
    # Plex keeps space for ':', Download remaps ':' -> ' - '
    assert sanitize_for_filesystem("Film: Part 1") == "Film Part 1"
    assert _sanitize_segment("Film: Part 1", fallback="Unknown") == "Film - Part 1"


@pytest.mark.asyncio
async def test_lifespan_rejects_multi_worker_env(tmp_path: Path, monkeypatch):
    from app.main import app, lifespan

    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setattr("app.config.settings.DATA_DIR", tmp_path)
    monkeypatch.setattr("app.config.settings.LOG_DIR", tmp_path)
    monkeypatch.setattr("app.config.settings.TRAILER_CACHE_DIR", "")

    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY=2"):
        async with lifespan(app):
            pass
