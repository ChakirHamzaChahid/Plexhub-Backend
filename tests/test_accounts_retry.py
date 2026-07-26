"""Guard tests for CR-C04 / AUDIT-P1-001 (S3.3): request-path writes in
app/api/accounts.py commit via a lock-retry helper instead of relying on
get_db's un-retried implicit commit (db/database.py get_db commits on
successful yield, but does NOT retry on 'database is locked').

update_account/delete_account are converted to `write_with_retry` (ADR 0004,
Decision 4: a fresh session per attempt is the only pattern that actually
survives a real SQLite lock — `commit_with_retry`'s same-session retry does
not, see tests/test_db_retry_real_lock.py). This suite locks in that both
endpoints commit explicitly through `write_with_retry`, same as the workers
and download_worker/plex_sync_service.

create_account is deliberately left on `commit_with_retry` (see the comment
at its call site, app/api/accounts.py) — account_service.create_account
mixes a real Xtream network auth call with the staged write, so replaying it
under write_with_retry would re-authenticate against the provider on every
retry.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import Base, XtreamAccount

# The JSON API is X-API-Key gated (fail-closed) — same pattern as
# tests/test_categories_refresh_camelcase.py.
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


async def test_update_account_commits_via_retry_helper(
    monkeypatch, api_client, seeded_account,
):
    """AUDIT-P1-001 (S3.3): PUT /api/accounts/{id} must commit explicitly via
    write_with_retry (it used to rely solely on get_db's implicit,
    un-retried commit; then on commit_with_retry, which cannot actually
    survive a real lock — ADR 0004, Decision 4)."""
    import app.api.accounts as accounts_module

    calls = {"n": 0}
    real_write_with_retry = accounts_module.write_with_retry

    async def _spy(work, **kwargs):
        calls["n"] += 1
        return await real_write_with_retry(work, **kwargs)

    monkeypatch.setattr(accounts_module, "write_with_retry", _spy)

    resp = await api_client.put(
        "/api/accounts/a", json={"label": "Updated Label"}, headers=API_HEADERS,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["label"] == "Updated Label"
    assert calls["n"] == 1  # the write committed through write_with_retry


async def test_delete_account_commits_via_retry_helper(
    monkeypatch, api_client, seeded_account,
):
    """AUDIT-P1-001 (S3.3): DELETE /api/accounts/{id} (multi-table cascade)
    must commit explicitly via write_with_retry (it used to rely solely on
    get_db's implicit, un-retried commit; then on commit_with_retry, which
    cannot actually survive a real lock — ADR 0004, Decision 4)."""
    import app.api.accounts as accounts_module

    calls = {"n": 0}
    real_write_with_retry = accounts_module.write_with_retry

    async def _spy(work, **kwargs):
        calls["n"] += 1
        return await real_write_with_retry(work, **kwargs)

    monkeypatch.setattr(accounts_module, "write_with_retry", _spy)

    resp = await api_client.delete("/api/accounts/a", headers=API_HEADERS)

    assert resp.status_code == 204, resp.text
    assert calls["n"] == 1  # the write committed through write_with_retry


# ──────────────────────────────────────────────────────────────────────────────
# Real-lock contention (ADR 0004, Decision 4) — proves write_with_retry
# actually survives a genuine SQLite writer-vs-writer lock on these two
# converted endpoints, not just that a helper function is called.
# ──────────────────────────────────────────────────────────────────────────────


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    """Real cross-connection write lock, held on a plain background thread —
    mirrors tests/test_db_retry_real_lock.py's harness."""
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS _lock_probe (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO _lock_probe DEFAULT VALUES")
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def seeded_account_filedb(tmp_path, monkeypatch):
    """Same seeding as `seeded_account`, but on a REAL file-backed engine
    with a short `busy_timeout` — required to exercise a genuine SQLite
    lock within a test's time budget (a `:memory:` engine can't be
    contended from a second, independent sqlite3 connection)."""
    from app.db import database as db_module

    db_path = tmp_path / "accounts_lock_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_short_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA busy_timeout=50")

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)

    async with factory() as s:
        s.add(_account("a"))
        await s.commit()

    yield str(db_path)
    await engine.dispose()


async def test_update_account_survives_real_lock(api_client, seeded_account_filedb):
    """AUDIT-P1-001 (S3.3): PUT /api/accounts/{id} must succeed despite a
    genuine SQLite write lock held by an independent connection for longer
    than write_with_retry's first couple of attempts — proving the fresh-
    session-per-attempt primitive (not just busy_timeout) recovers the
    write."""
    db_path = seeded_account_filedb
    hold_seconds = 0.35
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

    resp = await api_client.put(
        "/api/accounts/a", json={"label": "Survived Lock"}, headers=API_HEADERS,
    )

    blocker.join(timeout=5)
    assert not blocker.is_alive()
    assert resp.status_code == 200, resp.text
    assert resp.json()["label"] == "Survived Lock"


async def test_delete_account_survives_real_lock(api_client, seeded_account_filedb):
    """AUDIT-P1-001 (S3.3): DELETE /api/accounts/{id} (multi-table cascade)
    must succeed despite a genuine SQLite write lock held by an independent
    connection."""
    db_path = seeded_account_filedb
    hold_seconds = 0.35
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"

    resp = await api_client.delete("/api/accounts/a", headers=API_HEADERS)

    blocker.join(timeout=5)
    assert not blocker.is_alive()
    assert resp.status_code == 204, resp.text
