"""Unit tests for app.services.live_service (CR-A01 extraction).

Exercises the extracted EPG-ingest service function directly (no HTTP
layer), proving the moved logic (server_id parsing via the shared
app.utils.server_id helper, Xtream fetch, base64/timestamp parsing,
EpgEntry construction) behaves the same in isolation as it did inline in
app/api/live.py::get_channel_epg. Endpoint-level coverage lives in
tests/test_live_channels_query.py.
"""
from __future__ import annotations

import pytest

from app.models.database import XtreamAccount
from app.services import live_service
from app.services.xtream_service import xtream_service
from app.utils.server_id import build_server_id


def _account(id_: str = "a") -> XtreamAccount:
    return XtreamAccount(
        id=id_, label="Compte", base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


async def test_ingest_short_epg_invalid_server_id_raises(db_session):
    with pytest.raises(live_service.InvalidServerIdError):
        await live_service.ingest_short_epg(db_session, "not-xtream", 1, fetched_at=0)


async def test_ingest_short_epg_unknown_account_raises(db_session):
    server_id = build_server_id("does-not-exist")
    with pytest.raises(live_service.AccountNotFoundError):
        await live_service.ingest_short_epg(db_session, server_id, 1, fetched_at=0)


async def test_ingest_short_epg_provider_failure_degrades_to_empty_list(
    monkeypatch, db_session,
):
    """Mirrors the previous inline behavior: a provider exception logs a
    warning and returns an empty list rather than propagating a 500."""
    db_session.add(_account("a"))
    await db_session.flush()

    async def _boom(account, stream_id=None):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(xtream_service, "get_short_epg", _boom)

    entries = await live_service.ingest_short_epg(
        db_session, build_server_id("a"), 42, fetched_at=1000,
    )
    assert entries == []


async def test_ingest_short_epg_parses_and_stages_entries(monkeypatch, db_session):
    db_session.add(_account("a"))
    await db_session.flush()

    async def _fake_get_short_epg(account, stream_id=None):
        return {
            "epg_listings": [
                {
                    "epg_id": "chan1",
                    "title": "Evening News",  # not base64 — kept as-is
                    "description": "",
                    "start_timestamp": "1000",
                    "stop_timestamp": "2000",
                    "lang": "en",
                },
                # No usable start timestamp — must be dropped.
                {"epg_id": "chan1", "title": "Dropped", "start_timestamp": "0"},
                # Not a dict — must be skipped defensively.
                "garbage",
            ],
        }

    monkeypatch.setattr(xtream_service, "get_short_epg", _fake_get_short_epg)

    server_id = build_server_id("a")
    entries = await live_service.ingest_short_epg(
        db_session, server_id, 42, fetched_at=9999,
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.server_id == server_id
    assert entry.stream_id == 42
    assert entry.title == "Evening News"
    assert entry.start_time == 1000 * 1000
    assert entry.end_time == 2000 * 1000
    assert entry.fetched_at == 9999
    assert entry.lang == "en"

    # Staged on the session (caller commits) — visible via a flush + query.
    from sqlalchemy import select
    from app.models.database import EpgEntry

    await db_session.flush()
    rows = (await db_session.execute(select(EpgEntry))).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Evening News"


def test_try_base64_decode_reexported_for_back_compat():
    """tests/test_utilities.py imports this from its new home."""
    assert live_service._try_base64_decode("Evening News") == "Evening News"
