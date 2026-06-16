"""Pure ranking logic: cache lookup, parallel hydrate with timeout, cosine, centroid.

Stateless wrt user: only operates on tmdb_id integers. imdb_id resolution is done
by the endpoint layer (J3b) before calling this service.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass
from typing import Literal

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.embedding_service import (
    EMBEDDING_DIM,
    EmbeddingUnavailableError,
    embed_passages,
)
from app.services.tmdb_service import tmdb_service

logger = logging.getLogger("plexhub.ai.reco")

HYDRATE_CAP = 20
HYDRATE_PER_TASK_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class HydrateStats:
    hydrated: int           # successfully fetched + embedded + stored
    dropped: int            # over the cap (cap_excess) + timeouts + errors


# ──────────────────────────────────────────────────────────────────────────────
# Vector blob (de)serialization for sqlite-vec FLOAT[384]
# ──────────────────────────────────────────────────────────────────────────────

def _serialize_vec(values: list[float]) -> bytes:
    """Little-endian float32 blob, matching sqlite-vec's FLOAT[384] layout."""
    try:
        from sqlite_vec import serialize_float32  # type: ignore
        return serialize_float32(values)
    except Exception:
        return struct.pack(f"<{len(values)}f", *values)


def _deserialize_vec(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{EMBEDDING_DIM}f", blob))


# ──────────────────────────────────────────────────────────────────────────────
# Cache lookup
# ──────────────────────────────────────────────────────────────────────────────

async def load_cached_vectors(
    db: AsyncSession,
    tmdb_ids: list[int],
) -> dict[int, list[float]]:
    """Read ai_embeddings rows for the requested tmdb_id list.

    Returns {tmdb_id: vector} for hits only. Missing ids are not in the dict.
    Uses parameterized IN clause. Returns {} when tmdb_ids is empty.
    """
    if not tmdb_ids:
        return {}
    placeholders = ",".join(f":id{i}" for i in range(len(tmdb_ids)))
    params = {f"id{i}": tid for i, tid in enumerate(tmdb_ids)}
    sql = text(f"SELECT tmdb_id, embedding FROM ai_embeddings WHERE tmdb_id IN ({placeholders})")
    rows = (await db.execute(sql, params)).fetchall()
    return {row[0]: _deserialize_vec(row[1]) for row in rows}


# ──────────────────────────────────────────────────────────────────────────────
# Hydrate cache misses (TMDB fetch + embed + INSERT)
# ──────────────────────────────────────────────────────────────────────────────

async def _fetch_and_store_one(
    tmdb_id: int,
    media_type: Literal["movie", "tv"],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[int, list[float] | None]:
    """Fetch TMDB details, embed (overview + genres), INSERT cache + embedding.

    Returns (tmdb_id, vector) on success, (tmdb_id, None) on enrichment failure.
    Propagates EmbeddingUnavailableError (caught by the endpoint -> 503).
    Each call opens its own AsyncSession to avoid aiosqlite race on shared session.
    """
    try:
        if media_type == "movie":
            data = await tmdb_service.get_movie_details(tmdb_id)
        else:
            data = await tmdb_service.get_tv_details(tmdb_id)
    except EmbeddingUnavailableError:
        raise
    except Exception as exc:
        logger.warning("TMDB fetch failed for tmdb_id=%s media=%s: %s", tmdb_id, media_type, exc)
        return (tmdb_id, None)

    if data is None:
        return (tmdb_id, None)

    overview = (getattr(data, "overview", None) or "").strip()
    genres = (getattr(data, "genres", None) or "").strip()
    if not overview and not genres:
        return (tmdb_id, None)

    text_doc = f"{overview}\n{genres}".strip()
    # embed_passages propagates EmbeddingUnavailableError — let it bubble
    vecs = await embed_passages([text_doc])
    if not vecs:
        return (tmdb_id, None)
    vec = vecs[0]

    now_ms = int(time.time() * 1000)
    imdb_id = getattr(data, "imdb_id", None)
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, "
                "overview, genres, fetched_at, embedded_at) "
                "VALUES(:tmdb_id, :imdb_id, :media_type, NULL, :overview, :genres, :now, :now) "
                "ON CONFLICT(tmdb_id) DO UPDATE SET "
                "imdb_id=excluded.imdb_id, media_type=excluded.media_type, "
                "overview=excluded.overview, genres=excluded.genres, "
                "fetched_at=excluded.fetched_at, embedded_at=excluded.embedded_at"
            ),
            {
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "media_type": media_type,
                "overview": overview or None,
                "genres": genres or None,
                "now": now_ms,
            },
        )
        # ai_embeddings is a sqlite-vec virtual table (vec0); UPSERT is not
        # supported on virtual tables — use DELETE + INSERT instead.
        await session.execute(
            text("DELETE FROM ai_embeddings WHERE tmdb_id = :tid"),
            {"tid": tmdb_id},
        )
        await session.execute(
            text("INSERT INTO ai_embeddings(tmdb_id, embedding) VALUES(:tid, :v)"),
            {"tid": tmdb_id, "v": _serialize_vec(vec)},
        )
        await session.commit()

    return (tmdb_id, vec)


