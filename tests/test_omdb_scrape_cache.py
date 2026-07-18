"""Tests for the persistent OMDb lookup cache (imdb_id-keyed point lookup).

Mirrors the cache-part style of tests/test_enrichment_scraping.py
(TestScrapeCache) but against `omdb_scrape_cache_service` + `OMDbData`."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.database import OmdbScrapeCache
from app.services import omdb_scrape_cache_service as omdb_cache
from app.services.omdb_service import OMDbData


def _data(title="The Matrix", imdb_rating=8.7):
    return OMDbData(
        title=title, year="1999", runtime_minutes=136, genre="Action, Sci-Fi",
        director="Lana Wachowski, Lilly Wachowski", actors="Keanu Reeves",
        plot="A computer hacker learns the truth.", imdb_rating=imdb_rating,
        imdb_votes=2000000, type="movie",
    )


class TestOmdbScrapeCache:
    @pytest.mark.asyncio
    async def test_put_get_roundtrip_found(self, db_session):
        await omdb_cache.put(db_session, "tt0133093", "found", _data(), now_ms=1000)
        await db_session.commit()

        hit = await omdb_cache.get(db_session, "tt0133093", now_ms=2000)
        assert hit is not None
        assert hit.title == "The Matrix"
        assert hit.year == "1999"
        assert hit.runtime_minutes == 136
        assert hit.imdb_rating == 8.7
        assert hit.imdb_votes == 2000000
        assert hit.type == "movie"

    @pytest.mark.asyncio
    async def test_put_get_roundtrip_not_found_has_no_payload(self, db_session):
        await omdb_cache.put(db_session, "tt9999999", "not_found", None, now_ms=1000)
        await db_session.commit()

        # `get()` only ever hands back `OMDbData`, so a not_found row (there is
        # no data to return) resolves to None just like a genuine miss.
        assert await omdb_cache.get(db_session, "tt9999999", now_ms=1500) is None

        row = (await db_session.execute(
            select(OmdbScrapeCache).where(OmdbScrapeCache.imdb_id == "tt9999999")
        )).scalars().first()
        assert row is not None
        assert row.result == "not_found"
        assert row.payload is None

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, db_session):
        assert await omdb_cache.get(db_session, "tt_never_seen", now_ms=1000) is None

    @pytest.mark.asyncio
    async def test_found_entry_expires_after_match_ttl(self, db_session):
        await omdb_cache.put(db_session, "tt0133093", "found", _data(), now_ms=0)
        await db_session.commit()

        fresh = await omdb_cache.get(db_session, "tt0133093", now_ms=omdb_cache.MATCH_TTL_MS - 1)
        assert fresh is not None
        assert fresh.title == "The Matrix"

        stale = await omdb_cache.get(db_session, "tt0133093", now_ms=omdb_cache.MATCH_TTL_MS + 1)
        assert stale is None

    @pytest.mark.asyncio
    async def test_not_found_entry_returns_none_at_any_freshness(self, db_session):
        """Unlike `scrape_cache_service` (which wraps `result` in a
        `CacheHit` so callers can see a fresh "nomatch" distinctly from a
        miss), this cache's public `get()` returns `OMDbData | None` only —
        a "not_found" row therefore always resolves to None, whether within
        or past `NEG_TTL_MS` (there is no `OMDbData` to hand back either
        way). `NEG_TTL_MS` still gates the negative TTL for any future
        direct-row caller (e.g. the id-consistency validator script) that
        wants "confirmed not found, skip re-query" vs "never queried"."""
        await omdb_cache.put(db_session, "tt9999999", "not_found", None, now_ms=0)
        await db_session.commit()

        assert await omdb_cache.get(db_session, "tt9999999", now_ms=omdb_cache.NEG_TTL_MS - 1) is None
        assert await omdb_cache.get(db_session, "tt9999999", now_ms=omdb_cache.NEG_TTL_MS + 1) is None

        row = (await db_session.execute(
            select(OmdbScrapeCache).where(OmdbScrapeCache.imdb_id == "tt9999999")
        )).scalars().first()
        assert row.fetched_at == 0
        assert row.result == "not_found"

    @pytest.mark.asyncio
    async def test_upsert_overwrites_existing_row(self, db_session):
        await omdb_cache.put(db_session, "tt0133093", "not_found", None, now_ms=0)
        await db_session.commit()
        assert await omdb_cache.get(db_session, "tt0133093", now_ms=100) is None

        await omdb_cache.put(db_session, "tt0133093", "found", _data(), now_ms=200)
        await db_session.commit()

        hit = await omdb_cache.get(db_session, "tt0133093", now_ms=300)
        assert hit is not None
        assert hit.title == "The Matrix"

        rows = (await db_session.execute(
            select(OmdbScrapeCache).where(OmdbScrapeCache.imdb_id == "tt0133093")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].result == "found"
        assert rows[0].fetched_at == 200
