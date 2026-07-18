"""Persistent OMDb lookup cache, keyed directly on `imdb_id`.

Unlike `scrape_cache_service` (title+year signature — TMDB resolves
title -> id, so the cache key has to be a normalized title signature), the
imdb-id consistency validator always already has an `imdb_id` in hand: this
cache is therefore a direct point lookup, no title fuzz-matching. See
`docs/plans/2026-07-17-omdb-id-consistency-validator-design.md` §3.
"""
from __future__ import annotations

import dataclasses
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import OmdbScrapeCache
from app.services.omdb_service import OMDbData

logger = logging.getLogger("plexhub.omdb_scrape_cache")

# Matched resolutions are stable -> keep long. Negatives expire sooner so an
# id that failed once still gets retried later (mirrors scrape_cache_service's
# two-tier TTL, MATCH_TTL_MS/NEG_TTL_MS naming and values).
MATCH_TTL_MS = 30 * 24 * 3600 * 1000
NEG_TTL_MS = 3 * 24 * 3600 * 1000


async def get(db: AsyncSession, imdb_id: str, now_ms: int) -> OMDbData | None:
    """Return the fresh cached OMDb data for `imdb_id`, or None on
    miss/stale.

    A "not_found" row (fresh or stale) also resolves to None here — there is
    no `OMDbData` to hand back for a confirmed negative. `NEG_TTL_MS` still
    gates the negative TTL for direct-row callers that want to distinguish
    "confirmed not found, skip re-query" from "never queried" (e.g. the
    id-consistency validator script) — see the not_found expiry test for the
    exact contract this function exposes.
    """
    row = (await db.execute(
        select(OmdbScrapeCache).where(OmdbScrapeCache.imdb_id == imdb_id)
    )).scalars().first()
    if row is None:
        return None
    ttl = MATCH_TTL_MS if row.result == "found" else NEG_TTL_MS
    if now_ms - row.fetched_at > ttl:
        return None
    if row.result != "found" or not row.payload:
        return None
    try:
        return OMDbData(**json.loads(row.payload))
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Corrupted OMDb scrape-cache payload for %s: %s", imdb_id, e)
        return None


async def put(
    db: AsyncSession,
    imdb_id: str,
    result: str,
    data: OMDbData | None,
    now_ms: int,
) -> None:
    """Upsert an OMDb lookup resolution. Caller commits (same contract as
    `scrape_cache_service.put`)."""
    payload = json.dumps(dataclasses.asdict(data)) if data else None

    row = (await db.execute(
        select(OmdbScrapeCache).where(OmdbScrapeCache.imdb_id == imdb_id)
    )).scalars().first()
    if row is None:
        db.add(OmdbScrapeCache(
            imdb_id=imdb_id, result=result, payload=payload, fetched_at=now_ms,
        ))
    else:
        row.result = result
        row.payload = payload
        row.fetched_at = now_ms
