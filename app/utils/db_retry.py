"""Retry helper for SQLite `database is locked` contention.

SQLite serializes writes; under multi-worker load (sync + enrichment +
health_check + plex_generator) bursts can exceed the 5s `busy_timeout`.
This wrapper adds a short application-level retry on top.

.. important::
    ``commit_with_retry`` (same-session retry) is **not** a resilience
    primitive against a real lock — see its docstring and
    ``write_with_retry`` below. ADR 0004 (``docs/architecture/adr/
    0004-audit-v1-remediation-contracts.md``, Decision 4) is the source of
    truth for this file's contract.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from sqlalchemy.exc import OperationalError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("plexhub.db.retry")

T = TypeVar("T")

# Tuned for SQLite contention: short hops, total budget ~1.6s on top of busy_timeout.
DEFAULT_DELAYS = (0.1, 0.5, 1.0)


def _is_locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg


async def run_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
    op: str = "db_op",
) -> T:
    """Run an async DB operation with retry on `database is locked`.

    `func` must be a zero-arg coroutine factory so it can be re-invoked.
    """
    last_exc: OperationalError | None = None
    for attempt, delay in enumerate((*delays, None)):
        try:
            return await func()
        except OperationalError as e:
            if not _is_locked(e):
                raise
            last_exc = e
            if delay is None:
                break
            logger.warning(
                f"{op} hit 'database is locked' (attempt {attempt + 1}); retrying in {delay}s"
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


async def commit_with_retry(
    db: AsyncSession,
    *,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
    op: str = "commit",
) -> None:
    """Drop-in replacement for `await db.commit()` — **not** a same-session
    retry primitive against a real lock, despite appearances.

    In SQLAlchemy 2.x a failed ``commit()`` invalidates the session's
    transaction: a second ``db.commit()`` attempt on the *same* session
    raises ``PendingRollbackError`` (a session-state guard), not another
    ``OperationalError`` — so the underlying retry loop's
    ``except OperationalError`` never fires a second real attempt. This
    helper therefore only actually protects the case where SQLite's own
    ``busy_timeout`` (60s in production, `app/db/database.py`) resolves the
    contention *before* the first ``commit()`` raises at all; it does not
    add resilience beyond that.

    On ``PendingRollbackError`` we do a hygiene ``rollback()`` (so the
    session isn't left in a broken state for whatever the caller does
    next) and **re-raise the original `OperationalError`** (``database is
    locked``), never the confusing ``PendingRollbackError`` — so the trace
    a caller sees always names the real cause.

    For any writer actually exposed to contention (concurrent workers,
    request-path writers under load), use :func:`write_with_retry` instead,
    which opens a **fresh session per attempt** and is the only pattern
    that can genuinely retry past a real lock. See ADR 0004, Decision 4.
    """
    last_locked_exc: OperationalError | None = None
    for attempt, delay in enumerate((*delays, None)):
        try:
            await db.commit()
            return
        except OperationalError as e:
            if not _is_locked(e):
                raise
            last_locked_exc = e
            if delay is None:
                break
            logger.warning(
                f"{op} hit 'database is locked' (attempt {attempt + 1}); retrying in {delay}s"
            )
            await asyncio.sleep(delay)
        except PendingRollbackError:
            # The first failed `db.commit()` above already invalidated this
            # session's transaction — SQLAlchemy 2.x guard behaviour, not
            # another real lock. A same-session retry cannot recover from
            # this (see module/ADR 0004 docstring); do a hygiene rollback
            # and re-raise the ORIGINAL `OperationalError` ("database is
            # locked"), never this confusing guard exception, so the trace
            # always names the real cause.
            logger.warning(
                f"{op}: same-session retry cannot survive a real lock "
                "(PendingRollbackError after an invalidated transaction); "
                "re-raising the original 'database is locked' error. Use "
                "write_with_retry for a writer that must actually survive "
                "contention."
            )
            await db.rollback()
            if last_locked_exc is not None:
                raise last_locked_exc from None
            raise
    if last_locked_exc is not None:
        raise last_locked_exc


async def write_with_retry(
    work: Callable[[AsyncSession], Awaitable[T]],
    *,
    session_factory: Callable[[], AsyncSession] | None = None,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
    op: str = "write",
) -> T:
    """Run `work(session)` with retry on `database is locked`, opening a
    **fresh session for every attempt**.

    This is the only pattern that genuinely survives a real SQLite lock:
    unlike a same-session retry (`commit_with_retry`), a fresh session has
    no invalidated transaction to trip over, so a second attempt raises
    the same retryable `OperationalError` again instead of
    `PendingRollbackError`.

    Contract: `work` must be **replayable** — it should build/attach its
    own ORM objects from scratch on every call (never capture objects from
    an outer session) and commit inside itself via the session it is
    given. Do **not** add `await session.rollback()` before retrying on the
    *same* session as a "fix" for `commit_with_retry`: SQLAlchemy expels
    `pending` objects on rollback, so a same-session retry after that would
    commit zero rows *without raising* — a silent write loss, worse than
    the crash it would replace. Fresh-session-per-attempt is the only
    correct fix (ADR 0004, Decision 4).

    Args:
        work: async callable taking the fresh `AsyncSession` for this
            attempt and returning the result. Must call
            `await session.commit()` itself (or otherwise finalize the
            transaction) before returning.
        session_factory: zero-arg session factory. Defaults to
            `app.db.database.async_session_factory` (the API-facing pool)
            when omitted — imported lazily to keep this module free of a
            module-load-time dependency on the db layer.
        delays: retry backoff schedule.
        op: label used in retry/warning log lines.
    """
    if session_factory is None:
        from app.db.database import async_session_factory as session_factory  # type: ignore[assignment]

    async def _attempt() -> T:
        async with session_factory() as session:
            return await work(session)

    return await run_with_retry(_attempt, delays=delays, op=op)
