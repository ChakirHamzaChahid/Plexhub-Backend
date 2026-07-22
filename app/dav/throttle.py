"""DAV-1B: per-account upstream concurrency throttle.

Xtream providers cap simultaneous connections per account (`XtreamAccount.
max_connections`, same field the sync worker and `health_check_worker.
_account_concurrency` already clamp against). The WebDAV relay (`app/dav/
relay.py`) must never let rclone's parallel readers/prefetch open more
upstream connections to one provider than that cap allows — tripping it
answers 503/drops the connection, which reads as a dead stream to rclone.

`AccountThrottle` is a process-local `asyncio.Semaphore` per `account_id`,
sized to `upstream_limit(account)` the FIRST time that account is seen.
⚠️ If uvicorn is ever run with `--workers N > 1`, each worker process gets
its own semaphore and the effective cap multiplies by N (see docstring on
`AccountThrottle`) — this repo's Dockerfile runs a single process, so that
is out of scope here, not fixed here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from app.config import settings
from app.models.database import XtreamAccount

logger = logging.getLogger("plexhub.dav")


class ThrottleTimeout(Exception):
    """No upstream permit became available within the configured wait
    (`DAV_QUEUE_TIMEOUT_SECONDS`). The router maps this to `503 Service
    Unavailable` + `Retry-After` — rclone retries on its own."""


def upstream_limit(account: XtreamAccount) -> int:
    """Effective per-account upstream concurrency: the smaller of the global
    default (`DAV_UPSTREAM_PER_ACCOUNT`) and the provider's own
    `max_connections`, floored at 1 so a misconfigured account (0/None)
    never fully blocks the relay. Mirrors
    `health_check_worker._account_concurrency`'s clamp, but the DIRECTION
    is reversed on purpose: that validator falls back to a large global
    concurrency when `max_connections` is unset (0 = "no limit"), whereas a
    relay MUST stay conservative by default — an unset/zero
    `max_connections` here still clamps to `DAV_UPSTREAM_PER_ACCOUNT`
    (never higher), because opening more upstream connections than the
    provider expects is exactly the failure mode this throttle exists to
    prevent, not just a speed concern."""
    mc = account.max_connections or 0
    limit = min(settings.DAV_UPSTREAM_PER_ACCOUNT, mc) if mc > 0 else settings.DAV_UPSTREAM_PER_ACCOUNT
    return max(1, limit)


class AccountThrottle:
    """Process-local semaphore-per-account gate.

    A semaphore is created lazily on the first `acquire()` for a given
    `account_id`, sized to whatever `limit` that first call passed. Later
    calls for the SAME `account_id` reuse the existing semaphore
    unconditionally — the limit is effectively frozen at the account's
    first-seen value for the lifetime of this process (a `max_connections`
    edit takes effect on the next restart, not live; acceptable for a
    self-hosted single-process relay, not attempted here).
    """

    def __init__(self) -> None:
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def _get_semaphore(self, account_id: str, limit: int) -> asyncio.Semaphore:
        sem = self._sems.get(account_id)
        if sem is not None:
            return sem
        async with self._lock:
            sem = self._sems.get(account_id)
            if sem is None:
                sem = asyncio.Semaphore(limit)
                self._sems[account_id] = sem
            return sem

    async def acquire(
        self, account_id: str, limit: int, timeout: float,
    ) -> Callable[[], None]:
        """Acquire one upstream permit for `account_id`, waiting up to
        `timeout` seconds. Returns an idempotent `release` callable — calling
        it more than once (e.g. both the caller's `finally` and a client
        mid-stream disconnect) is a no-op past the first call, so the permit
        is never double-released back above `limit`.

        Raises `ThrottleTimeout` if no permit frees up in time; nothing is
        acquired in that case (no matching release needed).
        """
        sem = await self._get_semaphore(account_id, limit)
        try:
            await asyncio.wait_for(sem.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ThrottleTimeout(
                f"no upstream permit available within {timeout}s"
            ) from None

        released = {"done": False}

        def release() -> None:
            if released["done"]:
                return
            released["done"] = True
            sem.release()

        return release


# Singleton — one throttle per process (see module docstring re: --workers).
account_throttle = AccountThrottle()
