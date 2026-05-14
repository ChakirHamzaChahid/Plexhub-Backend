"""AI recommendation API.

Currently exposes :
    POST /api/ai/rank    Rank candidate media by cosine similarity to a single ref.

The router has a module-level dependency on verify_api_key — every endpoint
mounted here is auth-protected. Future J4 endpoints (/rank-multi) and J5/J6
endpoints (/embed/*) appended under this router inherit the same guard.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_api_key
from app.db.database import async_session_factory, get_db
from app.services.embedding_service import EmbeddingUnavailableError
from app.services.recommendation_service import (
    cosine_rank,
    hydrate_misses,
    load_cached_vectors,
)
from app.services.tmdb_service import tmdb_service

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
