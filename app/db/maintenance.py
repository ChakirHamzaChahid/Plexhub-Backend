"""SQLite planner-statistics maintenance (AUDIT-P3-001, docs/audit/v1/30-perf.md).

Without `sqlite_stat1`, SQLite's query planner has no cardinality
information and picks `ix_media_category_visible` — a boolean index
matching ~90.5% of `media` rows — for virtually every hot list/search/count
query, ignoring the 20 composite indexes migration 015 (CR-P02) already
created (they exist; without stats they're simply never chosen). Measured
on a real 102,721-row/189MB copy of the production DB: films COUNT
113.5ms -> 0.6ms (x188) after one `ANALYZE`; search `LIKE` 116.3ms -> 18.2ms;
one-shot `ANALYZE` cost 196ms.

Two entry points, both **non-fatal** (any exception is logged as a warning,
never raised — a missing/stale stats refresh degrades query plans but must
never break a boot or a scheduled pipeline run):

- `run_analyze(engine)` — one-shot `ANALYZE`, used by migration 023
  (db/migrations.py) at boot and reusable directly in tests.
- `run_sqlite_maintenance(engine)` — `ANALYZE` + `PRAGMA optimize`, called
  from the end of both pipeline coroutines in `app/main.py`
  (`_rebuild_unified_groups`, right after `unified_group_service.rebuild_all`)
  so the stats keep tracking catalog growth between reboots. `PRAGMA
  optimize` is SQLite's own lightweight heuristic ("re-analyze only the
  tables that look like they've drifted") — cheap enough to call
  unconditionally at the end of every pipeline pass, unlike a full
  `ANALYZE` re-run.

Both statements take a normal SQLite write lock, like any other writer
(WAL + `busy_timeout=60s`, house law piège 8). The pipeline call site runs
this while `_PIPELINE_LOCK` is still held (main.py), so there's no
concurrent pipeline writer at that moment; any *unrelated* concurrent
writer (e.g. a request-path download-job insert) simply waits behind the
busy_timeout like it already does for every other write, and vice-versa —
no new contention pattern is introduced. `ANALYZE`/`PRAGMA optimize` never
block the event loop directly: execution goes through the async SQLAlchemy
engine (aiosqlite), which already dispatches the underlying blocking
sqlite3 call to its own driver thread (house law piège 11) — no additional
``asyncio.to_thread`` wrapping is needed here.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("plexhub.db.maintenance")


async def run_analyze(engine: AsyncEngine) -> None:
    """Run a one-shot `ANALYZE`, (re)populating `sqlite_stat1`.

    Idempotent/rejouable by nature: `ANALYZE` only ever fully recomputes
    the `sqlite_stat1`/`sqlite_stat4` bookkeeping tables it owns (never
    appends), so re-running it — on a fresh/empty database or an already
    analyzed one — is always safe and always a no-op from the caller's
    point of view (it may simply have nothing new to learn).

    Never raises: any failure (locked DB past busy_timeout, read-only
    mount, disk full, ...) is logged as a warning and swallowed.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ANALYZE"))
        logger.info("SQLite maintenance: ANALYZE completed")
    except Exception as exc:  # pragma: no cover - defensive, never fatal
        logger.warning("SQLite maintenance: ANALYZE failed (non-fatal): %s", exc)


async def run_sqlite_maintenance(engine: AsyncEngine) -> None:
    """End-of-pipeline maintenance: `ANALYZE` then `PRAGMA optimize`.

    Called once per pipeline pass (scheduled interval AND the boot-time
    initial run, both funnel through `app.main._rebuild_unified_groups`) so
    planner statistics never go as stale as "since the last migration ran".

    Never raises — same non-fatal contract as `run_analyze`; a failure here
    must never take down the pipeline it terminates.
    """
    await run_analyze(engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA optimize"))
        logger.info("SQLite maintenance: PRAGMA optimize completed")
    except Exception as exc:  # pragma: no cover - defensive, never fatal
        logger.warning("SQLite maintenance: PRAGMA optimize failed (non-fatal): %s", exc)
