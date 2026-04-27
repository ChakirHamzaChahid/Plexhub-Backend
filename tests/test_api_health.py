"""Demo integration test: GET /api/health end-to-end via ASGI client.

Pattern for future API tests — see conftest.py for the `api_client` and
`db_engine` fixtures.
"""
from __future__ import annotations

import pytest

from app.db import database as db_module
from app.models.database import XtreamAccount


pytestmark = pytest.mark.asyncio


async def test_health_returns_ok_with_zero_accounts(monkeypatch, api_client, db_engine):
    """`/api/health` should return 200 with empty stats when no account exists."""
    # Wire the health endpoint's `Depends(get_db)` into our test engine.
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)

    resp = await api_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    # HealthResponse serializes to camelCase (alias_generator=to_camel).
    assert body["status"] == "ok"
    assert body["accounts"] == 0
    assert body["totalMedia"] == 0
    assert body["lastSyncAt"] is None


async def test_health_counts_existing_accounts(monkeypatch, api_client, db_engine, db_factory):
    """After inserting an account, /api/health reports the count."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)

    async with db_factory() as session:
        session.add(XtreamAccount(
            id="abc12345", label="test", base_url="http://x", port=80,
            username="u", password="p", is_active=True, created_at=0,
            last_synced_at=1234567890,
        ))
        await session.commit()

    resp = await api_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["accounts"] == 1
    assert body["lastSyncAt"] == 1234567890


async def test_request_id_echoed_in_response_header(api_client, db_factory, monkeypatch):
    """RequestIdMiddleware should echo (or generate) X-Request-ID."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    resp = await api_client.get("/api/health", headers={"X-Request-ID": "abc-123"})
    assert resp.headers.get("X-Request-ID") == "abc-123"
