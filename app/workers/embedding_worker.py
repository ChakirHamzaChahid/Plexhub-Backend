"""One-shot worker: re-embed all ai_tmdb_cache rows where embedded_at IS NULL.

Triggered exclusively by POST /api/ai/embed/rebuild (J5). Never auto-runs at boot
(R5). State stored in module-level _ai_jobs OrderedDict, FIFO eviction at cap 100.
Pagination cursor-based (tmdb_id > :cursor) to bound memory on large queues.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

from sqlalchemy import text

from app.db.database import async_session_factory
from app.services.embedding_service import EmbeddingUnavailableError, embed_passages
from app.services.recommendation_service import _serialize_vec

logger = logging.getLogger("plexhub.ai.worker")

PAGE_SIZE = 50
JOBS_CAP = 100

_ai_jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_job_id() -> str:
    """Format imposed by addendum B.5/J5: f'ai_rebuild_{now_ms}'."""
    return f"ai_rebuild_{_now_ms()}"


def register_job(job_id: str, payload: dict[str, Any]) -> None:
    """Insert a job entry with FIFO eviction when len exceeds JOBS_CAP."""
    if job_id in _ai_jobs:
        # update in place, keep insertion order
        _ai_jobs[job_id].update(payload)
        return
    while len(_ai_jobs) >= JOBS_CAP:
        _ai_jobs.popitem(last=False)  # evict oldest
    _ai_jobs[job_id] = payload


def get_job(job_id: str) -> dict[str, Any] | None:
    """Lookup a job. Does not move it (FIFO eviction stays insertion-order)."""
    return _ai_jobs.get(job_id)


async def run_embedding_rebuild(job_id: str) -> None:
    """Background coroutine: scan ai_tmdb_cache WHERE embedded_at IS NULL and embed.

    Pagination cursor-based on tmdb_id (no OFFSET), PAGE_SIZE per batch.
    Wraps everything in try/except so that status -> 'failed' on unexpected crash.
    """
    register_job(job_id, {
        "status": "running",
        "processed": 0,
        "errors": 0,
        "last_error": None,
        "started_at": _now_ms(),
        "finished_at": None,
    })
    cursor = 0
    try:
        while True:
            # Read a page using a fresh session, then close before embedding
            async with async_session_factory() as session:
                rows = (await session.execute(
                    text(
                        "SELECT tmdb_id, media_type, overview, genres "
                        "FROM ai_tmdb_cache "
                        "WHERE embedded_at IS NULL AND tmdb_id > :cursor "
                        "ORDER BY tmdb_id LIMIT :limit"
                    ),
                    {"cursor": cursor, "limit": PAGE_SIZE},
                )).fetchall()
            if not rows:
                break

            for tmdb_id, media_type, overview, genres in rows:
                cursor = tmdb_id  # advance cursor regardless of outcome
                overview = (overview or "").strip()
                genres = (genres or "").strip()
                if not overview and not genres:
                    # skip empty content — never embedded
                    continue
                doc = f"{overview}\n{genres}".strip()
                try:
                    [vec] = await embed_passages([doc])
                except EmbeddingUnavailableError as exc:
                    # Model unavailable — fail the job to surface the issue
                    _ai_jobs[job_id]["status"] = "failed"
                    _ai_jobs[job_id]["last_error"] = f"embedding unavailable: {exc}"
                    _ai_jobs[job_id]["finished_at"] = _now_ms()
                    logger.error("rebuild job %s aborted: model unavailable", job_id)
                    return
                except Exception as exc:
                    _ai_jobs[job_id]["errors"] += 1
                    _ai_jobs[job_id]["last_error"] = str(exc)
                    logger.warning("embed failed tmdb_id=%s: %s", tmdb_id, exc)
                    continue
                now = _now_ms()
                async with async_session_factory() as session:
                    # DELETE-then-INSERT on ai_embeddings (vec0 forbids UPSERT)
                    await session.execute(
                        text("DELETE FROM ai_embeddings WHERE tmdb_id = :t"),
                        {"t": tmdb_id},
                    )
                    await session.execute(
                        text("INSERT INTO ai_embeddings(tmdb_id, embedding) VALUES(:t, :v)"),
                        {"t": tmdb_id, "v": _serialize_vec(vec)},
                    )
                    await session.execute(
                        text("UPDATE ai_tmdb_cache SET embedded_at = :n WHERE tmdb_id = :t"),
                        {"n": now, "t": tmdb_id},
                    )
                    await session.commit()
                _ai_jobs[job_id]["processed"] += 1
        _ai_jobs[job_id]["status"] = "done"
        _ai_jobs[job_id]["finished_at"] = _now_ms()
    except Exception as exc:
        _ai_jobs[job_id]["status"] = "failed"
        _ai_jobs[job_id]["last_error"] = str(exc)
        _ai_jobs[job_id]["finished_at"] = _now_ms()
        logger.exception("rebuild job %s crashed", job_id)


async def enqueue_rebuild() -> str:
    """Create a new job_id, register pending, fire-and-forget via create_background_task."""
    from app.utils.tasks import create_background_task

    job_id = _make_job_id()
    register_job(job_id, {
        "status": "pending",
        "processed": 0,
        "errors": 0,
        "last_error": None,
        "started_at": _now_ms(),
        "finished_at": None,
    })
    create_background_task(run_embedding_rebuild(job_id), name=f"ai-rebuild-{job_id}")
    return job_id
