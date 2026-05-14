"""AI recommendation API.

Exposes :
    POST /api/ai/rank          Rank candidates by cosine sim to a single ref.
    POST /api/ai/rank-multi    Rank candidates by cosine sim to a weighted
                               centroid of N refs (J4).

The router has a module-level dependency on verify_api_key — every endpoint
mounted here is auth-protected. Future J5/J6 endpoints (/embed/*) appended
under this router inherit the same guard.
"""
from __future__ import annotations

import logging
import os
from typing import Literal

import psutil
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_api_key
from app.db.database import async_session_factory, get_db
from app.services.embedding_service import EmbeddingUnavailableError, weighted_centroid
from app.services.recommendation_service import (
    cosine_rank,
    hydrate_misses,
    load_cached_vectors,
)
from app.services.tmdb_service import tmdb_service
from app.workers.embedding_worker import enqueue_rebuild, get_job

logger = logging.getLogger("plexhub.ai.api")

router = APIRouter(
    prefix="/api/ai",
    tags=["ai"],
    dependencies=[Depends(verify_api_key)],
)


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas (camelCase aliases via alias_generator)
# ──────────────────────────────────────────────────────────────────────────────

_CAMEL_CONFIG = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MediaRef(BaseModel):
    """Reference to a movie or TV show. At least one of tmdb_id / imdb_id required."""

    model_config = _CAMEL_CONFIG

    tmdb_id: int | None = None
    imdb_id: str | None = None

    @model_validator(mode="after")
    def _at_least_one_id(self) -> "MediaRef":
        if self.tmdb_id is None and self.imdb_id is None:
            raise ValueError("tmdb_id or imdb_id required")
        return self


class RankRequest(BaseModel):
    model_config = _CAMEL_CONFIG

    ref: MediaRef
    candidates: list[MediaRef]
    limit: int = Field(default=20, ge=1, le=200)
    media_type: Literal["movie", "tv"] = "movie"


class RankMultiRequest(BaseModel):
    """Request body for POST /api/ai/rank-multi.

    refs / candidates: lists of MediaRef (tmdb_id or imdb_id).
    exclude_refs (default True): when True, any tmdb_id present in refs is
        removed from candidates BEFORE ranking so `limit` stays coherent.
    """

    model_config = _CAMEL_CONFIG

    refs: list[MediaRef]
    candidates: list[MediaRef]
    limit: int = Field(default=20, ge=1, le=200)
    media_type: Literal["movie", "tv"] = "movie"
    exclude_refs: bool = True


class RankedItem(BaseModel):
    model_config = _CAMEL_CONFIG

    tmdb_id: int
    imdb_id: str | None = None
    score: float


class RankResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    ranked: list[RankedItem]
    cache_hits: int
    cache_misses: int
    cache_misses_dropped: int
    resolution_failed: int


class RebuildResponse(BaseModel):
    model_config = _CAMEL_CONFIG
    job_id: str


class JobStatusResponse(BaseModel):
    model_config = _CAMEL_CONFIG
    job_id: str
    status: Literal["pending", "running", "done", "failed"]
    processed: int
    errors: int
    last_error: str | None
    started_at: int
    finished_at: int | None


class EmbedStatus(BaseModel):
    model_config = _CAMEL_CONFIG
    total_embeddings: int
    total_cache_entries: int
    pending_embed: int
    last_indexed_at: int | None
    rss_mb: int                # C6: int, pas float
    model_loaded: bool
    model_name: str
    embedding_dim: int
    vec_loaded: bool
    vec_error: str


# ──────────────────────────────────────────────────────────────────────────────
# Reference resolution helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _resolve_one(ref: MediaRef, media_type: str) -> int | None:
    """Resolve a single MediaRef to a tmdb_id. Returns None on failure."""
    if ref.tmdb_id is not None:
        return ref.tmdb_id
    if ref.imdb_id is None:
        return None
    return await tmdb_service.find_by_imdb_id(ref.imdb_id, media_type)  # type: ignore[arg-type]


