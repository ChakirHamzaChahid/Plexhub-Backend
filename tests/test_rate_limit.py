"""Tests for AUDIT-P2-004 / CR-S05 (S4.2): the TARGETED rate limit + pending-
session cap on `POST /api/tv-auth/start`.

Covers the DoD from `docs/plans/2026-07-26-refacto-audit-v1-plan.md` §S4.2:
  - N+1 calls from the same apparent IP -> 429 + Retry-After;
  - the global pending-session cap -> 429 even from a brand-new IP;
  - `GET /api/health` and `/dav` are NEVER rate-limited by this change;
  - a different IP is never penalized by another IP's flood;
  - the window resets (unit-level, on `SlidingWindowLimiter` directly);
  - `GET /api/tv-auth/status` (the device-flow poll) is NEVER limited, even
    right after the /start budget for the same client has been exhausted —
    the one behaviour this step must not break (CLAUDE.md §5.6).

Split into:
  - unit tests on `app.utils.rate_limit` primitives (no HTTP, full control
    over the fake clock via the `now=` parameter);
  - integration tests through real HTTP calls to `/api/tv-auth/start` /
    `/status` / `/api/health` / `/dav`, using a dedicated file-backed engine
    (same pattern as `tests/test_tv_auth.py::tv_client`).
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import Base
from app.utils import rate_limit


pytestmark = pytest.mark.asyncio


# ──────────────────────────────────────────────────────────────────────────────
# Unit tests — SlidingWindowLimiter / check_pending_cap / resolve_client_ip
# ──────────────────────────────────────────────────────────────────────────────


def test_sliding_window_limiter_allows_up_to_max_events():
    limiter = rate_limit.SlidingWindowLimiter()
    for i in range(3):
        limiter.hit("k", max_events=3, window_seconds=60, now=float(i))
    with pytest.raises(rate_limit.RateLimitExceeded):
        limiter.hit("k", max_events=3, window_seconds=60, now=3.0)


def test_sliding_window_limiter_resets_after_window_elapses():
    limiter = rate_limit.SlidingWindowLimiter()
    limiter.hit("k", max_events=1, window_seconds=10, now=0.0)
    with pytest.raises(rate_limit.RateLimitExceeded):
        limiter.hit("k", max_events=1, window_seconds=10, now=5.0)
    # Window fully elapsed -> allowed again, without needing a reset().
    limiter.hit("k", max_events=1, window_seconds=10, now=11.0)


def test_sliding_window_limiter_rejects_without_recording():
    """A rejected hit() must NOT count against the caller's own next
    attempt once the window frees up even slightly."""
    limiter = rate_limit.SlidingWindowLimiter()
    limiter.hit("k", max_events=1, window_seconds=10, now=0.0)
    for attempt_time in (1.0, 2.0, 3.0):
        with pytest.raises(rate_limit.RateLimitExceeded):
            limiter.hit("k", max_events=1, window_seconds=10, now=attempt_time)
    # Still only ONE real event recorded (at t=0) — window frees at t=10.
    limiter.hit("k", max_events=1, window_seconds=10, now=10.0)


def test_sliding_window_limiter_keys_are_independent():
    limiter = rate_limit.SlidingWindowLimiter()
    limiter.hit("ip-a", max_events=1, window_seconds=60, now=0.0)
    with pytest.raises(rate_limit.RateLimitExceeded):
        limiter.hit("ip-a", max_events=1, window_seconds=60, now=1.0)
    # A different key is entirely unaffected.
    limiter.hit("ip-b", max_events=1, window_seconds=60, now=1.0)


def test_sliding_window_limiter_max_events_le_zero_disables_check():
    limiter = rate_limit.SlidingWindowLimiter()
    for i in range(50):
        limiter.hit("k", max_events=0, window_seconds=60, now=float(i))  # never raises


def test_sliding_window_limiter_retry_after_is_positive():
    limiter = rate_limit.SlidingWindowLimiter()
    limiter.hit("k", max_events=1, window_seconds=30, now=0.0)
    try:
        limiter.hit("k", max_events=1, window_seconds=30, now=5.0)
        pytest.fail("expected RateLimitExceeded")
    except rate_limit.RateLimitExceeded as exc:
        assert exc.retry_after_seconds == pytest.approx(25.0, abs=0.01)


def test_check_pending_cap_raises_at_or_above_cap():
    rate_limit.check_pending_cap(4, 5, retry_after_seconds=30)  # below cap: no raise
    with pytest.raises(rate_limit.PendingCapExceeded):
        rate_limit.check_pending_cap(5, 5, retry_after_seconds=30)
    with pytest.raises(rate_limit.PendingCapExceeded):
        rate_limit.check_pending_cap(9, 5, retry_after_seconds=30)


def test_check_pending_cap_le_zero_disables_check():
    rate_limit.check_pending_cap(10_000, 0, retry_after_seconds=30)  # never raises
    rate_limit.check_pending_cap(10_000, -1, retry_after_seconds=30)


def test_resolve_client_ip_prefers_cf_connecting_ip(rf_request_factory):
    request = rf_request_factory(
        headers={"cf-connecting-ip": "203.0.113.1", "x-forwarded-for": "10.0.0.1"}
    )
    assert rate_limit.resolve_client_ip(request) == "203.0.113.1"


def test_resolve_client_ip_falls_back_to_x_forwarded_for(rf_request_factory):
    request = rf_request_factory(headers={"x-forwarded-for": "198.51.100.7, 10.0.0.1"})
    assert rate_limit.resolve_client_ip(request) == "198.51.100.7"


def test_resolve_client_ip_falls_back_to_transport_client(rf_request_factory):
    request = rf_request_factory(headers={}, client=("192.0.2.9", 4444))
    assert rate_limit.resolve_client_ip(request) == "192.0.2.9"


def test_resolve_client_ip_none_when_nothing_available(rf_request_factory):
    request = rf_request_factory(headers={}, client=None)
    assert rate_limit.resolve_client_ip(request) is None


@pytest.fixture
def rf_request_factory():
    """Builds a minimal real `starlette.requests.Request` with a controlled
    `client` tuple and header set — enough for `resolve_client_ip`, without
    spinning up the ASGI app."""
    from starlette.requests import Request as StarletteRequest

    def _make(*, headers: dict[str, str], client: tuple[str, int] | None = ("127.0.0.1", 123)):
        encoded_headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
        ]
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": encoded_headers,
            "client": client,
        }
        return StarletteRequest(scope)

    return _make


# ──────────────────────────────────────────────────────────────────────────────
# Integration tests — real HTTP calls
# ──────────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def rl_engine(tmp_path):
    """File-backed async engine (mirrors tests/test_tv_auth.py::tv_engine —
    avoids the :memory: pooled-connection trap)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'rate_limit_test.db'}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def rl_factory(rl_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(rl_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def rl_client(rl_factory, monkeypatch, tmp_path) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to a per-test file-backed DB (same pattern as
    tests/test_tv_auth.py::tv_client)."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", "secret-test-key")
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")  # derive from AI_API_KEY

    from app.main import app
    from app.db import database as db_module

    # Same reason as tv_client: write_with_retry resolves its own fresh
    # session factory at call time, bypassing FastAPI's Depends(get_db)
    # override — must be monkeypatched directly (CLAUDE.md isolation rule).
    monkeypatch.setattr(db_module, "async_session_factory", rl_factory)

    async def _override_get_db():
        async with rl_factory() as session:
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


async def test_start_flood_from_same_ip_gets_429_with_retry_after(rl_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_MAX", 3)
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_WINDOW_SECONDS", 60)
    headers = {"X-Forwarded-For": "203.0.113.10"}

    for _ in range(3):
        resp = await rl_client.post("/api/tv-auth/start", json={}, headers=headers)
        assert resp.status_code == 201, resp.text

    resp = await rl_client.post("/api/tv-auth/start", json={}, headers=headers)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


async def test_different_ip_not_penalized_by_another_ips_flood(rl_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_MAX", 2)
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_WINDOW_SECONDS", 60)
    flooder = {"X-Forwarded-For": "203.0.113.20"}
    innocent = {"X-Forwarded-For": "203.0.113.21"}

    for _ in range(2):
        resp = await rl_client.post("/api/tv-auth/start", json={}, headers=flooder)
        assert resp.status_code == 201

    resp = await rl_client.post("/api/tv-auth/start", json={}, headers=flooder)
    assert resp.status_code == 429

    # The innocent IP is completely unaffected by the flooder's budget.
    resp = await rl_client.post("/api/tv-auth/start", json={}, headers=innocent)
    assert resp.status_code == 201


async def test_global_pending_cap_blocks_even_from_a_fresh_ip(rl_client, monkeypatch):
    # Disable the per-IP layers so only the GLOBAL, DB-authoritative cap is
    # exercised — a fresh IP for every call proves this guard doesn't depend
    # on client identification at all.
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_MAX", 0)
    monkeypatch.setattr(settings, "TV_AUTH_PENDING_SESSIONS_CAP_PER_IP", 0)
    monkeypatch.setattr(settings, "TV_AUTH_PENDING_SESSIONS_CAP", 2)

    for i in range(2):
        resp = await rl_client.post(
            "/api/tv-auth/start", json={}, headers={"X-Forwarded-For": f"198.51.100.{i}"}
        )
        assert resp.status_code == 201, resp.text

    resp = await rl_client.post(
        "/api/tv-auth/start", json={}, headers={"X-Forwarded-For": "198.51.100.99"}
    )
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


async def test_pending_cap_disabled_when_le_zero(rl_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_MAX", 0)
    monkeypatch.setattr(settings, "TV_AUTH_PENDING_SESSIONS_CAP_PER_IP", 0)
    monkeypatch.setattr(settings, "TV_AUTH_PENDING_SESSIONS_CAP", 0)

    for i in range(5):
        resp = await rl_client.post(
            "/api/tv-auth/start", json={}, headers={"X-Forwarded-For": f"198.51.100.{i}"}
        )
        assert resp.status_code == 201


async def test_health_never_rate_limited(rl_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_MAX", 1)
    monkeypatch.setattr(settings, "TV_AUTH_PENDING_SESSIONS_CAP_PER_IP", 1)
    monkeypatch.setattr(settings, "TV_AUTH_PENDING_SESSIONS_CAP", 1)

    for _ in range(10):
        resp = await rl_client.get("/api/health")
        assert resp.status_code == 200


async def test_dav_never_rate_limited(rl_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_MAX", 1)
    monkeypatch.setattr(settings, "TV_AUTH_PENDING_SESSIONS_CAP_PER_IP", 1)
    monkeypatch.setattr(settings, "TV_AUTH_PENDING_SESSIONS_CAP", 1)

    # DAV_ENABLED defaults false -> 503 fail-closed either way; the point of
    # this test is that it is NEVER 429, i.e. this feature never wired the
    # limiter into /dav at all.
    for _ in range(10):
        resp = await rl_client.get("/dav/")
        assert resp.status_code != 429


async def test_status_poll_never_rate_limited_even_after_start_flood(rl_client, monkeypatch):
    """The device-flow poll must keep working even once /start's own budget
    for the SAME client is fully exhausted — this is the one regression this
    step must never introduce (CLAUDE.md §5.6)."""
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_MAX", 1)
    monkeypatch.setattr(settings, "TV_AUTH_START_RATE_LIMIT_WINDOW_SECONDS", 60)

    resp = await rl_client.post("/api/tv-auth/start", json={})
    assert resp.status_code == 201, resp.text
    device_code = resp.json()["deviceCode"]

    # Exhaust this client's /start budget.
    resp = await rl_client.post("/api/tv-auth/start", json={})
    assert resp.status_code == 429

    # Polling /status from the SAME apparent client is entirely unaffected —
    # the TV keeps polling in a loop while waiting to be approved.
    for _ in range(20):
        status_resp = await rl_client.get(
            "/api/tv-auth/status", params={"deviceCode": device_code}
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "pending"
