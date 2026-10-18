"""Write transaction that takes SQLite's writer lock UP FRONT, with a deadline.

Used by the metadata API (ADR 0011 § D2.3/D2.4). Why not `write_with_retry`:
that primitive retries a whole replayable unit and waits as long as the 60 s
`busy_timeout` of the pooled connection allows. The metadata API must instead
answer 503 `database_busy` within its own deadline (default 15 s) AND must read
the item's revision *after* holding the lock, otherwise two concurrent writers
could both pass the `If-Match` check on the same revision.

Mechanism (spike B2-01, `tests/test_immediate_write_real_lock.py`): the
driver is in legacy transaction control, so an explicit `BEGIN IMMEDIATE` sent
through the session starts a real SQLite write transaction that
`session.commit()` / `rollback()` then finish. `PRAGMA busy_timeout` is lowered
to the remaining deadline for the `BEGIN IMMEDIATE` only (SQLite itself gives
up, an aiosqlite call blocked in its thread cannot be cancelled), then put back
to the pool value at once so the pooled connection never keeps the short value —
also when the request is cancelled while it waits (the restore is shielded).

Concurrency cap (security review of lot B2): a request waiting for the writer lock
holds one connection of the API pool. `MAX_CONCURRENT_WRITERS` bounds how many may wait
at once; the others queue on a semaphore (no connection held) and give up with
`DatabaseBusy` when their deadline passes, so a burst of PATCH cannot drain the pool that
serves the rest of the API. Process-local, like the rate limiters.

Never retry on the same session after a failure here (CLAUDE.md piege 8): the
caller turns `DatabaseBusy` into a 503 and the CLIENT retries the whole request.
"""
from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("plexhub.db.immediate")

# Pool value (app/db/database.py: connect_args timeout=60 / PRAGMA busy_timeout).
_POOL_BUSY_TIMEOUT_MS = 60_000
_MIN_BUSY_MS = 50

# Writers allowed to hold a pool connection while waiting for / holding the writer lock.
MAX_CONCURRENT_WRITERS = 3
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _slots.get(loop)
    if sem is None:
        sem = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT_WRITERS)
    return sem


class DatabaseBusy(Exception):
    """The writer lock was not obtained, or the deadline passed, before COMMIT.
    Nothing has been written."""


@dataclass
class WriteTx:
    session: AsyncSession
    deadline_at: float  # time.monotonic()

    def remaining(self) -> float:
        return self.deadline_at - time.monotonic()

    def check_deadline(self) -> None:
        """Call between steps, BEFORE the commit: past the deadline => DatabaseBusy
        (the context manager rolls back). The COMMIT itself is never interrupted."""
        if self.remaining() <= 0:
            raise DatabaseBusy("deadline exceeded before commit")


def _is_locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "busy" in msg


async def _restore_busy_timeout(session: AsyncSession) -> None:
    try:
        await session.execute(text(f"PRAGMA busy_timeout={_POOL_BUSY_TIMEOUT_MS}"))
    except Exception:  # pragma: no cover - never mask the real outcome
        logger.warning("could not restore busy_timeout on a pooled connection")


async def _cleanup(session: AsyncSession) -> None:
    """Restore the pool `busy_timeout` and roll back, whatever interrupted us. Shielded: a
    cancelled request must still hand its connection back in the pool state."""

    async def _run() -> None:
        await _restore_busy_timeout(session)
        try:
            await session.rollback()
        except Exception:  # pragma: no cover
            logger.warning("rollback failed while cleaning up a write session")

    try:
        await asyncio.shield(_run())
    except asyncio.CancelledError:
        # the shielded cleanup keeps running to completion in the background
        raise


@asynccontextmanager
async def immediate_write_session(
    session_factory: Callable[[], AsyncSession] | None = None,
    *,
    deadline_s: float,
) -> AsyncIterator[WriteTx]:
    """Yield a `WriteTx` holding the SQLite writer lock; commit on normal exit,
    roll back on any exception. Raises `DatabaseBusy` when the lock cannot be
    obtained within `deadline_s` or `check_deadline()` finds the deadline passed."""
    if session_factory is None:
        from app.db.database import async_session_factory as session_factory  # type: ignore[assignment]

    deadline_at = time.monotonic() + deadline_s
    sem = _semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=deadline_s)
    except asyncio.TimeoutError:
        raise DatabaseBusy("too many writers waiting") from None
    try:
        async with session_factory() as session:
            remaining = max(deadline_at - time.monotonic(), 0.0)
            wait_ms = max(int(remaining * 1000), _MIN_BUSY_MS)
            try:
                await session.execute(text(f"PRAGMA busy_timeout={wait_ms}"))
                await session.execute(text("BEGIN IMMEDIATE"))
            except BaseException as exc:
                await _cleanup(session)
                if isinstance(exc, OperationalError) and _is_locked(exc):
                    raise DatabaseBusy("writer lock not obtained") from None
                raise
            await _restore_busy_timeout(session)

            tx = WriteTx(session=session, deadline_at=deadline_at)
            try:
                yield tx
                tx.check_deadline()
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
    finally:
        sem.release()
