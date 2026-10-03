"""Lot 10 sub-lot D: the bulk EPG writer under REAL SQLite write contention.

Same method as tests/test_worker_write_retry_real_lock.py: a file-backed WAL
database and a genuine `BEGIN IMMEDIATE` held by a second connection, not a
synthetic `OperationalError` (CLAUDE.md piège 8: "tout nouveau writer critique
doit être testé sous vrai lock").
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import Base, EpgEntry, LiveChannel, XtreamAccount
from app.services import epg_sync_service as svc
from app.services.epg_sync_service import run_epg_sync
from app.services.xtream_service import xtream_service
from app.utils.db_retry import write_with_retry
from tests._records import Records

_HOLD_SECONDS = 0.35
NOW_MS = (int(time.time()) // 60) * 60 * 1000
MIN = 60_000


def _wal_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


async def _create_schema(db_path: str) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _engine(db_path: str, busy_timeout_ms: int):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _busy(dbapi_conn, _record):
        dbapi_conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

    return engine


def _hold_write_lock(db_path: str, acquired: threading.Event, hold: float) -> None:
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO xtream_accounts (id, label, base_url, port, username, password, "
            "status, max_connections, allowed_formats, last_synced_at, is_active, "
            "created_at, category_filter_mode) VALUES ('blocker', 'blocker', "
            "'http://blocker', 80, 'u', 'p', 'Unknown', 1, '', 0, 0, 0, 'all')"
        )
        acquired.set()
        time.sleep(hold)
        conn.commit()
    finally:
        conn.close()


def _xml(n_programmes: int, channel="ch1") -> str:
    from datetime import datetime, timezone

    def f(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")

    progs = "".join(
        f'<programme start="{f(NOW_MS + i * MIN)}" stop="{f(NOW_MS + (i + 1) * MIN)}" '
        f'channel="{channel}"><title>P{i}</title></programme>'
        for i in range(n_programmes)
    )
    return f'<?xml version="1.0"?><tv>{progs}</tv>'


def _fake_download(monkeypatch, xml: str):
    async def _fake(acc, dest, *, max_bytes, total_timeout, **_):
        data = xml.encode()
        with open(dest, "wb") as fh:
            fh.write(data)
        return len(data)

    monkeypatch.setattr(xtream_service, "download_xmltv", _fake)


async def _seed(factory):
    async with factory() as s:
        s.add(XtreamAccount(
            id="a", label="A", base_url="http://a.example", port=80,
            username="u", password="p", is_active=True, created_at=0,
        ))
        s.add(LiveChannel(
            stream_id=10, server_id="xtream_a", name="C", name_sortable="c",
            epg_channel_id="ch1", category_id="1", added_at=0,
        ))
        await s.commit()


async def test_bulk_ingest_survives_a_real_held_write_lock(tmp_path, monkeypatch):
    """A writer holds the SQLite write lock for 350 ms while the sync runs.
    With a 50 ms busy_timeout the first batch gets a genuine `database is
    locked`: `write_with_retry` (fresh session per attempt) must carry it
    through, with no row lost and no error surfaced."""
    db_path = str(tmp_path / "epg_lock.db")
    _wal_schema(db_path)
    await _create_schema(db_path)
    engine = _engine(db_path, busy_timeout_ms=50)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await _seed(factory)
        monkeypatch.setattr(svc, "now_ms", lambda: NOW_MS)
        monkeypatch.setattr(settings, "EPG_SYNC_BATCH_SIZE", 50)
        _fake_download(monkeypatch, _xml(200))

        acquired = threading.Event()
        blocker = threading.Thread(
            target=_hold_write_lock, args=(db_path, acquired, _HOLD_SECONDS), daemon=True,
        )
        blocker.start()
        assert acquired.wait(timeout=5)

        with Records("plexhub.db.retry", 30) as logs:
            report = await run_epg_sync(factory)

        blocker.join(timeout=5)
        assert not blocker.is_alive()
        acc = report.accounts[0]
        assert acc.status == "ok", acc.error
        assert acc.inserted == 200
        assert "epg_sync.batch hit 'database is locked'" in logs.text, (
            "the test must have really collided with the held lock"
        )
        async with factory() as s:
            assert (await s.execute(select(func.count()).select_from(EpgEntry))).scalar() == 200
    finally:
        await engine.dispose()


async def test_concurrent_writer_is_not_starved_by_the_ingestion(tmp_path, monkeypatch):
    """While the sync ingests thousands of rows in short batches, an unrelated
    writer (the API / the pipeline in production) keeps getting the write lock:
    every one of its writes completes quickly, and none fails."""
    db_path = str(tmp_path / "epg_fair.db")
    _wal_schema(db_path)
    await _create_schema(db_path)
    sync_engine = _engine(db_path, busy_timeout_ms=5000)
    other_engine = _engine(db_path, busy_timeout_ms=5000)
    sync_factory = async_sessionmaker(sync_engine, class_=AsyncSession, expire_on_commit=False)
    other_factory = async_sessionmaker(other_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await _seed(sync_factory)
        monkeypatch.setattr(svc, "now_ms", lambda: NOW_MS)
        monkeypatch.setattr(settings, "EPG_SYNC_BATCH_SIZE", 100)
        monkeypatch.setattr(settings, "EPG_SYNC_FUTURE_HOURS", 100)
        _fake_download(monkeypatch, _xml(3000))

        latencies: list[float] = []
        errors: list[BaseException] = []
        stop = asyncio.Event()

        async def other_writer():
            n = 0
            while not stop.is_set():
                n += 1

                async def work(session, n=n):
                    await session.execute(
                        update(XtreamAccount).where(XtreamAccount.id == "a")
                        .values(label=f"w{n}")
                    )
                    await session.commit()

                t0 = time.monotonic()
                try:
                    await write_with_retry(work, session_factory=other_factory, op="other_writer")
                except BaseException as exc:  # noqa: BLE001 — the test asserts none happen
                    errors.append(exc)
                latencies.append(time.monotonic() - t0)
                await asyncio.sleep(0.005)

        writer_task = asyncio.create_task(other_writer())
        report = await run_epg_sync(sync_factory)
        stop.set()
        await writer_task

        assert report.accounts[0].status == "ok", report.accounts[0].error
        assert report.accounts[0].inserted == 3000
        assert errors == [], errors
        assert len(latencies) >= 5, "the concurrent writer must have run during the sync"
        # 30 short batches: a competing write never waits for the whole account.
        assert max(latencies) < 3.0, f"writer starved: worst latency {max(latencies):.2f}s"
    finally:
        await sync_engine.dispose()
        await other_engine.dispose()
