"""AUDIT-P1-001 / ADR 0004 Decision 4, Vague 3 S3.1 (docs/architecture/adr/
0004-audit-v1-remediation-contracts.md) -- worker call-site migration guard.

`tests/test_db_retry_real_lock.py` already proves the PRIMITIVE
(`write_with_retry`) survives a genuine SQLite WAL lock. This module proves
that the specific worker call sites converted in S3.1 (a fresh session per
retry attempt, replacing a same-session `commit_with_retry` that could only
ever succeed on the FIRST try under real contention) actually route through
that primitive end-to-end, against a REAL file-backed WAL database with a
real writer lock held by a second connection -- not a synthetic
`OperationalError`.

Converted and covered here:
- `sync_worker.sync_account`'s final "finalize" commit (visibility/adult
  tagging/orphan cleanup/last_synced_at).
- `enrichment_worker.run()`'s post-batch `display_rating` recompute.
- `health_check_worker._run_health_check_batch`'s apply-results commit.
- `enrichment_backfill_worker.run_backfill`'s Phase A write step (via
  `_run_phase_a`) and Phase B recompute.

Each test seeds real app-schema rows, holds a genuine `BEGIN IMMEDIATE` write
lock from an independent `sqlite3` connection for `_HOLD_SECONDS`, then drives
the worker function through the (now short-`busy_timeout`) engine — asserting
both that the call completes without raising AND that the row was actually
written, proving the retry — not just a silent no-op — is what recovered it.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import Base, Media, XtreamAccount
from app.services.xtream_service import xtream_service
from app.workers import enrichment_backfill_worker as backfill
from app.workers import enrichment_worker as ew
from app.workers import health_check_worker as hc
from app.workers import sync_worker as sync_worker_module

# Real contention takes real wall-clock time; short enough to keep the suite
# fast, long enough to force at least one genuine retry past the first
# attempt (mirrors tests/test_db_retry_real_lock.py's own budget).
_HOLD_SECONDS = 0.35


async def _init_wal_schema(db_path: str) -> None:
    """Set WAL mode via a plain sqlite3 connection BEFORE any SQLAlchemy
    engine touches the file (WAL only reliably "sticks" for a real file when
    set this way — see test_db_retry_real_lock.py's identical rationale),
    then create the full app schema through a throwaway async engine.
    Async (awaited by each test) rather than `asyncio.run()`-wrapped: these
    tests already run inside pytest-asyncio's event loop, which cannot host
    a nested `asyncio.run()`."""
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal", f"WAL mode did not stick: {mode}"
    finally:
        conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _make_short_timeout_engine(db_path: str, busy_timeout_ms: int = 50):
    """A file-based async engine with a deliberately SHORT `busy_timeout`
    (production uses 60s, CLAUDE.md piège #8) so a real lock surfaces
    `database is locked` quickly instead of SQLite silently absorbing the
    whole contention window — same rationale as
    tests/test_db_retry_real_lock.py::_make_engine."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

    return engine


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    """Runs on a plain background thread with its OWN sqlite3 connection:
    `BEGIN IMMEDIATE` acquires SQLite's RESERVED lock immediately, held for
    `hold_seconds` before committing. Any writer against this file --
    including our aiosqlite/SQLAlchemy engine -- gets a genuine `database is
    locked` once its (short) busy_timeout elapses. A plain read-only
    transaction is NOT blocked by this (WAL readers proceed against their own
    snapshot), which is exactly what lets each test's earlier, unconverted
    read-only phases complete unaffected."""
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO xtream_accounts "
            "(id, label, base_url, port, username, password, status, "
            " max_connections, allowed_formats, last_synced_at, is_active, "
            " created_at, category_filter_mode) "
            "VALUES ('blocker', 'blocker', 'http://blocker', 80, 'u', 'p', "
            " 'Unknown', 1, '', 0, 1, 0, 'all')"
        )
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


def _start_blocker(db_path: str, hold_seconds: float = _HOLD_SECONDS) -> threading.Thread:
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"
    return blocker


# ─── sync_worker: finalize_account (visibility/adult/cleanup/last_synced_at) ──


async def test_sync_account_finalize_survives_real_lock(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sync_finalize_lock.db")
    await _init_wal_schema(db_path)

    engine = _make_short_timeout_engine(db_path)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sync_worker_module, "async_session_factory", factory)

    account_id = "wr_lock_sync"
    async with factory() as s:
        s.add(XtreamAccount(
            id=account_id, label="Test", base_url="http://x.example", port=80,
            username="u", password="p", is_active=True, created_at=0,
        ))
        await s.commit()

    # Empty catalogs -- hermetic, no network -- so every phase up to the
    # final finalize commit is read-only (no contention with the held write
    # lock; only the finalize write needs the RESERVED lock the blocker
    # holds).
    async def _empty_list(*a, **kw):
        return []

    async def _empty_dict(*a, **kw):
        return {}

    monkeypatch.setattr(xtream_service, "get_vod_categories", _empty_list)
    monkeypatch.setattr(xtream_service, "get_series_categories", _empty_list)
    monkeypatch.setattr(xtream_service, "get_live_categories", _empty_list)
    monkeypatch.setattr(xtream_service, "get_vod_streams", _empty_list)
    monkeypatch.setattr(xtream_service, "get_vod_info", _empty_dict)
    monkeypatch.setattr(xtream_service, "get_series", _empty_list)
    monkeypatch.setattr(xtream_service, "get_series_info", _empty_dict)
    monkeypatch.setattr(xtream_service, "get_live_streams", _empty_list)

    blocker = _start_blocker(db_path)

    job_id = await sync_worker_module.sync_account(account_id)

    blocker.join(timeout=5)
    assert not blocker.is_alive()

    job = sync_worker_module.get_sync_job(job_id)
    assert job is not None
    assert job["status"] == "completed", job

    async with factory() as s:
        row = await s.get(XtreamAccount, account_id)
        assert row.last_synced_at > 0, (
            "finalize's last_synced_at write must have landed despite the "
            "real lock -- proves write_with_retry actually retried, not a "
            "silently-swallowed failure"
        )


# ─── enrichment_worker: post-batch display_rating recompute ─────────────────


async def test_enrichment_recompute_display_rating_survives_real_lock(tmp_path, monkeypatch):
    db_path = str(tmp_path / "enrichment_recompute_lock.db")
    await _init_wal_schema(db_path)

    engine = _make_short_timeout_engine(db_path)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ew, "async_session_factory", factory)

    # A row whose display_rating is stale relative to imdb/tmdb -- the
    # recompute's UPDATE must actually change it (and thus genuinely need
    # the write lock, not just execute a no-op).
    async with factory() as s:
        s.add(Media(
            rating_key="wr_lock_movie", server_id="xtream_wrlock",
            library_section_id="1", title="Wr Lock Movie", type="movie",
            imdb_rating=7.0, tmdb_rating=6.0, display_rating=0.0,
        ))
        await s.commit()

    # Empty EnrichmentQueue -- Phase 1/2 loops do nothing, so `run()` reaches
    # the recompute block (the converted site) almost immediately, well
    # within the blocker's hold window.
    blocker = _start_blocker(db_path)

    await ew.run()

    blocker.join(timeout=5)
    assert not blocker.is_alive()

    async with factory() as s:
        row = await s.get(Media, {
            "rating_key": "wr_lock_movie", "server_id": "xtream_wrlock",
            "filter": "all", "sort_order": "default",
        })
        assert row.display_rating == pytest.approx(6.5), (
            "recompute_display_rating must have landed despite the real "
            "lock -- proves write_with_retry actually retried"
        )


