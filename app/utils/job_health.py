"""Freshness tracking for scheduled jobs + master-election visibility.

Context (AUDIT-P8-001 volet b / AUDIT-P8-005,
`docs/plans/2026-07-26-refacto-audit-v1-plan.md` §VAGUE 5 S5.1): a master
process that FREEZES (starved event loop, deadlock, a pending task that never
completes — dette CR-C01) is indistinguishable, from the outside, from a
healthy-but-quiet instance. `GET /api/health` keeps responding (the loop
still breathes a little), and the 5 pre-existing business metrics stay silent
either way. The only prior signal was a log line — this module is the
freshness signal the audit asks for: a per-job "last successful run"
timestamp that an alert can compare against `time()`, plus whether THIS
process currently holds the `fcntl` master election (piège §9-7).

Deliberately process-local, in-memory, no persistence (same posture as
`app.services.job_registry`, house-law piège #7): a slave process's jobs
never run at all, so its gauges/timestamps for those jobs simply never
update — which is the CORRECT signal (no false freshness on a slave, per the
plan's explicit test requirement).

Two mirrored data stores:
  - the Prometheus gauges in `app.utils.metrics` (declared, zero-initialised,
    S1.4) — what `/metrics` exposes for alerting;
  - a plain in-process dict/bool here — what `GET /api/health` reads
    directly (there is no public "read a Gauge's current value back" API in
    `prometheus_client`, and `/api/health` shouldn't reach into the registry).

`app/main.py` calls only `mark_job_success`/`set_master`/`track_job` (see
module docstrings below) — no business logic lives in `main.py` itself
(house-law verrou V-1, ≤10 added lines there).
"""
from __future__ import annotations

import functools
import logging
from typing import Awaitable, Callable, TypeVar

from app.utils import metrics
from app.utils.time import now_ms

logger = logging.getLogger("plexhub")

_T = TypeVar("_T")

# Process-local mirror of `metrics.pipeline_last_success_timestamp_seconds`,
# in epoch MILLISECONDS (consistent with the rest of the codebase's `now_ms()`
# convention, e.g. `XtreamAccount.last_synced_at`) — `GET /api/health` reads
# this directly instead of poking the Prometheus registry.
_last_success_ms: dict[str, int] = {}

# Process-local mirror of `metrics.is_master`. Defaults to False: a process
# that never runs `lifespan()` (e.g. most unit tests, `tests/conftest.py`'s
# `api_client` fixture) or that lost the `fcntl` election reports "not master".
_is_master = False


def mark_job_success(job: str) -> None:
    """Record that scheduled job ``job`` just completed successfully.

    Call this ONLY from the success path of a job body — after its last
    meaningful step, never at job start and never from an `except`/`finally`
    block. A job whose body raises must NOT update its timestamp: the gauge
    going stale (``time() - gauge`` growing unbounded) IS the alert signal
    AUDIT-P8-001 asks for. `job` must be one of the closed, zero-initialised
    names declared in `metrics._PIPELINE_JOB_NAMES` — this function does not
    guard against typos (a stray label would create an UN-initialised series,
    caught by `tests/test_metrics_registry.py` for the known set).
    """
    ts_ms = now_ms()
    _last_success_ms[job] = ts_ms
    metrics.pipeline_last_success_timestamp_seconds.labels(job=job).set(ts_ms / 1000)


def last_success_ms(job: str) -> int | None:
    """Epoch-ms timestamp of `job`'s last recorded success, or `None` if it
    has never succeeded in this process (never run yet, always failed so
    far, or this process is a slave/test double that never runs it at all —
    house-law piège #7)."""
    return _last_success_ms.get(job)


def set_master(value: bool) -> None:
    """Record whether this process currently holds the `fcntl.flock` master
    election (`app.main.lifespan`). Reflects the REAL election result
    verbatim — this module never re-decides mastership, it only publishes
    what `lifespan()` already computed (a non-lock `OSError` at election time
    — AUDIT-P1-003 — already falls back to `is_master=False` upstream; this
    function must not paper over that)."""
    global _is_master
    _is_master = value
    metrics.is_master.set(1.0 if value else 0.0)


def is_master() -> bool:
    """Whether this process currently holds the master election. `False` by
    default (piège §9-7: always `False` on an un-shimmed Windows dev run,
    since `fcntl` doesn't exist there — this is expected, not a bug)."""
    return _is_master


def track_job(
    job: str,
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    """Decorator wrapping an async scheduled-job callable so a successful
    return marks ``job``'s freshness, and an exception propagates UNCHANGED
    (never swallowed here — the job's own caller, e.g. APScheduler or a
    `try/except` in `app/main.py`, keeps its existing error handling).

    Used at the `scheduler.add_job(...)` call site in `app/main.py` to
    instrument job bodies that live in OTHER modules (`health_check_worker
    .run`, the local `_cleanup_stale_epg`/`_subtitle_cache_cleanup`/
    `_scheduled_backup`/`_scheduled_plex_sync` closures) without editing
    those modules or restructuring their bodies (verrou V-1: no reach-in,
    ≤10 added lines in `main.py`).
    """

    def decorator(func: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> _T:
            result = await func(*args, **kwargs)
            mark_job_success(job)
            return result

        return wrapper

    return decorator
