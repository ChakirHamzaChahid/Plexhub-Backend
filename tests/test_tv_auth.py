"""Integration tests for the device-flow TV pairing API (Mission 18).

Covers: full cycle start -> status(pending) -> approve -> status(approved,
payload delivered once) -> complete -> status(completed); TTL expiration;
single-use semantics; invalid/unknown codes; approve authentication;
encryption at rest; migration 009; CR-F06 (deviceCode/device_code contract)
and CR-F07 (atomic one-shot payload delivery under concurrency).
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time as _time_module
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
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

    # AUDIT-P1-001 (S3.3): several tv_auth writes now go through
    # write_with_retry, whose default session_factory is a fresh
    # `from app.db.database import async_session_factory` at call time —
    # it does NOT go through FastAPI's dependency-override mechanism the
    # way `Depends(get_db)` does. Without this, those writes would silently
    # hit the REAL (untouched) production async_session_factory instead of
    # this test's file-backed `tv_factory` (CLAUDE.md isolation rule).
    monkeypatch.setattr(db_module, "async_session_factory", tv_factory)

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


async def _spy_write_with_retry(monkeypatch, calls: dict) -> None:
    """Wrap app.api.tv_auth.write_with_retry with a call-counting passthrough
    (still delegates to the real helper, so retry/commit semantics are
    unchanged — only observed)."""
    import app.api.tv_auth as tv_auth_module

    real = tv_auth_module.write_with_retry

    async def _spy(work, **kwargs):
        calls["n"] += 1
        return await real(work, **kwargs)

    monkeypatch.setattr(tv_auth_module, "write_with_retry", _spy)


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
# CR-F06: /status accepts deviceCode (camelCase) AND device_code (legacy)
# ──────────────────────────────────────────────────────────────────────────────

async def test_status_accepts_camelcase_device_code(tv_client):
    """CR-F06: a consistent client using camelCase everywhere (deviceCode) must
    not get a 422 on /status — the rest of the API is camelCase."""
    started = await _start(tv_client)

    resp = await tv_client.get(
        "/api/tv-auth/status", params={"deviceCode": started["deviceCode"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"


async def test_status_still_accepts_legacy_snake_case_device_code(tv_client):
    """CR-F06: existing callers using device_code (snake_case) keep working."""
    started = await _start(tv_client)

    resp = await tv_client.get(
        "/api/tv-auth/status", params={"device_code": started["deviceCode"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"


async def test_status_missing_device_code_returns_422(tv_client):
    """CR-F06: neither deviceCode nor device_code supplied -> 422, not a 404
    or a crash."""
    resp = await tv_client.get("/api/tv-auth/status")
    assert resp.status_code == 422


# ──────────────────────────────────────────────────────────────────────────────
# CR-F07: atomic one-shot payload delivery under concurrency
# ──────────────────────────────────────────────────────────────────────────────

async def test_status_concurrent_polls_deliver_payload_exactly_once(tv_client, tmp_path):
    """CR-F07: two truly concurrent /status polls racing on the same
    undelivered payload must not both receive it.

    Plain `asyncio.gather` against a single in-process event loop does not
    reliably reproduce this race (one coroutine tends to run to completion
    before the other's first await is even scheduled). To exercise the real
    race window — two independent requests, each with its own DB connection,
    genuinely overlapping in time, exactly like two workers/processes hitting
    the same SQLite file — this drives the actual `get_status` handler from
    two separate OS threads, each with its own event loop and its own engine
    connected to the SAME session DB file used by `tv_client`.

    Before the fix: both threads can observe `payload_delivered=False` and
    both decrypt+return the payload (the mark-delivered write happens only
    after the read, non-atomically) -> flaky double delivery.
    After the fix: the read-and-mark-delivered step is a single conditional
    UPDATE (`payload_delivered` False -> True, guarded by rowcount), so only
    one of the two threads can ever win the race.
    """
    from app.api.tv_auth import get_status

    started = await _start(tv_client)
    await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )

    db_path = tmp_path / "tv_auth_test.db"  # same file tv_engine/tv_factory point at
    device_code = started["deviceCode"]

    def _poll_in_own_thread(results: list, idx: int) -> None:
        async def _poll() -> dict | None:
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
            session_factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            try:
                async with session_factory() as session:
                    resp = await get_status(
                        device_code=device_code,
                        device_code_legacy=None,
                        db=session,
                    )
                return resp.payload
            finally:
                await engine.dispose()

        try:
            results[idx] = asyncio.run(_poll())
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            results[idx] = exc

    results: list = [None, None]
    t1 = threading.Thread(target=_poll_in_own_thread, args=(results, 0))
    t2 = threading.Thread(target=_poll_in_own_thread, args=(results, 1))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    for r in results:
        assert not isinstance(r, Exception), f"poll raised: {r!r}"

    delivered = [r for r in results if r is not None]
    assert len(delivered) == 1, (
        f"payload must be delivered to exactly one of the two racing polls, got {results!r}"
    )
    assert delivered[0] == PAYLOAD

    # A subsequent poll (via the normal HTTP client) confirms the session is
    # left in a consistent already-delivered state (no third delivery).
    third = await tv_client.get(
        "/api/tv-auth/status", params={"deviceCode": device_code}
    )
    assert third.status_code == 200
    body = third.json()
    assert body["status"] == "approved"
    assert body["payload"] is None


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
# AUDIT-P1-001 (S3.3): request-path writes commit via write_with_retry
# (fresh-session-per-attempt lock-retry, ADR 0004 Decision 4)
# ──────────────────────────────────────────────────────────────────────────────

async def test_start_approve_and_complete_commit_via_retry_helper(tv_client, monkeypatch):
    """AUDIT-P1-001: /start, /approve and /complete must commit through
    write_with_retry (not a bare db.commit(), and not commit_with_retry —
    which cannot actually survive a real lock, ADR 0004 Decision 4), same
    resilience pattern as the workers. Wraps (doesn't replace) the real
    helper, so behavior is unchanged — only observed via the call counter."""
    calls = {"n": 0}
    await _spy_write_with_retry(monkeypatch, calls)

    before_start = calls["n"]
    started = await _start(tv_client)
    assert calls["n"] > before_start

    before_approve = calls["n"]
    approve_resp = await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )
    assert approve_resp.status_code == 200, approve_resp.text
    assert calls["n"] > before_approve

    before_complete = calls["n"]
    complete_resp = await tv_client.post(
        "/api/tv-auth/complete", json={"deviceCode": started["deviceCode"]}
    )
    assert complete_resp.status_code == 200, complete_resp.text
    assert calls["n"] > before_complete


async def test_expire_path_commits_via_retry_helper(tv_client, tv_factory, monkeypatch):
    """AUDIT-P1-001: the lazy-expiry write in `_expire_if_needed` (exercised
    by /approve on an already-expired session) must also go through
    write_with_retry."""
    calls = {"n": 0}
    await _spy_write_with_retry(monkeypatch, calls)

    started = await _start(tv_client)
    await _force_expire(tv_factory, started["deviceCode"])

    before = calls["n"]
    resp = await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )
    assert resp.status_code == 410, resp.text
    assert calls["n"] > before  # the expire-and-scrub write went through write_with_retry


async def test_get_status_claim_still_uses_commit_with_retry(tv_client, monkeypatch):
    """AUDIT-P1-001 (S3.3) — deliberately NOT converted: the CR-F07 atomic
    one-shot delivery claim in get_status() stays on commit_with_retry (see
    the comment at that call site). This guards against an accidental
    future conversion regressing the atomicity test
    (test_status_concurrent_polls_deliver_payload_exactly_once)."""
    import app.api.tv_auth as tv_auth_module

    calls = {"n": 0}
    real_commit_with_retry = tv_auth_module.commit_with_retry

    async def _spy(db, **kwargs):
        calls["n"] += 1
        return await real_commit_with_retry(db, **kwargs)

    monkeypatch.setattr(tv_auth_module, "commit_with_retry", _spy)

    started = await _start(tv_client)
    await tv_client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )

    before = calls["n"]
    resp = await tv_client.get(
        "/api/tv-auth/status", params={"deviceCode": started["deviceCode"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["payload"] == PAYLOAD
    assert calls["n"] > before  # the delivery claim still committed via commit_with_retry


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


# ──────────────────────────────────────────────────────────────────────────────
# AUDIT-P1-001 (S3.3): real-lock contention (ADR 0004, Decision 4) — proves
# write_with_retry actually survives a genuine SQLite writer-vs-writer lock
# on the converted tv_auth endpoints, not just that a helper is called.
# ──────────────────────────────────────────────────────────────────────────────


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    """Real cross-connection write lock on a plain background thread — same
    harness as tests/test_db_retry_real_lock.py and
    tests/test_accounts_retry.py."""
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS _lock_probe (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO _lock_probe DEFAULT VALUES")
        lock_acquired.set()
        _time_module.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def tv_client_lockable(monkeypatch, tmp_path) -> AsyncIterator[tuple[AsyncClient, str]]:
    """Same wiring as `tv_client`, but on a dedicated file-backed engine with
    a short `busy_timeout` set BEFORE any connection is opened — required so
    a genuine SQLite lock surfaces quickly within a test's time budget
    instead of being silently absorbed by a long busy_timeout."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")

    db_path = tmp_path / "tv_auth_lock_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_short_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA busy_timeout=50")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.main import app
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", factory)

    async def _override_get_db():
        async with factory() as session:
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
            yield client, str(db_path)
    finally:
        app.dependency_overrides.pop(db_module.get_db, None)
        await engine.dispose()


