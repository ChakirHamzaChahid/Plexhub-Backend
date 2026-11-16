"""Pure ranking logic: cache lookup, parallel hydrate with timeout, cosine, centroid.

Also provides generate_blurb() for F3 (French synopsis + mood tags via Ollama/gemma4).

Stateless wrt user: only operates on tmdb_id integers. imdb_id resolution is done
by the endpoint layer (J3b) before calling this service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Literal

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.database import Media
from app.services import account_outage_service
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

def serialize_vec(values: list[float]) -> bytes:
    """Little-endian float32 blob, matching sqlite-vec's FLOAT[384] layout.

    Public: also consumed by app.workers.embedding_worker (cross-module reuse).
    """
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
    vec_blob = serialize_vec(vec)

    async def _persist(session: AsyncSession) -> None:
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
            {"tid": tmdb_id, "v": vec_blob},
        )
        await session.commit()

    from app.utils.db_retry import write_with_retry

    await write_with_retry(
        _persist,
        session_factory=sessionmaker,
        op="recommendation.hydrate_one",
    )

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

# CR-P08 — adaptive over-fetch tuning.
#
# vec0 has no media_type column, so a type-filtered search must over-fetch
# neighbors from the KNN index and post-filter by joining ai_tmdb_cache. A
# single fixed over-fetch factor can under-return under a skewed type mix:
# when the nearest-neighbor cloud is dominated by the *other* media type,
# the post-filter can drop enough rows that fewer than `limit` survive even
# though more matching rows exist further down the neighbor list.
#
# Mitigation: try the KNN at an initial ceiling (200 — unchanged from the
# prior fixed behaviour, so the common/unskewed case pays no extra cost); if
# the post-filter still comes up short of `limit` *and* the KNN returned a
# full page at that ceiling (meaning more neighbors may exist beyond it),
# escalate once to a hard cap of 2000 and re-query. This bounds every call
# to at most two index-scan round-trips (never a full-table scan) while
# making the result count robust to moderate/heavy skew. Extreme skew
# beyond the hard cap (fewer than `limit` matches of the requested type
# within the nearest 2000 neighbors) can still under-return — an accepted
# residual, tracked as CR-P08.
KNN_OVERFETCH_FACTOR = 4
KNN_OVERFETCH_CEILINGS = (200, 2000)  # escalation ladder; last value = hard cap
_SQLITE_IN_CHUNK = 900


def _genres_match(genre_text: str | None, req_genres: list[str]) -> bool:
    """Return True if any requested genre (lowercase) appears in *genre_text*."""
    if not genre_text:
        return False
    haystack = genre_text.lower()
    return any(g in haystack for g in req_genres)


async def filter_catalog_tmdb_ids(
    db: AsyncSession,
    tmdb_ids: list[int],
    *,
    media_type: str | None = None,
    playable_only: bool = True,
    genres: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    min_rating: float | None = None,
    cached_genres_by_id: dict[int, str | None] | None = None,
    masked_server_ids: frozenset[str] | None = None,
) -> set[int]:
    """Filter *tmdb_ids* against catalog availability and metadata criteria.

    When *playable_only* is True, a tmdb_id is kept only if at least one
    matching ``Media`` row exists with ``is_in_allowed_categories=True``,
    ``is_broken=False``, and ``server_id`` not in ``account_outage_service``.
    Optional ``genres``, ``year_min``, ``year_max``, and ``min_rating``
    filters are applied on ``Media`` (or ``ai_tmdb_cache.genres`` when
    ``playable_only=False`` and no year/rating filter requires ``Media``).
    """
    if not tmdb_ids:
        return set()

    req_genres = [g.strip().lower() for g in (genres or []) if g and g.strip()]
    needs_media_query = (
        playable_only
        or year_min is not None
        or year_max is not None
        or min_rating is not None
    )

    if not needs_media_query:
        if not req_genres:
            return set(tmdb_ids)
        genres_map = cached_genres_by_id
        if genres_map is None:
            genres_map = {}
            for i in range(0, len(tmdb_ids), _SQLITE_IN_CHUNK):
                chunk = tmdb_ids[i : i + _SQLITE_IN_CHUNK]
                ph = ",".join(f":id{j}" for j in range(len(chunk)))
                params = {f"id{j}": tid for j, tid in enumerate(chunk)}
                rows = (
                    await db.execute(
                        text(f"SELECT tmdb_id, genres FROM ai_tmdb_cache WHERE tmdb_id IN ({ph})"),
                        params,
                    )
                ).fetchall()
                for tid, gtxt in rows:
                    genres_map[tid] = gtxt
        return {tid for tid in tmdb_ids if _genres_match(genres_map.get(tid), req_genres)}

    masked = (
        masked_server_ids
        if masked_server_ids is not None
        else (await account_outage_service.masked_server_ids(db) if playable_only else frozenset())
    )

    conditions = []
    if media_type == "movie":
        conditions.append(Media.type == "movie")
    elif media_type == "tv":
        conditions.append(Media.type == "show")
    else:
        conditions.append(Media.type.in_(("movie", "show")))

    if playable_only:
        conditions.append(Media.is_in_allowed_categories == True)  # noqa: E712
        conditions.append(Media.is_broken == False)  # noqa: E712
        if masked:
            conditions.append(Media.server_id.notin_(masked))

    if year_min is not None:
        conditions.append(Media.year >= year_min)
    if year_max is not None:
        conditions.append(Media.year <= year_max)
    if min_rating is not None:
        conditions.append(Media.display_rating >= min_rating)

    allowed: set[int] = set()
    str_ids = [str(tid) for tid in tmdb_ids]
    for i in range(0, len(str_ids), _SQLITE_IN_CHUNK):
        chunk = str_ids[i : i + _SQLITE_IN_CHUNK]
        if req_genres:
            stmt = select(Media.tmdb_id, Media.genres).where(
                Media.tmdb_id.in_(chunk),
                *conditions,
            ).distinct()
            rows = (await db.execute(stmt)).fetchall()
            for tid_str, media_genres in rows:
                if tid_str is None:
                    continue
                try:
                    tid_int = int(tid_str)
                except ValueError:
                    continue
                fallback_genres = cached_genres_by_id.get(tid_int) if cached_genres_by_id else None
                if _genres_match(media_genres, req_genres) or _genres_match(fallback_genres, req_genres):
                    allowed.add(tid_int)
        else:
            stmt = select(Media.tmdb_id).where(
                Media.tmdb_id.in_(chunk),
                *conditions,
            ).distinct()
            rows = (await db.execute(stmt)).scalars().all()
            for tid_str in rows:
                if tid_str is None:
                    continue
                try:
                    allowed.add(int(tid_str))
                except ValueError:
                    continue
    return allowed


async def _knn_search_filtered(
    db: AsyncSession,
    vec_blob: bytes,
    media_type: str | None,
    limit: int,
    want_overview: bool,
    *,
    playable_only: bool = False,
    genres: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    min_rating: float | None = None,
) -> list[tuple[int, str | None, str, float, str | None]]:
    """Adaptive-over-fetch KNN + post-hoc media_type/catalog filter (CR-P08, T10.1).

    Shared core for semantic_search / semantic_search_with_overview. Runs the
    native sqlite-vec KNN query (index scan, never a full table scan) at an
    escalating ladder of `k` ceilings (see KNN_OVERFETCH_CEILINGS above),
    stopping as soon as the post-filter yields >= limit rows of the
    requested criteria, or the KNN itself returns fewer rows than asked
    (the whole vec table has already been scanned, escalating further would
    be a wasted round-trip).

    Returns (tmdb_id, title, media_type, score, overview) tuples sorted by
    score descending, truncated to `limit`. `overview` is always None when
    want_overview=False.
    """
    knn_sql = text(
        "SELECT tmdb_id, distance "
        "FROM ai_embeddings "
        "WHERE embedding MATCH :vec AND k = :k "
        "ORDER BY distance"
    )
    need_genres_col = bool(genres)
    extra_cols = (", overview" if want_overview else "") + (", genres" if need_genres_col else "")

    has_post_filter = (
        media_type is not None
        or playable_only
        or bool(genres)
        or year_min is not None
        or year_max is not None
        or min_rating is not None
    )
    if not has_post_filter:
        # No post-filter -> no over-fetch needed, exactly `limit` rows.
        attempt_ks = [limit]
    else:
        first_k = min(limit * KNN_OVERFETCH_FACTOR, KNN_OVERFETCH_CEILINGS[0])
        # Escalation tiers beyond the first attempt are used as absolute k
        # values (not re-multiplied by limit) so each retry is strictly
        # larger than the previous one, even for small `limit`.
        attempt_ks = [first_k] + [c for c in KNN_OVERFETCH_CEILINGS[1:] if c > first_k]

    masked_ids = (
        await account_outage_service.masked_server_ids(db)
        if playable_only
        else frozenset()
    )
    needs_catalog_filter = (
        playable_only
        or bool(genres)
        or year_min is not None
        or year_max is not None
        or min_rating is not None
    )

    results: list[tuple[int, str | None, str, float, str | None]] = []
    for k in attempt_ks:
        knn_rows = (await db.execute(knn_sql, {"vec": vec_blob, "k": k})).fetchall()
        if not knn_rows:
            return []

        knn_ids = [row[0] for row in knn_rows]
        dist_by_id = {row[0]: row[1] for row in knn_rows}

        placeholders = ",".join(f":id{i}" for i in range(len(knn_ids)))
        params: dict = {f"id{i}": tid for i, tid in enumerate(knn_ids)}
        cache_sql = text(
            f"SELECT tmdb_id, title, media_type{extra_cols} "
            f"FROM ai_tmdb_cache "
            f"WHERE tmdb_id IN ({placeholders})"
        )
        cache_rows = (await db.execute(cache_sql, params)).fetchall()

        type_matched: list[tuple[int, str | None, str, str | None]] = []
        cached_genres: dict[int, str | None] = {}
        for row in cache_rows:
            tmdb_id, title, row_media_type = row[0], row[1], row[2]
            if media_type is not None and row_media_type != media_type:
                continue
            idx = 3
            overview = None
            if want_overview:
                overview = row[idx]
                idx += 1
            if need_genres_col:
                cached_genres[tmdb_id] = row[idx]
            type_matched.append((tmdb_id, title, row_media_type, overview))

        if needs_catalog_filter and type_matched:
            allowed_ids = await filter_catalog_tmdb_ids(
                db,
                [item[0] for item in type_matched],
                media_type=media_type,
                playable_only=playable_only,
                genres=genres,
                year_min=year_min,
                year_max=year_max,
                min_rating=min_rating,
                cached_genres_by_id=cached_genres if need_genres_col else None,
                masked_server_ids=masked_ids,
            )
        else:
            allowed_ids = {item[0] for item in type_matched}

        results = []
        for tmdb_id, title, row_media_type, overview in type_matched:
            if tmdb_id not in allowed_ids:
                continue
            dist = dist_by_id[tmdb_id]
            # L2-norm vectors: cosine_sim = 1 - dist^2 / 2
            score = round(1.0 - (dist ** 2) / 2.0, 6)
            results.append((tmdb_id, title, row_media_type, score, overview))

        if len(results) >= limit or len(knn_rows) < k:
            break

    # Re-sort by score descending (join may have reordered) and truncate.
    results.sort(key=lambda x: x[3], reverse=True)
    return results[:limit]


async def semantic_search(
    db: AsyncSession,
    query_vec: list[float],
    media_type: str | None,
    limit: int,
    *,
    playable_only: bool = False,
    genres: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    min_rating: float | None = None,
) -> list[tuple[int, str | None, str, float]]:
    """KNN vector search against ai_embeddings, returning joined cache metadata."""
    vec_blob = serialize_vec(query_vec)
    rows = await _knn_search_filtered(
        db,
        vec_blob,
        media_type,
        limit,
        want_overview=False,
        playable_only=playable_only,
        genres=genres,
        year_min=year_min,
        year_max=year_max,
        min_rating=min_rating,
    )
    return [(tmdb_id, title, row_media_type, score) for tmdb_id, title, row_media_type, score, _overview in rows]


async def semantic_search_with_overview(
    db: AsyncSession,
    query_vec: list[float],
    media_type: str | None,
    limit: int,
    *,
    playable_only: bool = False,
    genres: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    min_rating: float | None = None,
) -> list[tuple[int, str | None, str, float, str | None]]:
    """Like semantic_search but also returns the overview column from ai_tmdb_cache."""
    vec_blob = serialize_vec(query_vec)
    return await _knn_search_filtered(
        db,
        vec_blob,
        media_type,
        limit,
        want_overview=True,
        playable_only=playable_only,
        genres=genres,
        year_min=year_min,
        year_max=year_max,
        min_rating=min_rating,
    )


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


# ──────────────────────────────────────────────────────────────────────────────
# F3 — AI-generated French synopsis + mood tags
# ──────────────────────────────────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # drop first line (```json or ```) and last line (```)
        lines = stripped.splitlines()
        inner = lines[1:] if len(lines) > 1 else lines
        # drop trailing ```
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        stripped = "\n".join(inner).strip()
    return stripped


def _parse_blurb_json(raw: str) -> dict | None:
    """Try to parse the LLM output as {"summary": str, "tags": list[str]}.

    Returns the dict on success, None on failure.
    """
    try:
        obj = json.loads(_strip_code_fences(raw))
        if isinstance(obj, dict) and "summary" in obj and "tags" in obj:
            summary = str(obj["summary"]).strip()
            tags = obj["tags"] if isinstance(obj["tags"], list) else []
            tags = [str(t).strip() for t in tags if str(t).strip()]
            return {"summary": summary, "tags": tags}
    except Exception:
        pass
    return None


async def generate_blurb(
    tmdb_id: int,
    media_type: Literal["movie", "tv"],
    title: str,
    overview: str,
    genres: str,
    lang: str = "fr",
) -> dict:
    """Call Ollama/gemma4 to produce a French synopsis + mood/genre tags.

    The LLM is asked for a JSON object:
        {"summary": "1-2 sentence spoiler-free synopsis", "tags": ["tag1", ...]}

    Parse strategy (robust fallback):
      1. Strip markdown code fences, parse JSON — if valid, return it.
      2. Retry once with a stricter prompt on parse failure.
      3. If still unparseable, fall back: summary = first 300 chars of raw text,
         tags = [].

    Returns {"summary": str, "tags": list[str]}.
    Propagates any exception from ollama_service.generate (caller handles 503).
    """
    from app.services import ollama_service as _ollama

    lang_label = "français" if lang == "fr" else lang

    def _build_prompt(strict: bool = False) -> str:
        genres_str = genres or "N/A"
        overview_str = overview or "Non disponible"
        strictness = (
            " Réponds UNIQUEMENT avec le JSON, rien d'autre, pas de texte avant ou après."
            if strict else ""
        )
        return (
            f"Tu es un expert en cinéma et séries TV. "
            f"Pour le titre suivant, génère un synopsis court (1-2 phrases, sans spoiler) "
            f"et 3 à 6 étiquettes courtes de genre/ambiance, EN {lang_label.upper()}. "
            f"Réponds UNIQUEMENT en JSON avec ce format exactement : "
            f'{{\"summary\": \"...\", \"tags\": [\"...\", ...]}}. '
            f"Pas d'explication, pas de markdown.{strictness}\n\n"
            f"Titre : {title}\n"
            f"Genres : {genres_str}\n"
            f"Synopsis TMDB : {overview_str}"
        )

    # First attempt
    raw = await _ollama.generate(_build_prompt(strict=False))
    result = _parse_blurb_json(raw)

    if result is None:
        # Retry once with stricter prompt
        raw2 = await _ollama.generate(_build_prompt(strict=True))
        result = _parse_blurb_json(raw2)
        if result is None:
            # Graceful fallback: use first 300 chars as summary, empty tags
            fallback_text = _strip_code_fences(raw2 or raw).strip()
            result = {"summary": fallback_text[:300] or title, "tags": []}

    return result
