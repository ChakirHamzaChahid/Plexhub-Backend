"""B2 — l'écriture de l'API metadata sous un VRAI verrou SQLite (fichier WAL, pool réel,
`timeout=60` comme en production), pas une exception synthétique (CLAUDE.md piège 8 ;
ADR 0011 § D2.4/D4) :

- un verrou d'écriture tenu par ailleurs au-delà de l'échéance => 503 `database_busy`, rien écrit ;
- deux écritures concurrentes sur la même révision => une 200, l'autre 412 ;
- deux requêtes concurrentes avec la même `Idempotency-Key` => une seule édition, l'autre rejoue.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db import database as db_module
from app.models.database import Base, Media, MetadataEdit
from app.services import api_key_service, metadata_limits
from tests._metadata_support import BASE, MASTER, SERVER, h, write_headers

URL = f"{BASE}/items/{SERVER}/vod_1.mkv"


@pytest_asyncio.fixture
async def file_db(tmp_path, monkeypatch):
    db_path = tmp_path / "real.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 60},
    )
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", factory)
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER)
    async with factory() as db:
        db.add(Media(
            rating_key="vod_1.mkv", server_id=SERVER, filter="all", sort_order="default",
            library_section_id="xtream_vod", title="The Matrix", title_sortable="the matrix",
            page_offset=1, type="movie", year=1999, summary="old", genres="Action",
            unification_id="title_the_matrix_1999", history_group_key="title_the_matrix_1999",
        ))
        await db.commit()
    yield factory, str(db_path)
    await engine.dispose()


def _hold_write_lock(db_path: str, acquired: threading.Event, seconds: float) -> None:
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        acquired.set()
        time.sleep(seconds)
        conn.rollback()
    finally:
        conn.close()


async def _revision(client) -> str:
    return (await client.get(URL, headers=h(MASTER))).json()["revision"]


async def _summary(factory) -> str:
    async with factory() as db:
        return (await db.execute(select(Media.summary))).scalar_one()


async def _edits(factory) -> int:
    async with factory() as db:
        return (await db.execute(select(func.count()).select_from(MetadataEdit))).scalar_one()


async def test_a_lock_held_past_the_deadline_is_a_503_and_nothing_is_written(api_client, file_db, monkeypatch):
    factory, db_path = file_db
    monkeypatch.setattr(metadata_limits, "request_timeout_seconds", lambda: 0.5)
    revision = await _revision(api_client)
    acquired = threading.Event()
    blocker = threading.Thread(target=_hold_write_lock, args=(db_path, acquired, 3.0), daemon=True)
    blocker.start()
    assert acquired.wait(5)

    started = time.monotonic()
    resp = await api_client.patch(
        URL, json={"fields": {"summary": "new"}}, headers=write_headers(MASTER, revision, "k-1"),
    )
    elapsed = time.monotonic() - started
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "database_busy"
    assert int(resp.headers["Retry-After"]) == resp.json()["detail"]["retryAfterSeconds"]
    assert elapsed < 2.5, elapsed                      # the 0.5 s deadline, not the 60 s pool timeout
    blocker.join(timeout=6)
    assert await _summary(factory) == "old" and await _edits(factory) == 0

    # the client retries after the lock is gone: the same Idempotency-Key was NOT recorded
    retry = await api_client.patch(
        URL, json={"fields": {"summary": "new"}}, headers=write_headers(MASTER, revision, "k-1"),
    )
    assert retry.status_code == 200 and retry.json()["replayed"] is False


async def test_two_concurrent_writes_on_one_revision_one_wins_one_gets_412(api_client, file_db):
    factory, _ = file_db
    revision = await _revision(api_client)

    async def write(summary: str, idem: str):
        return await api_client.patch(
            URL, json={"fields": {"summary": summary}}, headers=write_headers(MASTER, revision, idem),
        )

    first, second = await asyncio.gather(write("A", "k-a"), write("B", "k-b"))
    assert sorted([first.status_code, second.status_code]) == [200, 412]
    loser = first if first.status_code == 412 else second
    winner = second if loser is first else first
    assert loser.json()["detail"]["currentRevision"] == winner.json()["revisionAfter"]
    assert await _summary(factory) == ("A" if winner is first else "B")
    assert await _edits(factory) == 1


async def test_concurrent_replays_of_one_idempotency_key_record_a_single_edit(api_client, file_db):
    factory, _ = file_db
    revision = await _revision(api_client)

    async def write():
        return await api_client.patch(
            URL, json={"fields": {"summary": "same"}}, headers=write_headers(MASTER, revision, "k-same"),
        )

    results = await asyncio.gather(*(write() for _ in range(4)))
    assert [r.status_code for r in results] == [200] * 4
    assert sorted(r.json()["replayed"] for r in results) == [False, True, True, True]
    assert len({r.json()["editId"] for r in results}) == 1
    assert await _edits(factory) == 1 and await _summary(factory) == "same"


async def test_reads_are_not_blocked_while_a_write_transaction_is_open(api_client, file_db, monkeypatch):
    factory, db_path = file_db
    acquired = threading.Event()
    blocker = threading.Thread(target=_hold_write_lock, args=(db_path, acquired, 1.0), daemon=True)
    blocker.start()
    assert acquired.wait(5)
    started = time.monotonic()
    resp = await api_client.get(URL, headers=h(MASTER))
    assert resp.status_code == 200 and time.monotonic() - started < 0.9          # WAL readers never wait
    preview = await api_client.post(f"{URL}/preview", json={"fields": {"summary": "x"}}, headers=h(MASTER))
    assert preview.status_code == 200                                              # a simulation takes no writer lock
    blocker.join(timeout=3)
