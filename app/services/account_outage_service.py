"""Provider-outage bookkeeping for Xtream accounts (M025).

A single broken stream is normal attrition; a whole provider being down for
days is a different event, and until now the backend had **no memory of it
from one validation run to the next** — the circuit breaker in
`health_check_worker` tripped, skipped the account, and forgot everything.

This module is the only writer of `xtream_accounts.outage_strikes` /
`outage_since`, and the only place that decides which accounts are currently
"in confirmed outage":

  * `record_trip`  — the circuit breaker fired for this account this run.
  * `record_clear` — this account completed a healthy pass; forget the streak.
  * `masked_server_ids` — the server_ids whose media the app's read paths
    must hide (`outage_strikes >= settings.ACCOUNT_OUTAGE_STRIKES`).

.. important::
    This state is deliberately **NOT** `is_active` and **NOT** `is_broken`.
    `is_active` is a global kill switch read by the Plex/Jellyfin generator
    (`plex_generator/source.py`), `sync_worker`, the DAV relay and
    `download_service`; `is_broken` is filtered out of generation whenever
    `STREAM_FILTER_BROKEN` is on (the default). Flipping either during an
    outage would delete the generated library — the exact incident this
    feature exists to prevent. Only `media_service`'s read paths consume
    these columns, so an outage hides content from the app and nothing else.

Both writers go through `write_with_retry` with a fresh session per attempt:
the pipeline call site fires **immediately after** the breaker's
`db.rollback()` + `db.expunge_all()` on the shared session, and writing
through that session is the silent-write-loss trap documented in ADR 0004
Decision 4.

Callers pass their OWN `session_factory` (the dedicated worker pool, for every
current caller) rather than letting this module pick one: a worker that has
bound its DB traffic to a specific pool must not have part of that traffic
silently escape to another one — a bug this feature actually hit, where the
strike write went to the app pool while the worker ran against another
database entirely.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import worker_session_factory
from app.models.database import XtreamAccount
from app.utils.db_retry import write_with_retry
from app.utils.metrics import account_outage_strikes
from app.utils.server_id import build_server_id
from app.utils.time import now_ms
from app.utils.ttl_cache import TTLCache

logger = logging.getLogger("plexhub.outage")

SessionFactory = Callable[[], AsyncSession]

# The masked set changes at most once per validation run (hours apart), but is
# read on every catalogue request — a short TTL keeps the extra query off the
# hot path while still letting a manual DB fix take effect within a minute.
_MASK_TTL_SECONDS = 60.0
_masked_cache: TTLCache[tuple[int], frozenset[str]] = TTLCache(
    max_size=8, ttl_seconds=_MASK_TTL_SECONDS
)


def invalidate_mask_cache() -> None:
    """Drop the memoised masked-account set (tests, and after a trip/clear)."""
    _masked_cache.clear()


async def record_trip(
    account_id: str, *, session_factory: SessionFactory | None = None,
) -> int:
    """Record one more consecutive failed validation run for *account_id*.

    Returns the new strike count. `outage_since` is only set on the FIRST
    trip of a streak (COALESCE), so it keeps meaning "down since" rather than
    "last seen down".
    """
    async def _work(session: AsyncSession) -> int:
        await session.execute(
            update(XtreamAccount)
            .where(XtreamAccount.id == account_id)
            .values(
                outage_strikes=XtreamAccount.outage_strikes + 1,
                outage_since=func.coalesce(XtreamAccount.outage_since, now_ms()),
            )
        )
        await session.commit()
        strikes = (await session.execute(
            select(XtreamAccount.outage_strikes).where(XtreamAccount.id == account_id)
        )).scalar()
        return int(strikes or 0)

    strikes = await write_with_retry(
        _work, session_factory=session_factory or worker_session_factory,
        op="outage_trip",
    )
    account_outage_strikes.labels(account_id=account_id).set(strikes)
    invalidate_mask_cache()

    threshold = settings.ACCOUNT_OUTAGE_STRIKES
    if strikes >= threshold:
        logger.error(
            "Account %s: provider outage CONFIRMED (%d consecutive failed "
            "validation runs, threshold %d) — its media are now hidden from "
            "the app's catalogue reads%s. The generated library, sync, DAV "
            "and downloads are untouched.",
            account_id, strikes, threshold,
            "" if settings.ACCOUNT_OUTAGE_MASK_ENABLED
            else " (masking DISABLED by ACCOUNT_OUTAGE_MASK_ENABLED=false)",
        )
    else:
        logger.warning(
            "Account %s: validation circuit breaker tripped (%d/%d consecutive "
            "runs before the outage is confirmed)",
            account_id, strikes, threshold,
        )
    return strikes


async def record_clear(
    account_id: str, *, session_factory: SessionFactory | None = None,
) -> None:
    """Forget the outage streak for *account_id* after a healthy pass.

    No-op at the DB level when there is nothing to clear (the common case,
    every run for every healthy account), so this never writes needlessly.
    """
    async def _work(session: AsyncSession) -> int:
        previous = (await session.execute(
            select(XtreamAccount.outage_strikes).where(XtreamAccount.id == account_id)
        )).scalar()
        if not previous:
            return 0
        await session.execute(
            update(XtreamAccount)
            .where(XtreamAccount.id == account_id)
            .values(outage_strikes=0, outage_since=None)
        )
        await session.commit()
        return int(previous)

    previous = await write_with_retry(
        _work, session_factory=session_factory or worker_session_factory,
        op="outage_clear",
    )
    if previous:
        account_outage_strikes.labels(account_id=account_id).set(0)
        invalidate_mask_cache()
        logger.info(
            "Account %s: provider recovered after %d failed validation run(s) — "
            "its media are visible again",
            account_id, previous,
        )


async def list_outages(db: AsyncSession) -> list[dict]:
    """Accounts with a live outage streak, worst first (admin banner).

    Includes accounts still BELOW the masking threshold (strikes >= 1): the
    operator wants to see a provider going bad before its catalogue vanishes.
    """
    rows = (await db.execute(
        select(
            XtreamAccount.id, XtreamAccount.label,
            XtreamAccount.outage_strikes, XtreamAccount.outage_since,
        )
        .where(XtreamAccount.outage_strikes > 0)
        .order_by(XtreamAccount.outage_strikes.desc(), XtreamAccount.id.asc())
    )).all()
    threshold = settings.ACCOUNT_OUTAGE_STRIKES
    return [
        {
            "account_id": acc_id,
            "label": label or acc_id,
            "strikes": int(strikes or 0),
            "since": since,
            "masked": bool(strikes and strikes >= threshold
                           and settings.ACCOUNT_OUTAGE_MASK_ENABLED),
        }
        for acc_id, label, strikes, since in rows
    ]


async def masked_server_ids(db: AsyncSession) -> frozenset[str]:
    """Return the `server_id`s whose media must be hidden from app reads.

    Empty when masking is disabled (`ACCOUNT_OUTAGE_MASK_ENABLED=false`) —
    strikes are still counted, logged and exported as a metric, but nothing
    is hidden.

    The result is memoised for `_MASK_TTL_SECONDS`, keyed on the bound engine
    so isolated test databases (and, in theory, any second engine) can never
    serve each other's answer — same scoping rationale as
    `media_service`'s unified-groups cache key.
    """
    if not settings.ACCOUNT_OUTAGE_MASK_ENABLED:
        return frozenset()

    threshold = settings.ACCOUNT_OUTAGE_STRIKES
    if threshold <= 0:
        return frozenset()

    key = (id(db.get_bind()),)
    cached = _masked_cache.get(key, None)
    if cached is not None:
        return cached

    rows = (await db.execute(
        select(XtreamAccount.id).where(XtreamAccount.outage_strikes >= threshold)
    )).scalars().all()
    masked = frozenset(build_server_id(account_id) for account_id in rows)
    _masked_cache.set(key, masked)
    return masked