async def hydrate_misses(
    missing_ids: list[int],
    media_type: Literal["movie", "tv"],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[dict[int, list[float]], HydrateStats]:
    """Fetch+embed the first HYDRATE_CAP=20 misses in parallel, with per-task timeout.

    Misses beyond the cap are silently dropped (counted in stats.dropped).
    Per-task timeout 10s — timeouts also counted in dropped.
    Returns ({tmdb_id: vector}, HydrateStats).
    """
    if not missing_ids:
        return {}, HydrateStats(hydrated=0, dropped=0)

    cap_excess = max(0, len(missing_ids) - HYDRATE_CAP)
    targets = missing_ids[:HYDRATE_CAP]

    async def _bounded_one(tid: int) -> tuple[int, list[float] | None]:
        try:
            return await asyncio.wait_for(
                _fetch_and_store_one(tid, media_type, sessionmaker),
                timeout=HYDRATE_PER_TASK_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning("hydrate timeout tmdb_id=%s", tid)
            return (tid, None)

    results = await asyncio.gather(*(_bounded_one(t) for t in targets), return_exceptions=True)

    vectors: dict[int, list[float]] = {}
    failed = 0
    for r in results:
        if isinstance(r, BaseException):
            if isinstance(r, EmbeddingUnavailableError):
                raise r  # propagate to endpoint -> 503
            failed += 1
            continue
        tid, vec = r
        if vec is None:
            failed += 1
        else:
            vectors[tid] = vec

    return vectors, HydrateStats(
        hydrated=len(vectors),
        dropped=cap_excess + failed,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Semantic KNN search over ai_embeddings + join ai_tmdb_cache
# ──────────────────────────────────────────────────────────────────────────────

async def semantic_search(
    db: AsyncSession,
    query_vec: list[float],
    media_type: str | None,
    limit: int,
) -> list[tuple[int, str | None, str, float]]:
    """KNN vector search against ai_embeddings, returning joined cache metadata.

    Uses the native sqlite-vec KNN syntax:
        WHERE embedding MATCH :vec AND k = :k ORDER BY distance

    The vec0 distance metric is L2.  Because every embedding stored by this
    project is L2-normalised (output of embed_passages / embed_query), the
    conversion from L2 distance to cosine similarity is exact:
        cosine_sim = 1.0 - (l2_distance ** 2) / 2.0

    When media_type is given the vec0 table has no media_type column, so we
    over-fetch (limit * 4, capped at 200) from the KNN query, then filter by
    joining ai_tmdb_cache and truncate to limit.  When media_type is None no
    extra filtering is needed and we fetch exactly limit rows.

    Returns a list of (tmdb_id, title, media_type, score) tuples, sorted by
    score descending.  Rows with no matching ai_tmdb_cache entry are dropped
    (they cannot be hydrated into a usable result).
    """
    vec_blob = _serialize_vec(query_vec)

    # Determine how many rows to fetch from the KNN index before filtering.
    if media_type is not None:
        # Over-fetch to account for the type filter; cap to avoid runaway queries.
        knn_k = min(limit * 4, 200)
    else:
        knn_k = limit

    # Phase 1 — KNN over the vec0 virtual table (index scan, no full table scan).
    knn_sql = text(
        "SELECT tmdb_id, distance "
        "FROM ai_embeddings "
        "WHERE embedding MATCH :vec AND k = :k "
        "ORDER BY distance"
    )
    knn_rows = (await db.execute(knn_sql, {"vec": vec_blob, "k": knn_k})).fetchall()
    if not knn_rows:
        return []

    # Phase 2 — join ai_tmdb_cache for title / media_type.
    knn_ids = [row[0] for row in knn_rows]
    dist_by_id = {row[0]: row[1] for row in knn_rows}

    placeholders = ",".join(f":id{i}" for i in range(len(knn_ids)))
    params: dict = {f"id{i}": tid for i, tid in enumerate(knn_ids)}
    cache_sql = text(
        f"SELECT tmdb_id, title, media_type "
        f"FROM ai_tmdb_cache "
        f"WHERE tmdb_id IN ({placeholders})"
    )
    cache_rows = (await db.execute(cache_sql, params)).fetchall()

    # Build results, optionally filtering by media_type.
    results: list[tuple[int, str | None, str, float]] = []
    for tmdb_id, title, row_media_type in cache_rows:
        if media_type is not None and row_media_type != media_type:
            continue
        dist = dist_by_id[tmdb_id]
        # L2-norm vectors: cosine_sim = 1 - dist^2 / 2
        score = round(1.0 - (dist ** 2) / 2.0, 6)
        results.append((tmdb_id, title, row_media_type, score))

    # Re-sort by score descending (join may have reordered) and truncate.
    results.sort(key=lambda x: x[3], reverse=True)
    return results[:limit]


# ──────────────────────────────────────────────────────────────────────────────
# Cosine ranking
# ──────────────────────────────────────────────────────────────────────────────

def cosine_rank(
    query_vec: list[float],
    candidate_vecs: dict[int, list[float]],
    limit: int,
    exclude: set[int] | None = None,
) -> list[tuple[int, float]]:
    """Rank candidate tmdb_ids by cosine similarity to query_vec.

    Both query_vec and candidate_vecs values are assumed L2-normalized
    (output of embed_passages / embed_query). Cosine reduces to dot product.

    Returns sorted [(tmdb_id, score), ...] descending, length <= limit,
    excluding any id in `exclude`.
    """
    if not candidate_vecs:
        return []
    excl = exclude or set()
    q = np.asarray(query_vec, dtype=np.float32)
    items: list[tuple[int, float]] = []
    for tid, vec in candidate_vecs.items():
        if tid in excl:
            continue
        score = float(np.dot(q, np.asarray(vec, dtype=np.float32)))
        items.append((tid, score))
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:limit]
