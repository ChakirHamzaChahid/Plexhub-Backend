"""Unit tests for app.services.category_service.refresh_categories_from_provider
(CR-A01 extraction).

Exercises the extracted service function directly (no HTTP layer), proving
the moved logic (account lookup, Xtream VOD/series category fetch, upsert
loop) behaves the same in isolation as it did inline in
app/api/categories.py::refresh_categories. Endpoint-level coverage
(camelCase contract, 404, commit-with-retry) lives in
tests/test_categories_refresh_camelcase.py.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.database import XtreamAccount, XtreamCategory
from app.services import category_service
from app.services.xtream_service import xtream_service


def _account(id_: str = "a") -> XtreamAccount:
    return XtreamAccount(
        id=id_, label="Compte", base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


async def test_refresh_categories_unknown_account_raises(db_session):
    with pytest.raises(category_service.AccountNotFoundError):
        await category_service.refresh_categories_from_provider(db_session, "nope")


async def test_refresh_categories_upserts_and_returns_counts(monkeypatch, db_session):
    db_session.add(_account("a"))
    await db_session.flush()

    async def _fake_vod_categories(*args, **kwargs):
        return [
            {"category_id": "1", "category_name": "Action"},
            {"category_id": "2", "category_name": "Comedy"},
        ]

    async def _fake_series_categories(*args, **kwargs):
        return [{"category_id": "10", "category_name": "Drama"}]

    monkeypatch.setattr(xtream_service, "get_vod_categories", _fake_vod_categories)
    monkeypatch.setattr(xtream_service, "get_series_categories", _fake_series_categories)

    vod_count, series_count = await category_service.refresh_categories_from_provider(
        db_session, "a",
    )
    assert (vod_count, series_count) == (2, 1)

    await db_session.flush()
    rows = (await db_session.execute(select(XtreamCategory))).scalars().all()
    by_key = {(r.category_id, r.category_type): r for r in rows}
    assert by_key[("1", "vod")].category_name == "Action"
    assert by_key[("2", "vod")].category_name == "Comedy"
    assert by_key[("10", "series")].category_name == "Drama"
    assert all(r.is_allowed for r in rows)  # default-allowed on first insert


async def test_refresh_categories_preserves_existing_is_allowed(monkeypatch, db_session):
    """Re-running refresh must NOT reset a category a user explicitly
    disabled (upsert_category only sets is_allowed on first insert)."""
    db_session.add(_account("a"))
    db_session.add(XtreamCategory(
        account_id="a", category_id="1", category_type="vod",
        category_name="Action (old name)", is_allowed=False, last_fetched_at=0,
    ))
    await db_session.flush()

    async def _fake_vod_categories(*args, **kwargs):
        return [{"category_id": "1", "category_name": "Action"}]

    async def _fake_series_categories(*args, **kwargs):
        return []

    monkeypatch.setattr(xtream_service, "get_vod_categories", _fake_vod_categories)
    monkeypatch.setattr(xtream_service, "get_series_categories", _fake_series_categories)

    await category_service.refresh_categories_from_provider(db_session, "a")
    await db_session.flush()

    row = (await db_session.execute(select(XtreamCategory))).scalars().one()
    assert row.category_name == "Action"  # name refreshed
    assert row.is_allowed is False  # prior explicit choice preserved
