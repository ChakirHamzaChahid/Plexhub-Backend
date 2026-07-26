"""S3.2 (`/refacto` audit v1, VAGUE 3): real-lock proof that the converted
`category_service.update_media_adult_flags` survives genuine SQLite
contention via `write_with_retry` (AUDIT-P1-001, ADR 0004 Decision 4).

Mirrors the harness in `tests/test_db_retry_real_lock.py` (file-backed WAL
DB + a real `BEGIN IMMEDIATE` writer on a background thread) but against the
actual app schema and the actual converted call site, not a throwaway probe
table — proving the conversion (not just the primitive) survives contention.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import Base, Media, XtreamAccount, XtreamCategory
from app.services.category_service import update_media_adult_flags
from app.utils.server_id import build_server_id


class _ListHandler(logging.Handler):
    """Attached directly to `plexhub.db.retry` — see
    `tests/test_db_retry_real_lock.py` for why a root/caplog handler is not
    reliable once `app.main` has set `propagate = False` on the "plexhub"
    logger elsewhere in the suite."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def _create_schema(db_path: str) -> None:
    """Real file-backed WAL database with the full app schema (not a
    throwaway table) — the converted call site touches real `Media` /
    `XtreamCategory` / `XtreamAccount` rows."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


def _add_probe_table(db_path: str) -> None:
    """A throwaway table the blocker thread writes into to hold a real
    write lock — isolated from the app schema so the blocker thread never
    needs to satisfy any app-table constraint."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS retry_probe "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO retry_probe (value) VALUES ('blocker')")
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


def _make_engine(db_path: str, busy_timeout_ms: int = 50):
    """A deliberately SHORT `busy_timeout` so SQLite surfaces `database is
    locked` quickly instead of absorbing the whole contention window inside
    its own wait (production uses 60s, CLAUDE.md piège #8)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

    return engine


async def _seed(factory) -> None:
    async with factory() as s:
        s.add_all([
            XtreamAccount(
                id="a", label="Compte", base_url="http://a.example", port=80,
                username="u", password="p", is_active=True, created_at=0,
            ),
            XtreamCategory(
                account_id="a", category_id="1555", category_type="vod",
                category_name="VOD - ADULT +18", is_allowed=True, last_fetched_at=0,
            ),
            Media(
                rating_key="vod_adult.mp4", server_id=build_server_id("a"),
                filter="1555", sort_order="default", library_section_id="xtream_vod",
                title="Naughty Film", type="movie", year=2020, content_rating="PG-13",
                unification_id="tmdb://vod_adult.mp4",
                is_in_allowed_categories=True, is_broken=False,
            ),
        ])
        await s.commit()


def _read_flags(db_path: str) -> tuple[int, str]:
    check = sqlite3.connect(db_path)
    try:
        row = check.execute(
            "SELECT is_adult, content_rating FROM media WHERE rating_key = ?",
            ("vod_adult.mp4",),
        ).fetchone()
    finally:
        check.close()
    assert row is not None, "expected the seeded movie row to still exist"
    return row


class TestUpdateMediaAdultFlagsRealLock:
    """AUDIT-P1-001 / ADR 0004 Decision 4 — the converted call site actually
    survives a real SQLite lock, and the retried write is correct and
    idempotent (a fresh-session replay recomputes the whole flag set from
    scratch, never a partial/doubled effect)."""

    async def test_survives_a_real_lock_and_flags_the_movie(self, tmp_path):
        db_path = str(tmp_path / "adult_flags_lock.db")
        await _create_schema(db_path)
        _add_probe_table(db_path)

        engine = _make_engine(db_path)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        await _seed(factory)

        hold_seconds = 0.35
        lock_acquired = threading.Event()
        blocker = threading.Thread(
            target=_hold_write_lock,
            args=(db_path, lock_acquired, hold_seconds),
            daemon=True,
        )
        blocker.start()
        assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

        retry_logger = logging.getLogger("plexhub.db.retry")
        list_handler = _ListHandler()
        retry_logger.addHandler(list_handler)
        try:
            start = time.monotonic()
            async with factory() as session:
                await update_media_adult_flags(session, "a")
            elapsed = time.monotonic() - start
        finally:
            retry_logger.removeHandler(list_handler)

        blocker.join(timeout=5)
        await engine.dispose()

        assert not blocker.is_alive()
        # Real contention takes real wall-clock time — a no-op/synthetic
        # retry (or a silently-swallowed write, per the ADR's rejected
        # "rollback and retry same-session" option) would return near-instantly.
        assert elapsed >= hold_seconds * 0.5, "expected the call to have genuinely waited out the lock"

        locked_warnings = [
            r for r in list_handler.records if "database is locked" in r.getMessage().lower()
        ]
        assert locked_warnings, "expected a REAL 'database is locked' warning from write_with_retry"

        is_adult, content_rating = _read_flags(db_path)
        assert is_adult == 1
        assert content_rating == settings.ADULT_CONTENT_RATING

    async def test_no_spurious_retry_without_contention(self, tmp_path):
        """Control case: with no concurrent writer, the converted call
        succeeds on the very first attempt against the same file-based WAL
        setup — it does not manufacture false contention."""
        db_path = str(tmp_path / "adult_flags_no_contention.db")
        await _create_schema(db_path)

        engine = _make_engine(db_path)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        await _seed(factory)

        async with factory() as session:
            await update_media_adult_flags(session, "a")

        await engine.dispose()

        is_adult, content_rating = _read_flags(db_path)
        assert is_adult == 1
        assert content_rating == settings.ADULT_CONTENT_RATING

    async def test_retry_stays_idempotent_on_reclassification(self, tmp_path):
        """A movie already (correctly) flagged adult, re-run under real lock
        contention, ends up in the same correct state — no double-toggle or
        partial write from the retried attempt."""
        db_path = str(tmp_path / "adult_flags_reclassify_lock.db")
        await _create_schema(db_path)
        _add_probe_table(db_path)

        engine = _make_engine(db_path)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        await _seed(factory)

        # First pass (no contention) establishes the flagged state.
        async with factory() as session:
            await update_media_adult_flags(session, "a")
        is_adult, content_rating = _read_flags(db_path)
        assert is_adult == 1
        assert content_rating == settings.ADULT_CONTENT_RATING

        # Second pass, under real contention — must reconverge to the exact
        # same state, not accumulate any effect.
        hold_seconds = 0.35
        lock_acquired = threading.Event()
        blocker = threading.Thread(
            target=_hold_write_lock,
            args=(db_path, lock_acquired, hold_seconds),
            daemon=True,
        )
        blocker.start()
        assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

        async with factory() as session:
            await update_media_adult_flags(session, "a")

        blocker.join(timeout=5)
        await engine.dispose()

        is_adult, content_rating = _read_flags(db_path)
        assert is_adult == 1
        assert content_rating == settings.ADULT_CONTENT_RATING
