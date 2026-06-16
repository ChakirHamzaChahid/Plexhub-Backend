"""Persistent TMDB scrape cache.

Keyed by a normalized title signature so the same film/series — across accounts
AND across restarts — reuses one TMDB resolution instead of re-querying. This is
the durable counterpart to the in-memory TTL caches in `tmdb_service`.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import TmdbScrapeCache
from app.services.tmdb_service import TMDBEnrichmentData
from app.utils.string_normalizer import normalize_for_sorting

logger = logging.getLogger("plexhub.scrape_cache")

# Matched resolutions are stable → keep long. Negatives expire sooner so a title
# that failed once still gets retried later (e.g. after TMDB adds it).
MATCH_TTL_MS = 30 * 24 * 3600 * 1000
NEG_TTL_MS = 3 * 24 * 3600 * 1000


def make_key(media_type: str, title: str, year: int | None) -> str:
    """Signature key: same cleaned title+year → same cache entry."""
    return f"{media_type}|{normalize_for_sorting(title)}|{year or ''}"


@dataclass
class CacheHit:
    result: str                      # matched | ambiguous | nomatch
    confidence: float | None
    data: TMDBEnrichmentData | None   # set only when result == "matched"


async def get(db: AsyncSession, key: str, now_ms: int) -> CacheHit | None:
    """Return a fresh cache entry, or None on miss/stale."""
    row = (await db.execute(
        select(TmdbScrapeCache).where(TmdbScrapeCache.cache_key == key)
    )).scalars().first()
    if row is None:
        return None
    ttl = MATCH_TTL_MS if row.result == "matched" else NEG_TTL_MS
    if now_ms - row.fetched_at > ttl:
        return None
    data = None
    if row.result == "matched" and row.payload:
        try:
            data = TMDBEnrichmentData(**json.loads(row.payload))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Corrupted scrape-cache payload for %s: %s", key, e)
            return None
    return CacheHit(result=row.result, confidence=row.confidence, data=data)


async def put(
    db: AsyncSession,
    key: str,
    media_type: str,
    result: str,
    confidence: float | None,
    data: TMDBEnrichmentData | None,
    now_ms: int,
) -> None:
    """Upsert a resolution. Caller commits (uses the worker's session)."""
    payload = json.dumps(dataclasses.asdict(data)) if data else None
    tmdb_id = str(data.tmdb_id) if data else None
    imdb_id = data.imdb_id if data else None

    row = (await db.execute(
        select(TmdbScrapeCache).where(TmdbScrapeCache.cache_key == key)
    )).scalars().first()
    if row is None:
        db.add(TmdbScrapeCache(
            cache_key=key, media_type=media_type, result=result,
            tmdb_id=tmdb_id, imdb_id=imdb_id, confidence=confidence,
            payload=payload, fetched_at=now_ms,
        ))
    else:
        row.result = result
        row.tmdb_id = tmdb_id
        row.imdb_id = imdb_id
        row.confidence = confidence
        row.payload = payload
        row.fetched_at = now_ms
