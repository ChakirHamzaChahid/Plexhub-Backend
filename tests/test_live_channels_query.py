"""Guard test for CR-P03: GET /api/live/channels' total COUNT must apply the
SAME filters as the page query, computed with a narrow func.count() over the
base table — not a COUNT wrapping a `SELECT *` subquery — and stay correct
and independent of limit/offset.

Follows the seeded-DB + api_client pattern from
tests/test_categories_refresh_camelcase.py (X-API-Key fail-closed auth).
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import LiveChannel
from app.utils.server_id import build_server_id

API_KEY = "test-master-key"
API_HEADERS = {"X-API-Key": API_KEY}


def _channel(stream_id: int, name: str, account_id: str = "a") -> LiveChannel:
    return LiveChannel(
        stream_id=stream_id, server_id=build_server_id(account_id),
        name=name, name_sortable=name.lower(),
        category_id="1", is_in_allowed_categories=True,
        added_at=0,
    )


@pytest_asyncio.fixture
async def seeded_channels(db_engine, monkeypatch):
    """Seed 5 channels (3 matching "Sport") and wire the app onto the
    in-memory test DB, same wiring as test_categories_refresh_camelcase.py."""
    from app.db import database as db_module

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)

    async with factory() as s:
        s.add_all([
            _channel(1, "Sport 1"),
            _channel(2, "Sport 2"),
            _channel(3, "Sport News"),
            _channel(4, "Cinema 1"),
            _channel(5, "Documentary"),
        ])
        await s.commit()

    return factory


async def test_search_count_matches_full_filtered_set_independent_of_limit(
    api_client, seeded_channels,
):
    """3 channels match "Sport"; asking for limit=1 must still report
    total=3 (count independent of the page size) and return exactly 1 item."""
    resp = await api_client.get(
        "/api/live/channels", params={"search": "Sport", "limit": 1},
        headers=API_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["hasMore"] is True


async def test_search_returns_correct_matching_items(api_client, seeded_channels):
    """Full page (limit large enough) must contain exactly the 3 matches,
    none of the unrelated channels."""
    resp = await api_client.get(
        "/api/live/channels", params={"search": "Sport", "limit": 500},
        headers=API_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    names = {item["name"] for item in body["items"]}
    assert names == {"Sport 1", "Sport 2", "Sport News"}
    assert body["hasMore"] is False


async def test_no_search_returns_all_channels(api_client, seeded_channels):
    """Regression guard: no search filter still returns/counts everything."""
    resp = await api_client.get(
        "/api/live/channels", params={"limit": 500}, headers=API_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 5
