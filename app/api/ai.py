"""AI recommendation API.

Exposes :
    POST /api/ai/rank          Rank candidates by cosine sim to a single ref.
    POST /api/ai/rank-multi    Rank candidates by cosine sim to a weighted
                               centroid of N refs (J4).
    POST /api/ai/describe      Generate a recommendation blurb via Ollama/gemma4.
    POST /api/ai/chat          Free-form LLM chat about a media item.
    GET  /api/ai/llm/status    Health check for the Ollama backend.

The router has a module-level dependency on verify_api_key — every endpoint
mounted here is auth-protected.
"""
from __future__ import annotations

import asyncio
import hashlib
import httpx
import logging
import os
import time
from typing import AsyncIterator, Literal

import psutil
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_api_key
from app.db.database import async_session_factory, get_db
from app.models.database import AiSubtitleCache
from app.utils.db_retry import commit_with_retry
from app.utils.time import now_ms
from app.services.embedding_service import EmbeddingUnavailableError, weighted_centroid
from app.services.recommendation_service import (
    cosine_rank,
    hydrate_misses,
    load_cached_vectors,
    semantic_search,
    semantic_search_with_overview,
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
    explain: bool = False


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
    explain: bool = False


class RankedItem(BaseModel):
    model_config = _CAMEL_CONFIG

    tmdb_id: int
    imdb_id: str | None = None
    score: float
    explanation: str | None = None


class RankResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    ranked: list[RankedItem]
    cache_hits: int
    cache_misses: int
    cache_misses_dropped: int
    resolution_failed: int


class SearchRequest(BaseModel):
    """Request body for POST /api/ai/search."""

    model_config = _CAMEL_CONFIG

    query: str = Field(min_length=1)
    media_type: Literal["movie", "tv"] | None = None
    limit: int = Field(default=20, ge=1, le=50)


class SearchResult(BaseModel):
    model_config = _CAMEL_CONFIG

    tmdb_id: int
    title: str | None = None
    media_type: str
    score: float


class SearchResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    results: list[SearchResult]
    query_used: str
    model: str


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
# F5 – "why recommended" explanation constants + helper
# ──────────────────────────────────────────────────────────────────────────────

# Maximum number of top results that receive LLM-generated explanations per
# /rank or /rank-multi call (bounds Ollama cost; the rest keep explanation=None).
EXPLAIN_CAP = 5

# Max concurrency for concurrent Ollama explain calls (semaphore).
_EXPLAIN_CONCURRENCY = 4

# Per-call timeout for explanation generation (best-effort; failure -> None).
_EXPLAIN_TIMEOUT_S = 20.0


async def _fetch_titles_from_cache(
    db: AsyncSession,
    tmdb_ids: list[int],
) -> dict[int, str | None]:
    """Read title column from ai_tmdb_cache for the given tmdb_ids.

    Returns {tmdb_id: title_or_None}. IDs absent from the cache are not included.
    """
    if not tmdb_ids:
        return {}
    from sqlalchemy import text as _text
    placeholders = ",".join(f":id{i}" for i in range(len(tmdb_ids)))
    params = {f"id{i}": tid for i, tid in enumerate(tmdb_ids)}
    sql = _text(
        f"SELECT tmdb_id, title FROM ai_tmdb_cache WHERE tmdb_id IN ({placeholders})"
    )
    rows = (await db.execute(sql, params)).fetchall()
    return {row[0]: row[1] for row in rows}


async def _explain_items(
    items: list[RankedItem],
    ref_titles: list[str | None],
    db: AsyncSession,
) -> None:
    """Populate explanation on the first EXPLAIN_CAP items (in-place, best-effort).

    Fetches candidate titles from ai_tmdb_cache, then launches up to
    _EXPLAIN_CONCURRENCY concurrent Ollama calls. Any failure or timeout leaves
    the item's explanation as None.  Never raises — explain is a bonus feature.

    ref_titles: display names for the reference(s) used to build the prompt.
    items: the ranked list (modified in-place); only [:EXPLAIN_CAP] are touched.
    """
    from app.services import ollama_service as _ollama

    to_explain = items[:EXPLAIN_CAP]
    if not to_explain:
        return

    # Build a readable reference string for the prompt.
    clean_refs = [t for t in ref_titles if t]
    if clean_refs:
        ref_label = " / ".join(clean_refs)
    else:
        ref_label = "le(s) titre(s) de référence"

    # Fetch candidate titles from cache (one SQL call).
    cand_ids = [item.tmdb_id for item in to_explain]
    titles_map = await _fetch_titles_from_cache(db, cand_ids)

    sem = asyncio.Semaphore(_EXPLAIN_CONCURRENCY)

    async def _one(item: RankedItem) -> None:
        cand_title = titles_map.get(item.tmdb_id) or f"tmdb:{item.tmdb_id}"
        prompt = (
            f"En une seule phrase courte (25 mots maximum), explique en français pourquoi "
            f"'{cand_title}' est recommandé à quelqu'un qui a aimé '{ref_label}'. "
            f"Commence par 'Car' ou 'Parce que'. Ne répète pas les titres entiers dans la phrase."
        )
        async with sem:
            try:
                text_out = await asyncio.wait_for(
                    _ollama.generate(prompt),
                    timeout=_EXPLAIN_TIMEOUT_S,
                )
                item.explanation = text_out.strip() or None
            except Exception as exc:
                logger.debug(
                    "explain skipped for tmdb_id=%s (%s: %s)",
                    item.tmdb_id, type(exc).__name__, exc,
                )
                # Leave item.explanation = None (best-effort).

    await asyncio.gather(*(_one(item) for item in to_explain), return_exceptions=True)


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

    # F5: optional "why recommended" explanations (best-effort, never 503 on failure).
    if payload.explain and ranked_items:
        ref_titles_map = await _fetch_titles_from_cache(db, [ref_tmdb])
        ref_title = ref_titles_map.get(ref_tmdb)
        await _explain_items(ranked_items, [ref_title], db)

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

    # F5: optional "why recommended" explanations (best-effort, never 503 on failure).
    if payload.explain and ranked_items:
        ref_titles_map = await _fetch_titles_from_cache(db, available_ref_ids)
        ref_titles = [ref_titles_map.get(tid) for tid in available_ref_ids]
        await _explain_items(ranked_items, ref_titles, db)

    return RankResponse(
        ranked=ranked_items,
        cache_hits=cache_hits,
        cache_misses=stats.hydrated,
        cache_misses_dropped=stats.dropped,
        resolution_failed=resolution_failed,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/ai/search  (F2 — natural-language semantic search)
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse, response_model_by_alias=True)
async def search(
    payload: SearchRequest,
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """Natural-language semantic search over the embedded catalog.

    Pipeline:
      1. Best-effort query reformulation via Ollama/gemma4 (graceful degrades:
         if Ollama is unreachable or times out, the raw query is used instead —
         this is NOT a 503 path, unlike /describe).
      2. Embed the (possibly reformulated) query via fastembed. An embedding
         model failure propagates as EmbeddingUnavailableError -> 503
         (same contract as /rank).
      3. KNN over ai_embeddings (sqlite-vec vec0 MATCH syntax) joined to
         ai_tmdb_cache for titles.  media_type filter is applied post-KNN
         (over-fetch then truncate) because vec0 has no media_type column.
      4. Return ranked results with camelCase aliases.
    """
    from app.services import ollama_service as _ollama
    from app.config import settings as _cfg

    # 1. Query reformulation (best-effort, never 503 on LLM failure).
    query_used = payload.query
    try:
        rewrite_prompt = (
            "Rewrite the following user search query into ONE concise sentence "
            "that describes the ideal title's themes, tone, and genre. "
            "Reply ONLY with that sentence — no explanations, no quotes. "
            "Keep the same language as the input query.\n\n"
            f"Query: {payload.query}"
        )
        reformulated = await asyncio.wait_for(
            _ollama.generate(rewrite_prompt),
            timeout=15.0,
        )
        reformulated = reformulated.strip()
        if reformulated:
            query_used = reformulated
    except Exception as exc:
        # Ollama down / timeout / any error — degrade gracefully to raw query.
        logger.info("Query reformulation skipped (%s); using raw query.", type(exc).__name__)

    # 2. Embed the query (propagates EmbeddingUnavailableError -> 503).
    from app.services.embedding_service import (
        EmbeddingUnavailableError as _EmbUnavail,
        embed_query as _embed_query,
        DEFAULT_MODEL_NAME,
    )
    try:
        query_vec = await _embed_query(query_used)
    except _EmbUnavail as exc:
        logger.warning("embedding unavailable during /search: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI model unavailable",
        ) from exc

    # 3. KNN + join.
    rows = await semantic_search(
        db=db,
        query_vec=query_vec,
        media_type=payload.media_type,
        limit=payload.limit,
    )

    # 4. Build response.
    results = [
        SearchResult(
            tmdb_id=tmdb_id,
            title=title,
            media_type=row_media_type,
            score=score,
        )
        for tmdb_id, title, row_media_type, score in rows
    ]
    return SearchResponse(
        results=results,
        query_used=query_used,
        model=DEFAULT_MODEL_NAME,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/ai/assistant  (F4 — catalog RAG assistant)
# ──────────────────────────────────────────────────────────────────────────────

_OVERVIEW_TRUNCATE = 300  # chars per title fed to the LLM context window


class AssistantMessage(BaseModel):
    """One turn in the optional conversation history."""

    model_config = _CAMEL_CONFIG

    role: str
    content: str


class AssistantRequest(BaseModel):
    model_config = _CAMEL_CONFIG

    message: str = Field(min_length=1)
    media_type: Literal["movie", "tv"] | None = None
    history: list[AssistantMessage] = Field(default_factory=list)
    limit: int = Field(default=5, ge=1, le=10)


class AssistantSource(BaseModel):
    model_config = _CAMEL_CONFIG

    tmdb_id: int
    title: str | None = None
    media_type: str


class AssistantResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    reply: str
    sources: list[AssistantSource]
    model: str


@router.post("/assistant", response_model=AssistantResponse, response_model_by_alias=True)
async def assistant(
    payload: AssistantRequest,
    db: AsyncSession = Depends(get_db),
) -> AssistantResponse:
    """Catalog RAG assistant: answer the user question grounded in real titles.

    Pipeline:
      1. Embed the user message (EmbeddingUnavailableError -> 503).
      2. Retrieve top-`limit` titles + overviews from ai_embeddings / ai_tmdb_cache.
      3. Build an Ollama /api/chat messages list:
           - system instruction (French, grounding rule: cite only listed titles)
           - catalog context block (title + truncated overview per retrieved item)
           - optional prior conversation history
           - the user message
      4. Call ollama_service.chat. Ollama failure -> 503.
      5. Return reply, sources (grounding titles), model name.
    """
    from app.services import ollama_service as _ollama
    from app.services.embedding_service import (
        EmbeddingUnavailableError as _EmbUnavail,
        embed_query as _embed_query,
        DEFAULT_MODEL_NAME,
    )

    # 1. Embed user message — propagates EmbeddingUnavailableError -> 503.
    try:
        query_vec = await _embed_query(payload.message)
    except _EmbUnavail as exc:
        logger.warning("embedding unavailable during /assistant: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI model unavailable",
        ) from exc

    # 2. KNN retrieval with overview column.
    rows = await semantic_search_with_overview(
        db=db,
        query_vec=query_vec,
        media_type=payload.media_type,
        limit=payload.limit,
    )

    # 3. Build the chat messages list for Ollama.
    #    a. System instruction: answer ONLY using the listed titles, in French.
    catalog_lines: list[str] = []
    sources: list[AssistantSource] = []
    for tmdb_id, title, row_media_type, _score, overview in rows:
        sources.append(AssistantSource(tmdb_id=tmdb_id, title=title, media_type=row_media_type))
        label = title or f"tmdb:{tmdb_id}"
        short_overview = (overview or "")[:_OVERVIEW_TRUNCATE].strip()
        line = f"- {label}"
        if short_overview:
            line += f" : {short_overview}"
        catalog_lines.append(line)

    catalog_block = "\n".join(catalog_lines) if catalog_lines else "(aucun titre disponible)"

    system_content = (
        "Tu es un assistant cinéma et séries TV intégré au catalogue PlexHub. "
        "Réponds UNIQUEMENT en t'appuyant sur les titres du catalogue fourni ci-dessous. "
        "Ne mentionne et n'invente aucun titre absent de cette liste. "
        "Cite les titres par leur nom exact. Réponds en français.\n\n"
        f"Catalogue disponible :\n{catalog_block}"
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]

    #    b. Prior conversation history (mapped as-is — roles are user/assistant/system).
    for turn in payload.history:
        messages.append({"role": turn.role, "content": turn.content})

    #    c. Current user message.
    messages.append({"role": "user", "content": payload.message})

    # 4. Call Ollama /api/chat — LLM is essential here, so failure -> 503.
    try:
        reply = await ollama_service.chat(messages)
    except Exception as exc:
        raise _ollama_503(exc) from exc

    # 5. Return structured response.
    return AssistantResponse(
        reply=reply.strip(),
        sources=sources,
        model=_settings.OLLAMA_MODEL,
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
        model_name=embedding_service._resolve_model_name(),
        embedding_dim=embedding_service.EMBEDDING_DIM,
        vec_loaded=bool(_VEC_LOADED.get("ok")),
        vec_error=str(_VEC_LOADED.get("error") or ""),
    )


# ──────────────────────────────────────────────────────────────────────────────
# LLM endpoints (Ollama / gemma4)
# ──────────────────────────────────────────────────────────────────────────────

from app.services import ollama_service  # noqa: E402 — import after router definition
from app.config import settings as _settings  # noqa: E402
from app.services.subtitle_service import (  # noqa: E402
    SubtitleFormatError,
    SubtitleTooLargeError,
    translate_subtitles,
)


class DescribeRequest(BaseModel):
    """Generate a short recommendation blurb for a media item."""

    model_config = _CAMEL_CONFIG

    title: str
    overview: str | None = None
    genres: list[str] = Field(default_factory=list)
    year: int | None = None
    language: Literal["fr", "en"] = "fr"


class DescribeResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    recommendation: str
    model: str


class ChatMessage(BaseModel):
    model_config = _CAMEL_CONFIG

    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    model_config = _CAMEL_CONFIG

    messages: list[ChatMessage]
    stream: bool = False


class ChatResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    reply: str
    model: str


class LlmStatus(BaseModel):
    model_config = _CAMEL_CONFIG

    healthy: bool
    model: str
    ollama_url: str
    detail: str


def _ollama_503(exc: Exception) -> HTTPException:
    logger.warning("Ollama unavailable: %s", exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="LLM unavailable — Ollama unreachable or model not loaded",
    )


@router.post("/describe", response_model=DescribeResponse, response_model_by_alias=True)
async def describe(payload: DescribeRequest) -> DescribeResponse:
    """Generate a short personalized recommendation for a media item via gemma4."""
    lang = "en français" if payload.language == "fr" else "in English"
    genres_str = ", ".join(payload.genres) if payload.genres else "N/A"
    year_str = f" ({payload.year})" if payload.year else ""

    prompt = (
        f"Tu es un expert en recommandations cinéma et séries TV. "
        f"Rédige {lang} une courte présentation enthousiaste (2-3 phrases max) "
        f"pour convaincre quelqu'un de regarder ce titre :\n\n"
        f"Titre : {payload.title}{year_str}\n"
        f"Genres : {genres_str}\n"
        f"Synopsis : {payload.overview or 'Non disponible'}\n\n"
        f"Sois percutant, sans spoiler, et sans répéter le titre en début de phrase."
    )
    try:
        text = await ollama_service.generate(prompt)
        return DescribeResponse(recommendation=text.strip(), model=_settings.OLLAMA_MODEL)
    except Exception as exc:
        raise _ollama_503(exc) from exc


@router.post("/chat", response_model_by_alias=True)
async def chat(payload: ChatRequest):
    """Free-form chat with gemma4. Supports streaming (stream=true → SSE)."""
    messages = [{"role": m.role, "content": m.content} for m in payload.messages]

    if payload.stream:
        async def _sse() -> AsyncIterator[str]:
            try:
                async for chunk in ollama_service.stream_generate(
                    messages[-1]["content"] if messages else ""
                ):
                    yield f"data: {chunk}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                logger.warning("Ollama stream error: %s", exc)
                yield "data: [ERROR]\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    try:
        reply = await ollama_service.chat(messages)
        return ChatResponse(reply=reply.strip(), model=_settings.OLLAMA_MODEL)
    except Exception as exc:
        raise _ollama_503(exc) from exc


@router.get("/llm/status", response_model=LlmStatus, response_model_by_alias=True)
async def llm_status() -> LlmStatus:
    """Health check for the Ollama LLM backend."""
    ok, detail = await ollama_service.is_healthy()
    return LlmStatus(
        healthy=ok,
        model=_settings.OLLAMA_MODEL,
        ollama_url=_settings.OLLAMA_URL,
        detail=detail,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/ai/subtitles/translate
# ──────────────────────────────────────────────────────────────────────────────


class SubtitleTranslateRequest(BaseModel):
    model_config = _CAMEL_CONFIG

    content: str = Field(min_length=1)      # raw SRT or VTT
    target_lang: str = "fr"                 # JSON: targetLang
    format: str | None = None               # "srt"|"vtt"; auto-detect when null
    source_lang: str | None = None          # JSON: sourceLang (hint)
    media_id: str | None = None             # JSON: mediaId (optional, traceability)
    rating_key: str | None = None           # JSON: ratingKey (optional)


class SubtitleTranslateResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    translated_content: str                 # JSON: translatedContent
    format: str
    cue_count: int                          # JSON: cueCount
    cached: bool
    model: str
    duration_seconds: float                 # JSON: durationSeconds


@router.post(
    "/subtitles/translate",
    response_model=SubtitleTranslateResponse,
    response_model_by_alias=True,
)
async def translate_subtitle(
    payload: SubtitleTranslateRequest,
    db: AsyncSession = Depends(get_db),
) -> SubtitleTranslateResponse:
    """Translate subtitle content (SRT or VTT) via Ollama.

    Cache keyed on SHA-256(content + target_lang + model). Returns cached result
    immediately on hit (durationSeconds=0.0, cached=True). On miss, calls the
    subtitle_service, persists to ai_subtitle_cache, then returns the result.
    """
    # 1. Compute deterministic cache key — fold in format + source_lang so that
    #    the same content requested with a different format override or sourceLang
    #    hint is never served a stale cache entry.
    cache_key = hashlib.sha256(
        "\x00".join([
            payload.content,
            payload.target_lang,
            payload.format or "",
            payload.source_lang or "",
            _settings.OLLAMA_MODEL,
        ]).encode("utf-8")
    ).hexdigest()

    # 2. Cache lookup
    row = (
        await db.execute(select(AiSubtitleCache).where(AiSubtitleCache.cache_key == cache_key))
    ).scalar_one_or_none()
    if row is not None:
        return SubtitleTranslateResponse(
            translated_content=row.translated_content,
            format=row.source_format,
            cue_count=row.cue_count,
            cached=True,
            model=row.model,
            duration_seconds=0.0,
        )

    # 3. Cache miss — call translation service
    t0 = time.perf_counter()
    try:
        result = await translate_subtitles(
            payload.content,
            target_lang=payload.target_lang,
            fmt=payload.format,
            source_lang=payload.source_lang,
        )
    except SubtitleFormatError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc) or "Malformed or unrecognized subtitle content",
        ) from exc
    except SubtitleTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail=str(exc) or "Subtitle file too large",
        ) from exc
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        # Real LLM/transport/timeout failure — mapped to 503 per contract.
        raise _ollama_503(exc) from exc
    except Exception as exc:
        # Unexpected failure: log full traceback so the root cause is diagnosable,
        # then still return 503 to the client (contract unchanged).
        logger.error("subtitle translate failed", exc_info=True)
        raise _ollama_503(exc) from exc

    duration = round(time.perf_counter() - t0, 2)

    # 4. Persist to cache — ignore concurrent race on same cache_key
    try:
        db.add(
            AiSubtitleCache(
                cache_key=cache_key,
                target_lang=payload.target_lang,
                model=result.model,
                source_format=result.fmt,
                cue_count=result.cue_count,
                translated_content=result.translated_content,
                created_at=now_ms(),
            )
        )
        await commit_with_retry(db)
    except IntegrityError:
        await db.rollback()

    # 5. Return fresh result
    return SubtitleTranslateResponse(
        translated_content=result.translated_content,
        format=result.fmt,
        cue_count=result.cue_count,
        cached=False,
        model=result.model,
        duration_seconds=duration,
    )

