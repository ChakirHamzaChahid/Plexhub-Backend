"""Tests for Lot 4 (P1-B): Observability, body limits, security hardening & route audit."""
from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request as StarletteRequest

from app.api.body_limit import DEFAULT_API_MAX_BODY_BYTES, ScrapeBodyLimitMiddleware
from app.api.route_audit import assert_api_routes_authenticated
from app.config import settings
from app.utils import rate_limit
from app.utils.metrics import metadata_edits_total


def test_cors_origins_default_is_not_wildcard():
    # CR-S09: unless CORS_ORIGINS env var is explicitly set, default is empty (not '*')
    import os

    if "CORS_ORIGINS" not in os.environ:
        assert settings.CORS_ORIGINS == []


def test_resolve_client_ip_ignores_spoofed_headers_from_untrusted_peer():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (b"cf-connecting-ip", b"203.0.113.99"),
            (b"x-forwarded-for", b"203.0.113.99"),
        ],
        "client": ("198.51.100.50", 12345),
    }
    req = StarletteRequest(scope)
    assert rate_limit.resolve_client_ip(req) == "198.51.100.50"


@pytest.mark.asyncio
async def test_body_limit_enforces_default_cap_on_general_api_mutations():
    inner = FastAPI()

    @inner.post("/api/accounts")
    async def create():
        return {"ok": True}

    wrapped = ScrapeBodyLimitMiddleware(inner, default_api_limit=DEFAULT_API_MAX_BODY_BYTES)
    async with AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://test") as client:
        res = await client.post(
            "/api/accounts",
            content=b"{}",
            headers={"content-length": str(DEFAULT_API_MAX_BODY_BYTES + 1)},
        )
        assert res.status_code == 413
        assert res.json()["detail"]["code"] == "invalid_parameter"


@pytest.mark.asyncio
async def test_tv_auth_endpoints_rate_limited(api_client, db_session, monkeypatch):
    from app.db.database import get_db
    from app.main import app

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    monkeypatch.setattr(settings, "TV_AUTH_STATUS_RATE_LIMIT_MAX", 2)
    try:
        for _ in range(2):
            r = await api_client.get("/api/tv-auth/status", params={"deviceCode": "x" * 20})
            assert r.status_code in (200, 404)
        r3 = await api_client.get("/api/tv-auth/status", params={"deviceCode": "x" * 20})
        assert r3.status_code == 429
        assert "retry-after" in {k.lower() for k in r3.headers}
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_basic_auth_failure_triggers_backoff_429(api_client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "correct-horse-battery")
    monkeypatch.setattr(settings, "BASIC_AUTH_FAIL_RATE_LIMIT_MAX", 2)
    monkeypatch.setattr(settings, "BASIC_AUTH_FAIL_RATE_LIMIT_WINDOW_SECONDS", 60)

    for _ in range(2):
        r = await api_client.get("/admin", auth=("admin", "wrong"))
        assert r.status_code == 401

    r_blocked = await api_client.get("/admin", auth=("admin", "wrong"))
    assert r_blocked.status_code == 429
    assert "retry-after" in {k.lower() for k in r_blocked.headers}


def test_route_audit_catches_unguarded_non_api_routes():
    app = FastAPI()
    router = APIRouter()

    @router.get("/admin/unguarded")
    async def bad_admin():
        return {}

    app.include_router(router)
    with pytest.raises(RuntimeError, match="/admin/unguarded"):
        assert_api_routes_authenticated(app)


def test_metadata_metrics_zero_initialized():
    # CR-O04: closed label sets are pre-initialized
    assert metadata_edits_total.labels(operation="patch", result="ok")._value.get() >= 0
    assert metadata_edits_total.labels(operation="lock", result="conflict")._value.get() >= 0
    assert metadata_edits_total.labels(operation="revert", result="error")._value.get() >= 0
