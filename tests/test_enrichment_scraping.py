"""Tests for the persistent scrape cache + enrichment worker cache short-circuit."""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.database import EnrichmentQueue
from app.services import scrape_cache_service as scrape_cache
from app.services.tmdb_service import TMDBEnrichmentData


def _data(tmdb_id=218, imdb="tt0088247"):
    return TMDBEnrichmentData(
        tmdb_id=tmdb_id, imdb_id=imdb, overview="A cyborg assassin.",
        poster_url="http://img/p.jpg", backdrop_url="http://img/b.jpg",
        vote_average=8.0, genres="Action, Sci-Fi", year=1984, cast="Arnold",
    )


class TestScrapeCache:
    def test_make_key_normalizes(self):
        assert scrape_cache.make_key("movie", "Terminator (VF)", 1984) == "movie|terminator vf|1984"
        assert scrape_cache.make_key("show", "Breaking Bad", None) == "show|breaking bad|"

    @pytest.mark.asyncio
    async def test_put_get_roundtrip(self, db_session):
        key = scrape_cache.make_key("movie", "Terminator", 1984)
        await scrape_cache.put(db_session, key, "movie", "matched", 0.97, _data(), now_ms=1000)
        await db_session.commit()

        hit = await scrape_cache.get(db_session, key, now_ms=2000)
        assert hit is not None
        assert hit.result == "matched"
        assert hit.data.tmdb_id == 218
        assert hit.data.imdb_id == "tt0088247"

    @pytest.mark.asyncio
    async def test_negative_entry_expires_sooner(self, db_session):
        key = scrape_cache.make_key("movie", "Nope", None)
        await scrape_cache.put(db_session, key, "movie", "nomatch", 0.4, None, now_ms=0)
        await db_session.commit()
        # Within NEG_TTL -> hit; past it -> miss.
        assert await scrape_cache.get(db_session, key, now_ms=scrape_cache.NEG_TTL_MS - 1) is not None
        assert await scrape_cache.get(db_session, key, now_ms=scrape_cache.NEG_TTL_MS + 1) is None


class TestEnrichmentCacheShortCircuit:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_tmdb(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew

        from app.utils.time import now_ms
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        # Seed a matched cache entry for "Terminator" (1984) at ~now (fresh).
        key = scrape_cache.make_key("movie", "Terminator", 1984)
        async with factory() as s:
            await scrape_cache.put(s, key, "movie", "matched", 0.97, _data(), now_ms=now_ms())
            await s.commit()

        monkeypatch.setattr(ew, "async_session_factory", factory)

        class _Boom:
            is_configured = True
            async def search_movie(self, *a, **k):
                raise AssertionError("TMDB search must NOT be called on a cache hit")
            async def get_movie_details(self, *a, **k):
                raise AssertionError("TMDB details must NOT be called on a cache hit")
        monkeypatch.setattr(ew, "tmdb_service", _Boom())

        item = EnrichmentQueue(
            rating_key="vod_1.mp4", server_id="xtream_a", media_type="movie",
            title="Terminator", year=1984, status="pending", attempts=0, created_at=0,
        )
        fr = await ew._resolve(item, "movie", asyncio.Semaphore(1))

        assert fr.from_cache is True
        assert fr.api_used == 0
        assert fr.result == "matched"
        assert fr.data.tmdb_id == 218

    @pytest.mark.asyncio
    async def test_fallback_then_cache_store(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        from app.services.tmdb_service import TMDBMatch, TMDBSearchOutcome

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)

        calls = {"search": 0, "details": 0}

        class _Fake:
            is_configured = True
            async def search_movie(self, title, year, *, summary=None, language=None):
                calls["search"] += 1
                # Miss with year, match once year is dropped (fallback step 2).
                if year is not None:
                    return TMDBSearchOutcome("nomatch", best=TMDBMatch(0, "x", None, 0.4))
                return TMDBSearchOutcome(
                    "matched",
                    match=TMDBMatch(218, "Terminator", 1984, 0.97, title_score=1.0),
                    best=TMDBMatch(218, "Terminator", 1984, 0.97),
                )
            async def search_multi(self, *a, **k):
                return TMDBSearchOutcome("nomatch")
            async def get_movie_details(self, tmdb_id):
                calls["details"] += 1
                return _data(tmdb_id=tmdb_id)
        monkeypatch.setattr(ew, "tmdb_service", _Fake())

        item = EnrichmentQueue(
            rating_key="vod_9.mp4", server_id="xtream_b", media_type="movie",
            title="Terminator", year=1999, status="pending", attempts=0, created_at=0,
        )
        fr = await ew._resolve(item, "movie", asyncio.Semaphore(1))
        assert fr.result == "matched"
        assert fr.cache_key == scrape_cache.make_key("movie", "Terminator", 1999)
        assert calls["search"] >= 2  # first (with year) missed, retry (no year) matched
        assert calls["details"] == 1
