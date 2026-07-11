"""Regression net for the fail-closed X-API-Key guard (CR-T02, P1).

`app/main.py:396-405` wraps the entire JSON API (accounts/categories/live/
media/stream/sync/plex) with a shared guard:

    _guard = [Depends(verify_backend_secret)]
    app.include_router(accounts.router, prefix="/api", dependencies=_guard)
    ...

`verify_backend_secret` (`app/api/deps.py:59`) accepts EITHER the master
secret `settings.AI_API_KEY` OR an active per-user key from the `api_keys`
table, and raises 401 otherwise (fail-closed).

Before this file there were ZERO tests asserting that guard actually rejects
unauthenticated requests (`grep verify_backend_secret tests/` = 0) — if
`dependencies=_guard` were ever dropped from one of those `include_router`
calls, the corresponding router would silently open up and CI would stay
green. This file is that regression net: for a representative endpoint on
each guarded router it asserts 401 with no key, 401 with a wrong key, and
"auth passed" (not 401) with the configured master key. It also asserts the
public `/api/health` endpoint stays open with no key at all.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.db import database as db_module


pytestmark = pytest.mark.asyncio

MASTER_KEY = "test-secret"

# One representative GET endpoint per guarded router (`app/main.py:399-405`).
# A wrong/absent key must 401 *before* the handler runs, so the `{id}`/`{key}`
# placeholders below never need to resolve to a real row — for the master-key
# positive case they simply reach the handler and return a non-401 (e.g. 404).
GUARDED_GET_ENDPOINTS = [
    "/api/accounts",                            # accounts.router (prefix /accounts)
    "/api/accounts/no-such-account/categories", # categories.router (prefix /accounts)
    "/api/media/movies",                        # media.router (prefix /media)
    "/api/live/channels",                       # live.router (prefix /live)
    "/api/stream/no-such-rating-key",           # stream.router (prefix /api, no sub-prefix)
    "/api/sync/jobs",                           # sync.router (prefix /sync) — in-memory tracker
]


@pytest.fixture(autouse=True)
def _configure_master_key(monkeypatch):
    """Give verify_backend_secret a deterministic master secret to compare
    against, instead of whatever (possibly empty) AI_API_KEY is in the real
    environment."""
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER_KEY)


@pytest.fixture(autouse=True)
def _wire_test_db(monkeypatch, db_factory):
    """Point `get_db` (used by accounts/media/live handlers) at the isolated
    in-memory engine from conftest (`db_engine`/`db_factory`, all tables
    created) instead of the real on-disk database — same pattern as
    `tests/test_api_health.py`."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)


@pytest.mark.parametrize("path", GUARDED_GET_ENDPOINTS)
async def test_guarded_endpoint_401_without_key(api_client, path):
    """No X-API-Key header at all -> 401 (fail-closed)."""
    resp = await api_client.get(path)
    assert resp.status_code == 401, f"{path} should reject a request with no X-API-Key"


@pytest.mark.parametrize("path", GUARDED_GET_ENDPOINTS)
async def test_guarded_endpoint_401_with_wrong_key(api_client, path):
    """A present but incorrect X-API-Key -> 401."""
    resp = await api_client.get(path, headers={"X-API-Key": "definitely-wrong"})
    assert resp.status_code == 401, f"{path} should reject a wrong X-API-Key"


@pytest.mark.parametrize("path", GUARDED_GET_ENDPOINTS)
async def test_guarded_endpoint_not_401_with_master_key(api_client, path):
    """The configured master secret authenticates -> never 401."""
    resp = await api_client.get(path, headers={"X-API-Key": MASTER_KEY})
    assert resp.status_code != 401, (
        f"{path} should authenticate with the master X-API-Key (got {resp.status_code})"
    )


# ─── POST /api/plex/generate — separate because it's a POST with a body ────


async def test_plex_generate_401_without_key(api_client):
    resp = await api_client.post("/api/plex/generate", json={})
    assert resp.status_code == 401


async def test_plex_generate_401_with_wrong_key(api_client):
    resp = await api_client.post(
        "/api/plex/generate", json={}, headers={"X-API-Key": "definitely-wrong"},
    )
    assert resp.status_code == 401


async def test_plex_generate_not_401_with_master_key(api_client, monkeypatch):
    """With the master key, auth passes and the request reaches the handler —
    it may still 400 (no outputDir/PLEX_LIBRARY_DIR configured in this test),
    but it must never be 401. PLEX_LIBRARY_DIR is explicitly cleared so the
    outcome doesn't depend on the ambient environment."""
    monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", "")
    resp = await api_client.post(
        "/api/plex/generate", json={}, headers={"X-API-Key": MASTER_KEY},
    )
    assert resp.status_code != 401


# ─── Public endpoint stays public ───────────────────────────────────────────


async def test_health_stays_public_no_key_required(api_client):
    """GET /api/health is intentionally NOT part of `_guard` (monitoring) —
    it must keep responding 200 with no X-API-Key at all."""
    resp = await api_client.get("/api/health")
    assert resp.status_code == 200
