"""Retry helper for SQLite `database is locked` contention.

SQLite serializes writes; under multi-worker load (sync + enrichment +
health_check + plex_generator) bursts can exceed the 5s `busy_timeout`.
This wrapper adds a short application-level retry on top.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from sqlalchemy.exc import OperationalError
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
) -> None:
    """Drop-in replacement for `await db.commit()` that retries on contention."""
    await run_with_retry(db.commit, delays=delays, op="commit")
