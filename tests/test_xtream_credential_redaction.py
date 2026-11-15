"""Regression tests for CR-S01 / CR-S02: Xtream credential redaction.

Verifies that ``username`` and ``password`` embedded in Xtream URLs inside
``httpx.HTTPStatusError`` or network/transport exceptions are never leaked in:
- HTTP 400 responses from ``POST /api/accounts/{id}/test`` (CR-S01)
- Logs from ``xtream_service``, ``sync_worker``, and ``main._auto_provision_xtream_account`` (CR-S02)
"""
from __future__ import annotations

import logging

import httpx
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import XtreamAccount
from app.services import xtream_service as xs_module
from app.services.xtream_service import XtreamService, safe_xtream_error

SECRET_USER = "xtream-secret-user-99"
SECRET_PASS = "xtream-secret-pass-42"
API_KEY = "test-master-key"
API_HEADERS = {"X-API-Key": API_KEY}


@pytest_asyncio.fixture(autouse=True)
def _propagate_plexhub_logs(monkeypatch):
    """app.main sets plexhub.propagate = False; enable propagation for caplog."""
    monkeypatch.setattr(logging.getLogger("plexhub"), "propagate", True)


def _http_status_error(status_code: int = 401) -> httpx.HTTPStatusError:
    url = (
        f"http://prov.example:8080/player_api.php"
        f"?username={SECRET_USER}&password={SECRET_PASS}"
    )
    req = httpx.Request("GET", url)
    resp = httpx.Response(status_code, request=req)
    return httpx.HTTPStatusError(
        f"Client error '{status_code}' for url '{url}'",
        request=req,
        response=resp,
    )


def _connect_error() -> httpx.ConnectError:
    url = (
        f"http://prov.example:8080/player_api.php"
        f"?username={SECRET_USER}&password={SECRET_PASS}"
    )
    req = httpx.Request("GET", url)
    return httpx.ConnectError(f"Failed to connect to {url}", request=req)


@pytest_asyncio.fixture
async def seeded_secret_account(db_engine, monkeypatch):
    from app.db import database as db_module

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)

    async with factory() as s:
        s.add(
            XtreamAccount(
                id="sec1",
                label="Secret",
                base_url="http://prov.example",
                port=8080,
                username=SECRET_USER,
                password=SECRET_PASS,
                is_active=True,
                created_at=0,
            )
        )
        await s.commit()

    return factory


# ─── CR-S01: POST /api/accounts/{id}/test ─────────────────────────────────


async def test_post_account_test_redacts_http_status_error(
    monkeypatch, api_client, seeded_secret_account,
):
    async def _boom(_account):
        raise _http_status_error(401)

    monkeypatch.setattr(xs_module.xtream_service, "authenticate", _boom)

    resp = await api_client.post("/api/accounts/sec1/test", headers=API_HEADERS)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail == "Connection test failed: HTTP 401"
    assert SECRET_USER not in resp.text
    assert SECRET_PASS not in resp.text
    assert "player_api.php" not in resp.text


async def test_post_account_test_redacts_network_error(
    monkeypatch, api_client, seeded_secret_account,
):
    async def _boom(_account):
        raise _connect_error()

    monkeypatch.setattr(xs_module.xtream_service, "authenticate", _boom)

    resp = await api_client.post("/api/accounts/sec1/test", headers=API_HEADERS)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail == "Connection test failed: ConnectError"
    assert SECRET_USER not in resp.text
    assert SECRET_PASS not in resp.text
    assert "player_api.php" not in resp.text


# ─── CR-S02: safe_xtream_error & log redaction ────────────────────────────


def test_safe_xtream_error_formats_without_credentials():
    msg_http = safe_xtream_error(_http_status_error(503))
    assert msg_http == "HTTP 503"
    assert SECRET_USER not in msg_http and SECRET_PASS not in msg_http

    msg_conn = safe_xtream_error(_connect_error())
    assert msg_conn == "ConnectError"
    assert SECRET_USER not in msg_conn and SECRET_PASS not in msg_conn


