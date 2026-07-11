"""Guard test for CR-C02: POST /api/accounts/{id}/categories/refresh must return
a typed camelCase payload (``vodCount``/``seriesCount``), not the old raw dict
with snake_case ``vod_count``/``series_count`` keys.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import XtreamAccount
from app.services.xtream_service import xtream_service


# The JSON API is X-API-Key gated (fail-closed) — same pattern as
# tests/test_adult_classification.py.
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


async def test_refresh_categories_returns_camelcase_counts(
    monkeypatch, api_client, seeded_account,
):
    """The endpoint must expose vodCount/seriesCount (camelCase), never the old
    vod_count/series_count (snake_case)."""

    async def _fake_vod_categories(*args, **kwargs):
        return [
            {"category_id": "1", "category_name": "Action"},
            {"category_id": "2", "category_name": "Comedy"},
        ]

    async def _fake_series_categories(*args, **kwargs):
        return [{"category_id": "10", "category_name": "Drama"}]

    monkeypatch.setattr(xtream_service, "get_vod_categories", _fake_vod_categories)
    monkeypatch.setattr(xtream_service, "get_series_categories", _fake_series_categories)

    resp = await api_client.post(
        "/api/accounts/a/categories/refresh", headers=API_HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()

    # New camelCase contract.
    assert body["vodCount"] == 2
    assert body["seriesCount"] == 1
    assert body["total"] == 3
    assert body["message"] == "Categories refreshed successfully"

    # The old snake_case keys must be gone from the wire (CR-C02).
    assert "vod_count" not in body
    assert "series_count" not in body


async def test_refresh_categories_unknown_account_returns_404(api_client, seeded_account):
    resp = await api_client.post(
        "/api/accounts/does-not-exist/categories/refresh", headers=API_HEADERS,
    )
    assert resp.status_code == 404


async def test_refresh_categories_commits_via_retry_helper(
    monkeypatch, api_client, seeded_account,
):
    """CR-C04: the refresh endpoint's write must go through commit_with_retry
    (lock-retry) instead of a bare db.commit(), same as the workers, so a
    transient 'database is locked' during a concurrent sync/validation is
    retried instead of surfacing as a raw 500. Wraps (doesn't replace) the
    real helper, so behavior is unchanged — only observed via the counter.
    """

    async def _fake_vod_categories(*args, **kwargs):
        return [{"category_id": "1", "category_name": "Action"}]

    async def _fake_series_categories(*args, **kwargs):
        return []

    monkeypatch.setattr(xtream_service, "get_vod_categories", _fake_vod_categories)
    monkeypatch.setattr(xtream_service, "get_series_categories", _fake_series_categories)

    import app.api.categories as categories_module

    calls = {"n": 0}
    real_commit_with_retry = categories_module.commit_with_retry

    async def _spy(db, **kwargs):
        calls["n"] += 1
        return await real_commit_with_retry(db, **kwargs)

    monkeypatch.setattr(categories_module, "commit_with_retry", _spy)

    resp = await api_client.post(
        "/api/accounts/a/categories/refresh", headers=API_HEADERS,
    )

    assert resp.status_code == 200, resp.text
    assert calls["n"] == 1  # the write committed through the lock-retry helper
