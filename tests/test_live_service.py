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


# ─── Lot 10 sub-lot D (FR-BE-554): ingest_short_epg is an UPSERT ────────────


async def _epg_rows(session):
    from sqlalchemy import select

    from app.models.database import EpgEntry

    return (await session.execute(
        select(EpgEntry).order_by(EpgEntry.start_time)
    )).scalars().all()


def _listing(start: str, stop: str, title: str, **extra) -> dict:
    return {
        "epg_id": "chan1", "title": title, "description": "",
        "start_timestamp": start, "stop_timestamp": stop, "lang": "fr", **extra,
    }


async def test_ingest_short_epg_twice_is_idempotent_and_updates_fields(
    monkeypatch, db_session,
):
    db_session.add(_account("a"))
    await db_session.flush()
    server_id = build_server_id("a")

    payload = {"epg_listings": [_listing("1000", "2000", "Old title")]}

    async def _fake(account, stream_id=None):
        return payload

    monkeypatch.setattr(xtream_service, "get_short_epg", _fake)

    first = await live_service.ingest_short_epg(db_session, server_id, 42, fetched_at=100)
    await db_session.commit()
    assert [e.title for e in first] == ["Old title"]

    # Same (server, stream, start) re-delivered with a corrected title/end.
    payload["epg_listings"] = [_listing("1000", "2500", "New title")]
    second = await live_service.ingest_short_epg(db_session, server_id, 42, fetched_at=200)
    await db_session.commit()

    rows = await _epg_rows(db_session)
    assert len(rows) == 1, "a re-ingest must update in place, never duplicate"
    assert rows[0].title == "New title"
    assert rows[0].end_time == 2500 * 1000
    assert rows[0].fetched_at == 200
    # The returned entries are the persisted rows (same id as the original).
    assert [e.id for e in second] == [rows[0].id] == [first[0].id]


async def test_ingest_short_epg_repeated_start_in_one_payload_keeps_one_row(
    monkeypatch, db_session,
):
    db_session.add(_account("a"))
    await db_session.flush()

    async def _fake(account, stream_id=None):
        return {"epg_listings": [
            _listing("1000", "2000", "first"),
            _listing("1000", "2100", "last wins"),
            _listing("3000", "4000", "other"),
        ]}

    monkeypatch.setattr(xtream_service, "get_short_epg", _fake)
    entries = await live_service.ingest_short_epg(
        db_session, build_server_id("a"), 42, fetched_at=1,
    )
    await db_session.commit()

    assert [e.title for e in entries] == ["last wins", "other"]
    assert len(await _epg_rows(db_session)) == 2


async def test_ingest_short_epg_all_listings_invalid_returns_empty(monkeypatch, db_session):
    db_session.add(_account("a"))
    await db_session.flush()

    async def _fake(account, stream_id=None):
        return {"epg_listings": [{"title": "no start"}, "garbage"]}

    monkeypatch.setattr(xtream_service, "get_short_epg", _fake)
    assert await live_service.ingest_short_epg(
        db_session, build_server_id("a"), 42, fetched_at=1,
    ) == []
    assert await _epg_rows(db_session) == []


async def test_two_concurrent_cache_misses_converge_on_one_row_per_programme(
    monkeypatch, tmp_path,
):
    """FR-BE-554: two requests miss the cache for the same channel at the same
    time, both fetch, both write. Before the upsert this was an IntegrityError
    (or a duplicate on a DB without the unique index); now both commit and the
    table holds exactly one row per programme."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.models.database import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add(_account("a"))
            await s.commit()

        barrier = asyncio.Barrier(2)

        async def _fake(account, stream_id=None):
            await barrier.wait()  # both requests are past their cache miss
            return {"epg_listings": [
                _listing("1000", "2000", "Prog 1"),
                _listing("2000", "3000", "Prog 2"),
            ]}

        monkeypatch.setattr(xtream_service, "get_short_epg", _fake)
        server_id = build_server_id("a")

        async def _one_request(fetched_at: int):
            async with factory() as session:
                entries = await live_service.ingest_short_epg(
                    session, server_id, 42, fetched_at=fetched_at,
                )
                await session.commit()
                return entries

        results = await asyncio.gather(_one_request(1), _one_request(2))
        assert all(len(r) == 2 for r in results)

        async with factory() as s:
            rows = await _epg_rows(s)
        assert [r.title for r in rows] == ["Prog 1", "Prog 2"]
    finally:
        await engine.dispose()