async def test_xtream_service_get_retry_log_redacts_credentials(monkeypatch, caplog):
    async def _instant(_):
        return None

    monkeypatch.setattr(xs_module.asyncio, "sleep", _instant)

    svc = XtreamService()
    with caplog.at_level(logging.WARNING, logger="plexhub.xtream"):
        with respx.mock:
            respx.get("http://prov.example:8080/player_api.php").mock(
                side_effect=[
                    _connect_error(),
                    httpx.Response(200, json={"user_info": {"status": "Active"}}),
                ]
            )
            data = await svc.authenticate(
                "http://prov.example", 8080, SECRET_USER, SECRET_PASS,
            )
    await svc.close()

    assert data == {"user_info": {"status": "Active"}}
    assert "ConnectError" in caplog.text
    assert SECRET_USER not in caplog.text
    assert SECRET_PASS not in caplog.text


async def test_auto_provision_log_redacts_credentials(
    monkeypatch, db_engine, caplog,
):
    from app import main as main_module
    from app.db import database as db_module

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(settings, "XTREAM_BASE_URL", "http://prov.example")
    monkeypatch.setattr(settings, "XTREAM_PORT", 8080)
    monkeypatch.setattr(settings, "XTREAM_USERNAME", SECRET_USER)
    monkeypatch.setattr(settings, "XTREAM_PASSWORD", SECRET_PASS)

    async def _boom(_creds):
        raise _http_status_error(403)

    monkeypatch.setattr(xs_module.xtream_service, "authenticate", _boom)

    with caplog.at_level(logging.ERROR, logger="plexhub"):
        await main_module._auto_provision_xtream_account()

    assert "HTTP 403" in caplog.text
    assert SECRET_USER not in caplog.text
    assert SECRET_PASS not in caplog.text


async def test_sync_worker_logs_redact_credentials(monkeypatch, caplog):
    from app.workers import sync_worker

    async def _boom(*_args, **_kwargs):
        raise _http_status_error(401)

    monkeypatch.setattr(sync_worker.xtream_service, "get_vod_categories", _boom)

    acc = XtreamAccount(
        id="acc1",
        label="Test",
        base_url="http://prov.example",
        port=8080,
        username=SECRET_USER,
        password=SECRET_PASS,
        is_active=True,
        created_at=0,
    )

    with caplog.at_level(logging.WARNING):
        streams, failed = await sync_worker._fetch_streams_by_categories(
            _boom, acc, ["10"], "VOD",
        )
        await sync_worker._refresh_categories(None, acc, "acc1")

    assert streams == []
    assert failed == {"10"}
    assert "HTTP 401" in caplog.text
    assert SECRET_USER not in caplog.text
    assert SECRET_PASS not in caplog.text


async def test_sync_account_outer_except_redacts_httpx_error(
    monkeypatch, db_engine, caplog,
):
    from app.services import job_registry
    from app.workers import sync_worker

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sync_worker, "worker_session_factory", factory)

    async with factory() as s:
        s.add(
            XtreamAccount(
                id="acc2",
                label="Test",
                base_url="http://prov.example",
                port=8080,
                username=SECRET_USER,
                password=SECRET_PASS,
                is_active=True,
                created_at=0,
            )
        )
        await s.commit()

    async def _boom(*_args, **_kwargs):
        raise _http_status_error(502)

    monkeypatch.setattr(sync_worker, "_refresh_categories", _boom)

    with caplog.at_level(logging.ERROR):
        job_id = await sync_worker.sync_account("acc2")

    job = job_registry.get_job(job_id)
    assert job is not None
    assert job["status"] == "failed"
    assert job["error"] == "HTTP 502"
    assert SECRET_USER not in caplog.text
    assert SECRET_PASS not in caplog.text
    assert SECRET_USER not in str(job)
    assert SECRET_PASS not in str(job)
