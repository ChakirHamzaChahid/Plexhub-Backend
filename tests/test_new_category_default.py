"""A category the provider has just introduced must not default to allowed.

Bug: both insertion paths hard-coded ``is_allowed=True``, so in whitelist mode
a brand-new (typically non-FR) category was fetched at the next sync and its
media surfaced. Only blacklist mode ("everything except what I block") keeps
new categories allowed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.database import XtreamAccount, XtreamCategory
from app.services import category_service
from app.services.xtream_service import xtream_service
from app.workers import sync_worker


def _account(id_: str, mode: str) -> XtreamAccount:
    return XtreamAccount(
        id=id_, label="Compte", base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
        category_filter_mode=mode,
    )


async def _flags(db_session) -> dict[tuple[str, str], bool]:
    rows = (await db_session.execute(select(XtreamCategory))).scalars().all()
    return {(r.category_id, r.category_type): r.is_allowed for r in rows}


@pytest.mark.parametrize(
    "mode,expected",
    [("whitelist", False), ("all", False), ("blacklist", True), (None, False)],
)
def test_default_is_allowed_for_new_category(mode, expected):
    assert category_service.default_is_allowed_for_new_category(mode) is expected


@pytest.mark.parametrize(
    "mode,expected",
    [("whitelist", False), ("all", False), ("blacklist", True)],
)
async def test_refresh_from_provider_new_category_follows_mode(
    monkeypatch, db_session, mode, expected,
):
    db_session.add(_account("a", mode))
    db_session.add(XtreamCategory(
        account_id="a", category_id="1", category_type="vod",
        category_name="Known", is_allowed=True, last_fetched_at=0,
    ))
    await db_session.flush()

    async def _vod(*args, **kwargs):
        return [
            {"category_id": "1", "category_name": "Known"},
            {"category_id": "2", "category_name": "New"},
        ]

    async def _series(*args, **kwargs):
        return [{"category_id": "10", "category_name": "NewSeries"}]

    monkeypatch.setattr(xtream_service, "get_vod_categories", _vod)
    monkeypatch.setattr(xtream_service, "get_series_categories", _series)

    await category_service.refresh_categories_from_provider(db_session, "a")
    await db_session.flush()

    flags = await _flags(db_session)
    assert flags[("1", "vod")] is True  # known category untouched
    assert flags[("2", "vod")] is expected
    assert flags[("10", "series")] is expected


@pytest.mark.parametrize(
    "mode,expected",
    [("whitelist", False), ("all", False), ("blacklist", True)],
)
async def test_sync_refresh_categories_new_category_follows_mode(
    monkeypatch, db_session, mode, expected,
):
    account = _account("a", mode)
    db_session.add(account)
    db_session.add(XtreamCategory(
        account_id="a", category_id="1", category_type="vod",
        category_name="Old name", is_allowed=True, last_fetched_at=0,
    ))
    db_session.add(XtreamCategory(
        account_id="a", category_id="3", category_type="live",
        category_name="Blocked live", is_allowed=False, last_fetched_at=0,
    ))
    await db_session.flush()

    async def _vod(*args, **kwargs):
        return [
            {"category_id": "1", "category_name": "Known"},
            {"category_id": "2", "category_name": "New"},
        ]

    async def _series(*args, **kwargs):
        return [{"category_id": "10", "category_name": "NewSeries"}]

    async def _live(*args, **kwargs):
        return [
            {"category_id": "3", "category_name": "Blocked live"},
            {"category_id": "4", "category_name": "NewLive"},
        ]

    monkeypatch.setattr(xtream_service, "get_vod_categories", _vod)
    monkeypatch.setattr(xtream_service, "get_series_categories", _series)
    monkeypatch.setattr(xtream_service, "get_live_categories", _live)

    await sync_worker._refresh_categories(db_session, account, "a")

    flags = await _flags(db_session)
    assert flags[("1", "vod")] is True  # prior flag preserved on conflict
    assert flags[("3", "live")] is False  # explicit block preserved
    assert flags[("2", "vod")] is expected
    assert flags[("10", "series")] is expected
    assert flags[("4", "live")] is expected
    names = {
        r.category_id: r.category_name
        for r in (await db_session.execute(select(XtreamCategory))).scalars()
        if r.category_type == "vod"
    }
    assert names["1"] == "Known"  # name still refreshed


async def test_sync_refresh_categories_account_without_mode_attr(monkeypatch, db_session):
    """Defensive: an account-like object lacking the attribute -> 'all' -> False."""
    async def _vod(*args, **kwargs):
        return [{"category_id": "2", "category_name": "New"}]

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(xtream_service, "get_vod_categories", _vod)
    monkeypatch.setattr(xtream_service, "get_series_categories", _empty)
    monkeypatch.setattr(xtream_service, "get_live_categories", _empty)

    await sync_worker._refresh_categories(db_session, SimpleNamespace(), "a")

    assert (await _flags(db_session))[("2", "vod")] is False