async def _resolve_refs(
    refs: list[MediaRef],
    media_type: str,
) -> tuple[list[int], dict[int, str | None], int]:
    """Resolve a list of MediaRef to (tmdb_ids, tmdb->imdb mapping, failed_count).

    Duplicate tmdb_ids are dropped (first occurrence wins). imdb_id mapping
    keeps the first imdb_id seen per tmdb_id for response enrichment.
    """
    seen: set[int] = set()
    tmdb_ids: list[int] = []
    mapping: dict[int, str | None] = {}
    failed = 0
    for ref in refs:
        tid = await _resolve_one(ref, media_type)
        if tid is None:
            failed += 1
            continue
        if tid in seen:
            continue
        seen.add(tid)
        tmdb_ids.append(tid)
        mapping[tid] = ref.imdb_id
    return tmdb_ids, mapping, failed


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/ai/rank
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/rank", response_model=RankResponse, response_model_by_alias=True)
async def rank(
    payload: RankRequest,
    db: AsyncSession = Depends(get_db),
) -> RankResponse:
    """Rank candidates by cosine similarity to a single reference media."""
    # 1. Resolve refs
    ref_ids, _ref_map, ref_failed = await _resolve_refs([payload.ref], payload.media_type)
    if not ref_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ref unresolvable",
        )
    ref_tmdb = ref_ids[0]

    cand_ids, cand_mapping, cand_failed = await _resolve_refs(
        payload.candidates, payload.media_type
    )
    resolution_failed = ref_failed + cand_failed

    # 2. Cache lookup (ref + candidates combined for a single SQL query)
    all_ids = list({ref_tmdb, *cand_ids})
    cached = await load_cached_vectors(db, all_ids)
    cache_hits = len(cached)
    miss_ids = [tid for tid in all_ids if tid not in cached]

    # 3. Hydrate misses (cap 20 internally, per-task timeout)
    try:
        hydrated, stats = await hydrate_misses(
            miss_ids, payload.media_type, async_session_factory
        )
    except EmbeddingUnavailableError as exc:
        logger.warning("embedding unavailable during /rank: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI model unavailable",
        ) from exc

    cache_misses = stats.hydrated
    cache_misses_dropped = stats.dropped
    vectors = {**cached, **hydrated}

    # The ref must have a vector — otherwise nothing meaningful can be ranked.
    if ref_tmdb not in vectors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ref embedding unavailable",
        )

    # 4. Rank
    ref_vec = vectors[ref_tmdb]
    cand_set = set(cand_ids)
    cand_vecs = {
        tid: vec for tid, vec in vectors.items() if tid in cand_set and tid != ref_tmdb
    }
    ranked_pairs = cosine_rank(ref_vec, cand_vecs, payload.limit)

    ranked_items = [
        RankedItem(tmdb_id=tid, imdb_id=cand_mapping.get(tid), score=score)
        for tid, score in ranked_pairs
    ]
    return RankResponse(
        ranked=ranked_items,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        cache_misses_dropped=cache_misses_dropped,
        resolution_failed=resolution_failed,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/ai/rank-multi
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/rank-multi", response_model=RankResponse, response_model_by_alias=True)
async def rank_multi(
    payload: RankMultiRequest,
    db: AsyncSession = Depends(get_db),
) -> RankResponse:
    """Rank candidates by cosine similarity to the L2-normalized weighted
    centroid of refs.

    Weights decay 1.0, 0.9, 0.8, ..., clamped to min 0.1 (so the 10th+ ref still
    contributes). When exclude_refs is True (default), tmdb_ids in refs are
    removed from candidates BEFORE ranking (so limit stays coherent).
    """
    # 1. Resolve refs
    if not payload.refs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="refs cannot be empty",
        )

    ref_ids, _ref_mapping, ref_failed = await _resolve_refs(payload.refs, payload.media_type)
    if not ref_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no refs resolved",
        )

    cand_ids_raw, cand_mapping, cand_failed = await _resolve_refs(
        payload.candidates, payload.media_type
    )
    resolution_failed = ref_failed + cand_failed

    # 2. exclude_refs : drop refs from candidates BEFORE the ranking
    if payload.exclude_refs:
        ref_set = set(ref_ids)
        cand_ids = [tid for tid in cand_ids_raw if tid not in ref_set]
    else:
        cand_ids = cand_ids_raw

    # 3. Cache lookup union (refs + remaining candidates)
    all_ids = list({*ref_ids, *cand_ids})
    cached = await load_cached_vectors(db, all_ids)
    cache_hits = len(cached)
    miss_ids = [tid for tid in all_ids if tid not in cached]

    # 4. Hydrate
    try:
        hydrated, stats = await hydrate_misses(
            miss_ids, payload.media_type, async_session_factory
        )
    except EmbeddingUnavailableError as exc:
        logger.warning("embedding unavailable during /rank-multi: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI model unavailable",
        ) from exc

    vectors = {**cached, **hydrated}

    # 5. Build the weighted centroid (decay 1.0, 0.9, ..., min 0.1)
    available_ref_ids = [tid for tid in ref_ids if tid in vectors]
    if not available_ref_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no ref embeddings available",
        )
    ref_vecs = [vectors[tid] for tid in available_ref_ids]
    weights = [max(0.1, 1.0 - 0.1 * i) for i in range(len(ref_vecs))]
    query_vec = weighted_centroid(ref_vecs, weights)

    # 6. Rank candidates
    cand_set = set(cand_ids)
    cand_vecs = {tid: vec for tid, vec in vectors.items() if tid in cand_set}
    ranked_pairs = cosine_rank(query_vec, cand_vecs, payload.limit)

    ranked_items = [
        RankedItem(tmdb_id=tid, imdb_id=cand_mapping.get(tid), score=score)
        for tid, score in ranked_pairs
    ]
    return RankResponse(
        ranked=ranked_items,
        cache_hits=cache_hits,
        cache_misses=stats.hydrated,
        cache_misses_dropped=stats.dropped,
        resolution_failed=resolution_failed,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/ai/embed/rebuild  +  GET /api/ai/embed/jobs/{job_id}
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/embed/rebuild",
    response_model=RebuildResponse,
    response_model_by_alias=True,
    status_code=202,
)
async def embed_rebuild() -> RebuildResponse:
    """Trigger a background re-embedding of all ai_tmdb_cache rows pending an embed.

    Returns immediately with 202 + jobId. Poll GET /embed/jobs/{jobId} for progress.
    Never auto-runs at boot — only via this endpoint (R5).
    """
    job_id = await enqueue_rebuild()
    return RebuildResponse(job_id=job_id)