# ─── health_check_worker: apply-results commit ───────────────────────────────


async def test_health_check_batch_apply_survives_real_lock(tmp_path, monkeypatch):
    db_path = str(tmp_path / "health_check_apply_lock.db")
    await _init_wal_schema(db_path)

    engine = _make_short_timeout_engine(db_path)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(hc, "worker_session_factory", factory)

    async with factory() as s:
        s.add(XtreamAccount(
            id="wrlock", label="wrlock", base_url="http://wrlock.test",
            username="u", password="p", max_connections=20,
        ))
        s.add(Media(
            rating_key="vod_xtream_wrlock_0.mp4", server_id="xtream_wrlock",
            library_section_id="1", title="t0", type="movie", page_offset=0,
            is_in_allowed_categories=True,
        ))
        await s.commit()

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    async def _fake_check_one(client, item, account, semaphore):
        return item, True, "timeout", None  # marks stream_error_count += 1

    monkeypatch.setattr(hc, "_check_one", _fake_check_one)

    blocker = _start_blocker(db_path)

    await hc._run_health_check_batch()

    blocker.join(timeout=5)
    assert not blocker.is_alive()

    async with factory() as s:
        result = await s.execute(
            select(Media).where(Media.rating_key == "vod_xtream_wrlock_0.mp4")
        )
        row = result.scalars().first()
        assert row is not None
        assert row.last_stream_check is not None, (
            "the apply-results commit must have landed despite the real "
            "lock -- proves write_with_retry actually retried"
        )
        assert row.stream_error_count == 1


