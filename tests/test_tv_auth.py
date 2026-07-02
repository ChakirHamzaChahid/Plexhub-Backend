"""Integration tests for the device-flow TV pairing API (Mission 18).

Covers: full cycle start -> status(pending) -> approve -> status(approved,
payload delivered once) -> complete -> status(completed); TTL expiration;
single-use semantics; invalid/unknown codes; approve authentication;
encryption at rest; migration 009.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.migrations import _migration_009_create_tv_auth_sessions
from app.models.database import Base
from app.utils import payload_crypto


pytestmark = pytest.mark.asyncio

API_KEY = "secret-test-key"
AUTH = {"X-API-Key": API_KEY}
PAYLOAD = {
    "plexToken": "xyz-PLEX-TOKEN-123",
    "backendUrl": "http://192.168.0.175:8070",
    "apiKey": "shared-secret",
}


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def tv_engine(tmp_path):
    """File-backed async engine (avoids the :memory: pooled-connection trap)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'tv_auth_test.db'}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def tv_factory(tv_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(tv_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def tv_client(tv_factory, monkeypatch, tmp_path) -> AsyncIterator[AsyncClient]:
    """ASGI client with the shared API key set and get_db overridden onto the
    per-test file-backed engine."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")  # derive from AI_API_KEY

    from app.main import app
    from app.db import database as db_module

    async def _override_get_db():
        async with tv_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[db_module.get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(db_module.get_db, None)


async def _start(client: AsyncClient, device_name: str = "Mi Box S test") -> dict:
    resp = await client.post("/api/tv-auth/start", json={"deviceName": device_name})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _force_expire(tv_factory, device_code: str) -> None:
    """Rewind expires_at into the past for a given session."""
    async with tv_factory() as s:
        await s.execute(
            text("UPDATE tv_auth_sessions SET expires_at = 1000 WHERE device_code = :dc"),
            {"dc": device_code},
        )
        await s.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Full cycle
# ──────────────────────────────────────────────────────────────────────────────

async def test_start_response_shape(tv_client):
    data = await _start(tv_client)
    assert set(data.keys()) == {
        "deviceCode", "userCode", "verificationUri", "expiresIn", "interval",
    }
    # deviceCode: long opaque secret; userCode: ABCD-EFGH display form
    assert len(data["deviceCode"]) >= 32
    assert len(data["userCode"]) == 9 and data["userCode"][4] == "-"
    assert data["expiresIn"] == settings.TV_AUTH_TTL_SECONDS == 900
    assert data["interval"] >= 1
    assert data["verificationUri"].endswith("/api/tv-auth/approve")


async def test_full_cycle(tv_client):
    started = await _start(tv_client)
    device_code, user_code = started["deviceCode"], started["userCode"]

    # 1. pending
    resp = await tv_client.get("/api/tv-auth/status", params={"device_code": device_code})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["payload"] is None
    assert 0 < body["expiresIn"] <= 900

    # 2. approve (mobile, authenticated) — uses the displayed ABCD-EFGH form
    resp = await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": user_code, "payload": PAYLOAD},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    # 3. first poll after approval delivers the decrypted payload
    resp = await tv_client.get("/api/tv-auth/status", params={"device_code": device_code})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["payload"] == PAYLOAD

    # 4. complete (one-shot)
    resp = await tv_client.post("/api/tv-auth/complete", json={"deviceCode": device_code})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"

    # 5. terminal status, payload never re-delivered
    resp = await tv_client.get("/api/tv-auth/status", params={"device_code": device_code})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["payload"] is None


async def test_payload_delivered_exactly_once(tv_client):
    started = await _start(tv_client)
    await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )

    first = await tv_client.get(
        "/api/tv-auth/status", params={"device_code": started["deviceCode"]}
    )
    assert first.json()["payload"] == PAYLOAD

    second = await tv_client.get(
        "/api/tv-auth/status", params={"device_code": started["deviceCode"]}
    )
    body = second.json()
    assert body["status"] == "approved"
    assert body["payload"] is None  # single delivery


# ──────────────────────────────────────────────────────────────────────────────
# Expiration (TTL 15 min)
# ──────────────────────────────────────────────────────────────────────────────

async def test_expired_session_status(tv_client, tv_factory):
    started = await _start(tv_client)
    await _force_expire(tv_factory, started["deviceCode"])

    resp = await tv_client.get(
        "/api/tv-auth/status", params={"device_code": started["deviceCode"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "expired"
    assert body["payload"] is None


async def test_expired_session_rejects_approve(tv_client, tv_factory):
    started = await _start(tv_client)
    await _force_expire(tv_factory, started["deviceCode"])

    resp = await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )
    assert resp.status_code == 410


async def test_expired_approved_session_never_delivers_payload(tv_client, tv_factory):
    """Payload attached, then session expires BEFORE the TV polls: the payload
    must not be delivered and must be scrubbed from the DB."""
    started = await _start(tv_client)
    await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )
    await _force_expire(tv_factory, started["deviceCode"])

    resp = await tv_client.get(
        "/api/tv-auth/status", params={"device_code": started["deviceCode"]}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"
    assert resp.json()["payload"] is None

    # complete is also refused
    resp = await tv_client.post(
        "/api/tv-auth/complete", json={"deviceCode": started["deviceCode"]}
    )
    assert resp.status_code == 410

    # the encrypted blob has been scrubbed at rest
    async with tv_factory() as s:
        row = (
            await s.execute(
                text("SELECT payload_encrypted FROM tv_auth_sessions WHERE device_code = :dc"),
                {"dc": started["deviceCode"]},
            )
        ).fetchone()
    assert row is not None and row[0] is None


# ──────────────────────────────────────────────────────────────────────────────
# Single-use / state machine
# ──────────────────────────────────────────────────────────────────────────────

async def test_complete_is_one_shot(tv_client):
    started = await _start(tv_client)
    await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )
    first = await tv_client.post(
        "/api/tv-auth/complete", json={"deviceCode": started["deviceCode"]}
    )
    assert first.status_code == 200

    second = await tv_client.post(
        "/api/tv-auth/complete", json={"deviceCode": started["deviceCode"]}
    )
    assert second.status_code == 409


async def test_complete_requires_prior_approval(tv_client):
    started = await _start(tv_client)
    resp = await tv_client.post(
        "/api/tv-auth/complete", json={"deviceCode": started["deviceCode"]}
    )
    assert resp.status_code == 409


async def test_approve_twice_conflict(tv_client):
    started = await _start(tv_client)
    body = {"userCode": started["userCode"], "payload": PAYLOAD}
    first = await tv_client.post("/api/tv-auth/approve", json=body, headers=AUTH)
    assert first.status_code == 200
    second = await tv_client.post("/api/tv-auth/approve", json=body, headers=AUTH)
    assert second.status_code == 409


# ──────────────────────────────────────────────────────────────────────────────
# Invalid input / auth
# ──────────────────────────────────────────────────────────────────────────────

async def test_unknown_codes_404(tv_client):
    resp = await tv_client.get(
        "/api/tv-auth/status", params={"device_code": "x" * 43}
    )
    assert resp.status_code == 404

    resp = await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": "ZZZZ-ZZZZ", "payload": PAYLOAD},
        headers=AUTH,
    )
    assert resp.status_code == 404

    resp = await tv_client.post(
        "/api/tv-auth/complete", json={"deviceCode": "x" * 43}
    )
    assert resp.status_code == 404


async def test_approve_requires_api_key(tv_client):
    started = await _start(tv_client)
    body = {"userCode": started["userCode"], "payload": PAYLOAD}

    no_key = await tv_client.post("/api/tv-auth/approve", json=body)
    assert no_key.status_code == 401

    bad_key = await tv_client.post(
        "/api/tv-auth/approve", json=body, headers={"X-API-Key": "wrong"}
    )
    assert bad_key.status_code == 401


async def test_approve_rejects_empty_payload(tv_client):
    started = await _start(tv_client)
    resp = await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": {}},
        headers=AUTH,
    )
    assert resp.status_code == 422


async def test_user_code_normalization(tv_client):
    """The mobile may type 'abcd efgh' or 'abcd-efgh' — both must match."""
    started = await _start(tv_client)
    sloppy = started["userCode"].replace("-", " ").lower()
    resp = await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": sloppy, "payload": PAYLOAD},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text


async def test_unconfigured_returns_503(tv_client, monkeypatch):
    """No encryption key resolvable (no TV_AUTH_ENCRYPTION_KEY, no AI_API_KEY)
    -> pairing unavailable, never a crash.

    /start is public, so it reaches the encryption check and returns 503.
    /approve is auth-gated: with AI_API_KEY nulled the AUTH key no longer
    matches the master secret, so it fails closed with 401 before the handler
    (still never a crash)."""
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")

    resp = await tv_client.post("/api/tv-auth/start", json={})
    assert resp.status_code == 503

    resp = await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": "ABCD-EFGH", "payload": PAYLOAD},
        headers=AUTH,
    )
    assert resp.status_code == 401


# ──────────────────────────────────────────────────────────────────────────────
# Encryption at rest
# ──────────────────────────────────────────────────────────────────────────────

async def test_payload_encrypted_at_rest(tv_client, tv_factory):
    started = await _start(tv_client)
    await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )

    async with tv_factory() as s:
        row = (
            await s.execute(
                text("SELECT payload_encrypted FROM tv_auth_sessions WHERE device_code = :dc"),
                {"dc": started["deviceCode"]},
            )
        ).fetchone()

    stored = row[0]
    assert stored is not None
    # No plaintext secret leaks into the stored blob
    assert "xyz-PLEX-TOKEN-123" not in stored
    assert "plexToken" not in stored
    # Fernet tokens start with the version byte 0x80 -> base64 'gAAAA'
    assert stored.startswith("gAAAA")
    # And it round-trips with the configured (derived) key
    assert payload_crypto.decrypt_payload(stored) == PAYLOAD


async def test_complete_scrubs_payload_at_rest(tv_client, tv_factory):
    started = await _start(tv_client)
    await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )
    await tv_client.get(
        "/api/tv-auth/status", params={"device_code": started["deviceCode"]}
    )
    await tv_client.post(
        "/api/tv-auth/complete", json={"deviceCode": started["deviceCode"]}
    )

    async with tv_factory() as s:
        row = (
            await s.execute(
                text("SELECT payload_encrypted, status FROM tv_auth_sessions WHERE device_code = :dc"),
                {"dc": started["deviceCode"]},
            )
        ).fetchone()
    assert row is not None
    assert row[0] is None  # scrubbed
    assert row[1] == "completed"


# ──────────────────────────────────────────────────────────────────────────────
# Migration 009
# ──────────────────────────────────────────────────────────────────────────────

async def test_migration_009_creates_table_and_indexes(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'mig009.db'}", future=True
    )
    try:
        await _migration_009_create_tv_auth_sessions(engine)
        # idempotent re-run must not raise
        await _migration_009_create_tv_auth_sessions(engine)

        async with engine.connect() as conn:
            tables = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='tv_auth_sessions'")
                )
            ).fetchall()
            assert len(tables) == 1

            indexes = {
                r[0]
                for r in (
                    await conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tv_auth_sessions'")
                    )
                ).fetchall()
            }
            assert {
                "uix_tv_auth_device_code",
                "uix_tv_auth_user_code",
                "ix_tv_auth_expires",
                "ix_tv_auth_status",
            }.issubset(indexes)
    finally:
        await engine.dispose()