@router.get(
    "/embed/jobs/{job_id}",
    response_model=JobStatusResponse,
    response_model_by_alias=True,
)
async def embed_job_status(job_id: str) -> JobStatusResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatusResponse(job_id=job_id, **job)


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/ai/embed/status
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/embed/status",
    response_model=EmbedStatus,
    response_model_by_alias=True,
)
async def embed_status(db: AsyncSession = Depends(get_db)) -> EmbedStatus:
    """Diagnostic snapshot of the AI subsystem.

    Returns counts (embeddings, cache rows, pending), the most recent embedded_at,
    process RSS, and load state of the fastembed model and sqlite-vec extension.
    """
    from sqlalchemy import text
    from app.services import embedding_service
    from app.db.database import _VEC_LOADED

    total_embeddings = (
        await db.execute(text("SELECT COUNT(*) FROM ai_embeddings"))
    ).scalar() or 0
    total_cache_entries = (
        await db.execute(text("SELECT COUNT(*) FROM ai_tmdb_cache"))
    ).scalar() or 0
    pending_embed = (
        await db.execute(text("SELECT COUNT(*) FROM ai_tmdb_cache WHERE embedded_at IS NULL"))
    ).scalar() or 0
    last_indexed_at = (
        await db.execute(text("SELECT MAX(embedded_at) FROM ai_tmdb_cache"))
    ).scalar()

    rss_bytes = psutil.Process(os.getpid()).memory_info().rss
    rss_mb = int(rss_bytes // (1024 * 1024))

    return EmbedStatus(
        total_embeddings=int(total_embeddings),
        total_cache_entries=int(total_cache_entries),
        pending_embed=int(pending_embed),
        last_indexed_at=int(last_indexed_at) if last_indexed_at is not None else None,
        rss_mb=rss_mb,
        model_loaded=embedding_service._model is not None,
        model_name=embedding_service.MODEL_NAME,
        embedding_dim=embedding_service.EMBEDDING_DIM,
        vec_loaded=bool(_VEC_LOADED.get("ok")),
        vec_error=str(_VEC_LOADED.get("error") or ""),
    )