# ─── enrichment_backfill_worker: Phase A write step + Phase B recompute ─────


async def test_enrichment_backfill_phase_a_write_page_survives_real_lock(
    tmp_path, monkeypatch,
):
    db_path = str(tmp_path / "backfill_phase_a_lock.db")
    await _init_wal_schema(db_path)

    engine = _make_short_timeout_engine(db_path)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as s:
        s.add(Media(
            rating_key="wr_lock_backfill", server_id="xtream_wrlock",
            library_section_id="1", title="Wr Lock Backfill", type="movie",
            imdb_id="tt0000001", imdb_rating=None,
        ))
        await s.commit()

    async def _fake_fetch_omdb_by_id(imdb_id, session_factory):
        from app.services.omdb_service import OMDbData

        return (
            OMDbData(
                title="Wr Lock Backfill", year="2020", runtime_minutes=None,
                genre=None, director=None, actors=None, plot=None,
                imdb_rating=8.1, imdb_votes=1000, type="movie",
                imdb_id=imdb_id,
            ),
            None,  # cache put already resolved -- nothing new to persist
        )

    monkeypatch.setattr(backfill, "_fetch_omdb_by_id", _fake_fetch_omdb_by_id)

    blocker = _start_blocker(db_path)

    await backfill._run_phase_a(
        "wr_lock_job", factory, media_types=("movie", "show"), limit=None,
    )

    blocker.join(timeout=5)
    assert not blocker.is_alive()

    async with factory() as s:
        result = await s.execute(
            select(Media).where(Media.rating_key == "wr_lock_backfill")
        )
        row = result.scalars().first()
        assert row is not None
        assert row.imdb_rating == pytest.approx(8.1), (
            "Phase A's write_page commit must have landed despite the real "
            "lock -- proves write_with_retry actually retried"
        )


async def test_enrichment_backfill_recompute_survives_real_lock(tmp_path, monkeypatch):
    db_path = str(tmp_path / "backfill_recompute_lock.db")
    await _init_wal_schema(db_path)

    engine = _make_short_timeout_engine(db_path)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(backfill, "async_session_factory", factory)

    async with factory() as s:
        s.add(Media(
            rating_key="wr_lock_recompute", server_id="xtream_wrlock",
            library_section_id="1", title="Wr Lock Recompute", type="show",
            imdb_rating=9.0, tmdb_rating=7.0, display_rating=0.0,
        ))
        await s.commit()

    blocker = _start_blocker(db_path)

    job_id = "wr_lock_backfill_job"
    backfill.register_job(job_id, {"status": "running"})
    try:
        await backfill.run_backfill(
            job_id, factory, media_type="all",
            recompute_display_rating=True, limit=0,
        )
    finally:
        backfill._jobs.clear()
        backfill._running = False

    blocker.join(timeout=5)
    assert not blocker.is_alive()

    async with factory() as s:
        result = await s.execute(
            select(Media).where(Media.rating_key == "wr_lock_recompute")
        )
        row = result.scalars().first()
        assert row is not None
        assert row.display_rating == pytest.approx(8.0), (
            "Phase B's recompute commit must have landed despite the real "
            "lock -- proves write_with_retry actually retried"
        )
