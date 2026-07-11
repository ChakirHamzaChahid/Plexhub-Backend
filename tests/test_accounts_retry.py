"""Guard tests for CR-C04: request-path writes in app/api/accounts.py commit
via commit_with_retry (lock-retry) instead of relying on get_db's un-retried
implicit commit (db/database.py get_db commits on successful yield, but does
NOT retry on 'database is locked').

update_account/delete_account previously had no explicit commit at all and
depended on that implicit commit — this suite locks in that they now commit
explicitly through the shared retry helper, same as the workers.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import XtreamAccount

# The JSON API is X-API-Key gated (fail-closed) — same pattern as
# tests/test_categories_refresh_camelcase.py.
API_KEY = "test-master-key"
API_HEADERS = {"X-API-Key": API_KEY}


def _account(id_: str = "a") -> XtreamAccount:
    return XtreamAccount(
        id=id_, label="Compte", base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


@pytest_asyncio.fixture
async def seeded_account(db_engine, monkeypatch):
    """Seed one active account and wire the app onto the in-memory test DB."""
    from app.db import database as db_module

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)

    async with factory() as s:
        s.add(_account("a"))
        await s.commit()

    return factory


async def test_update_account_commits_via_retry_helper(
    monkeypatch, api_client, seeded_account,
):
    """CR-C04: PUT /api/accounts/{id} must commit explicitly via
    commit_with_retry (it used to rely solely on get_db's implicit,
    un-retried commit)."""
    import app.api.accounts as accounts_module

    calls = {"n": 0}
    real_commit_with_retry = accounts_module.commit_with_retry

    async def _spy(db, **kwargs):
        calls["n"] += 1
        return await real_commit_with_retry(db, **kwargs)

    monkeypatch.setattr(accounts_module, "commit_with_retry", _spy)

    resp = await api_client.put(
        "/api/accounts/a", json={"label": "Updated Label"}, headers=API_HEADERS,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["label"] == "Updated Label"
    assert calls["n"] == 1  # the write committed through the lock-retry helper


async def test_delete_account_commits_via_retry_helper(
    monkeypatch, api_client, seeded_account,
):
    """CR-C04: DELETE /api/accounts/{id} (multi-table cascade) must commit
    explicitly via commit_with_retry (it used to rely solely on get_db's
    implicit, un-retried commit)."""
    import app.api.accounts as accounts_module

    calls = {"n": 0}
    real_commit_with_retry = accounts_module.commit_with_retry

    async def _spy(db, **kwargs):
        calls["n"] += 1
        return await real_commit_with_retry(db, **kwargs)

    monkeypatch.setattr(accounts_module, "commit_with_retry", _spy)

    resp = await api_client.delete("/api/accounts/a", headers=API_HEADERS)

    assert resp.status_code == 204, resp.text
    assert calls["n"] == 1  # the write committed through the lock-retry helper
