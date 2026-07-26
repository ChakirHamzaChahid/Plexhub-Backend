"""Shared in-memory registry for the 5 fire-and-forget triggers exposed by
``app/api/sync.py`` (``/xtream``, ``/xtream/all``, ``/enrichment``,
``/validate-streams``, ``/full-pipeline``).

Extracted from ``app/workers/sync_worker.py`` (formerly a private
``_sync_jobs`` dict + ``_record_sync_job`` helper, consumed by
``sync_account`` only) so that ALL five triggers can register and update a
job under the SAME ``jobId`` the router hands back to the caller.

Why this exists (AUDIT-P5-001 / AUDIT-P5-008, ADR 0004 Décision 2): the
handshake was broken end-to-end -- the router fabricated
``f"sync_{account_id}_{id(task)}"`` while the worker registered
``f"sync_{account_id}_{now_ms()}"`` under a private tracker that only
``sync_account`` ever wrote to. The other 4 triggers never registered
anything at all, and ``GET /status/{job_id}`` returned ``200
{"status": "unknown"}`` for ANY unknown id -- indistinguishable from a
real, still-pending job.

Contract:
  - The **router** creates the job entry (``create_job``) synchronously,
    BEFORE scheduling the background task and returning the 202 response --
    so a ``GET /status/{jobId}`` issued right after the trigger response is
    guaranteed to resolve to a known job, never racing the background
    task's own start (asyncio does not guarantee a freshly-created task has
    run even one line by the time the creator's coroutine yields/returns).
  - The background task (worker) updates the SAME entry via ``update_job``,
    keyed by the jobId the router handed it.
  - ``get_job``/``get_all_jobs`` never raise; the router turns a ``None``
    into a 404 (id unknown = never registered, evicted by the FIFO cap, or
    the process-local registry lost across a worker/master boundary or a
    restart -- dette AUDIT-P1-008, assumed, house-law piege #7: job state
    is NOT shared across processes/restarts).

Process-local, in-memory, FIFO-bounded (``_MAX_JOBS``) -- same semantics as
the tracker this replaces: no persistence, no cross-process visibility.
"""
from __future__ import annotations

from typing import Any

from app.utils.time import now_ms

# Cap kept identical to the pre-extraction tracker (`sync_worker._MAX_SYNC_JOBS`).
_MAX_JOBS = 100

_jobs: dict[str, dict[str, Any]] = {}

_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def _evict_if_over_cap() -> None:
    """FIFO eviction: drop the oldest entries (by first-registration
    insertion order -- Python dicts preserve it, and re-assigning an
    existing key does NOT move it) once the cap is exceeded."""
    if len(_jobs) > _MAX_JOBS:
        excess = len(_jobs) - _MAX_JOBS
        for key in list(_jobs)[:excess]:
            del _jobs[key]


def set_job(job_id: str, data: dict[str, Any]) -> None:
    """Raw insert/overwrite of a job entry (FIFO-evicting beyond the cap).

    Low-level primitive -- kept as the building block for ``create_job``/
    ``update_job`` below, and for backward compatibility with the
    pre-extraction ``sync_worker._record_sync_job`` call sites/tests.
    Prefer ``create_job``/``update_job`` for new call sites.
    """
    _jobs[job_id] = data
    _evict_if_over_cap()


def create_job(job_id: str, *, phase: str | None = None) -> None:
    """Register a brand-new job as ``processing``.

    Called by the router at trigger time (synchronously, before the
    background task is scheduled) so the returned ``jobId`` is immediately
    resolvable by ``GET /status/{jobId}``.
    """
    set_job(job_id, {
        "status": "processing",
        "progress": {},
        "phase": phase,
        "started_at": now_ms(),
        "finished_at": None,
        "error": None,
    })


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    phase: str | None = None,
    error: str | None = None,
) -> None:
    """Merge fields onto an existing entry.

    If the entry doesn't exist yet (a caller invoked ``update_job`` without
    ever going through ``create_job`` -- e.g. ``sync_account`` called
    directly by ``run_all_accounts``/the scheduler, outside of an HTTP
    trigger), a fresh entry is created with sane defaults first.

    A terminal ``status`` (``completed``/``failed``) stamps ``finished_at``
    (once -- it is never overwritten by a later call).
    """
    current = _jobs.get(job_id) or {
        "status": "processing",
        "progress": {},
        "phase": phase,
        "started_at": now_ms(),
        "finished_at": None,
        "error": None,
    }
    updated = dict(current)
    if status is not None:
        updated["status"] = status
    if progress is not None:
        updated["progress"] = progress
    if phase is not None:
        updated["phase"] = phase
    if error is not None:
        updated["error"] = error
    if updated.get("status") in _TERMINAL_STATUSES and updated.get("finished_at") is None:
        updated["finished_at"] = now_ms()
    set_job(job_id, updated)


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return the raw job dict, or ``None`` if unknown (never registered,
    evicted, or process-local state lost). Callers turn ``None`` into a
    404 -- this module never raises."""
    return _jobs.get(job_id)


def get_all_jobs() -> list[dict[str, Any]]:
    """Return all tracked jobs, most recently registered first."""
    return [
        {"job_id": k, **v}
        for k, v in reversed(list(_jobs.items()))
    ]