async def test_start_survives_real_lock(tv_client_lockable):
    """AUDIT-P1-001 (S3.3): POST /api/tv-auth/start (the delicate collision
    + lock-retry endpoint) must succeed despite a genuine SQLite write lock
    held by an independent connection — proving write_with_retry's
    fresh-session-per-attempt genuinely recovers the write, including
    re-running the user_code collision loop from scratch on retry."""
    client, db_path = tv_client_lockable
    hold_seconds = 0.35
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

    resp = await client.post("/api/tv-auth/start", json={"deviceName": "Lock Test TV"})

    blocker.join(timeout=5)
    assert not blocker.is_alive()
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["deviceCode"]) >= 32


async def test_approve_survives_real_lock(tv_client_lockable):
    """AUDIT-P1-001 (S3.3): POST /api/tv-auth/approve must succeed despite a
    genuine SQLite write lock held by an independent connection."""
    client, db_path = tv_client_lockable
    started = await _start(client)

    hold_seconds = 0.35
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

    resp = await client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )

    blocker.join(timeout=5)
    assert not blocker.is_alive()
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


async def test_complete_survives_real_lock(tv_client_lockable):
    """AUDIT-P1-001 (S3.3): POST /api/tv-auth/complete must succeed despite a
    genuine SQLite write lock held by an independent connection."""
    client, db_path = tv_client_lockable
    started = await _start(client)
    await client.post(
        "/api/tv-auth/approve",
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers=AUTH,
    )

    hold_seconds = 0.35
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

    resp = await client.post(
        "/api/tv-auth/complete", json={"deviceCode": started["deviceCode"]}
    )

    blocker.join(timeout=5)
    assert not blocker.is_alive()
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
