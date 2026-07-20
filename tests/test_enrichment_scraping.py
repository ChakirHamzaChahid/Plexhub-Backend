"""Tests for the persistent scrape cache + enrichment worker cache short-circuit."""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import EnrichmentQueue, Media, OmdbScrapeCache
from app.services import omdb_scrape_cache_service as omdb_scrape_cache
from app.services import scrape_cache_service as scrape_cache
from app.services.omdb_service import OMDbData
from app.services.tmdb_service import TMDBEnrichmentData, TMDBMatch, TMDBSearchOutcome
from app.utils.time import now_ms


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


class TestApplyResultsCacheDedup:
    """Regression: bulk re-queue of no-id duplicates (same normalized title+year)
    puts several items with one shared cache_key into a single batch. Under
    `no_autoflush`, a naive per-item `scrape_cache.put` adds two rows with the
    same primary key → `UNIQUE constraint failed: tmdb_scrape_cache.cache_key`
    crashes the whole batch. The batch must dedupe puts by cache_key."""

    @pytest.mark.asyncio
    async def test_same_cache_key_in_batch_does_not_crash(self, db_session):
        from app.workers.enrichment_worker import _apply_enrichment_results, FetchResult
        from app.models.database import TmdbScrapeCache

        key = scrape_cache.make_key("movie", "Terminator", 1984)
        items = [
            EnrichmentQueue(
                rating_key=f"vod_{i}.mp4", server_id="xtream_a",
                media_type="movie", title="Terminator", year=1984,
                status="pending", attempts=0, created_at=0,
            )
            for i in (1, 2, 3)
        ]
        db_session.add_all(items)
        await db_session.flush()

        results = [
            FetchResult(item=it, data=_data(), confidence=0.97, result="matched",
                        api_used=1, cache_key=key, from_cache=False)
            for it in items
        ]

        # Must not raise IntegrityError on commit.
        await _apply_enrichment_results(db_session, results)
        await db_session.commit()

        n = (await db_session.execute(
            select(sa_func.count()).select_from(TmdbScrapeCache)
            .where(TmdbScrapeCache.cache_key == key)
        )).scalar_one()
        assert n == 1                                  # one cache row, not three
        assert all(it.status == "done" for it in items)  # every row still resolved


# ─── OMDb fetch in the concurrent _resolve phase (dual-provider design C3) ──


def _omdb(title="Terminator", year="1984", imdb_id="tt0088247",
          imdb_rating=8.1, imdb_votes=900000, type="movie"):
    return OMDbData(
        title=title, year=year, runtime_minutes=107, genre="Action",
        director="J. Cameron", actors="Arnold Schwarzenegger",
        plot="A cyborg is sent back in time.", imdb_rating=imdb_rating,
        imdb_votes=imdb_votes, type=type, imdb_id=imdb_id,
    )


class _FakeOmdbSvc:
    """Call-counting double for `app.services.omdb_service.omdb_service`.

    Distinguishes by-id (`get_by_imdb_id`) from by-title (`search_by_title`)
    calls so the tests can assert exactly which OMDb path fired and how often."""

    def __init__(self, by_id=None, by_title=None, configured=True, count=0, raises=False):
        self._by_id = by_id
        self._by_title = by_title
        self._configured = configured
        self._count = count
        self._raises = raises
        self.id_calls = 0
        self.title_calls = 0

    @property
    def is_configured(self) -> bool:
        return self._configured

    def get_request_count(self) -> int:
        return self._count

    async def get_by_imdb_id(self, imdb_id):
        self.id_calls += 1
        self._count += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._by_id

    async def search_by_title(self, title, year, media_type):
        self.title_calls += 1
        self._count += 1
        return self._by_title


class _FakeTmdbDetails:
    """Scenario-2 TMDB double: existing tmdb_id -> details with an imdb_id."""
    is_configured = True

    async def get_movie_details(self, tmdb_id):
        return _data(tmdb_id=tmdb_id)

    async def get_tv_details(self, tmdb_id):
        return _data(tmdb_id=tmdb_id)


class _FakeTmdbNomatch:
    """Scenario-4 TMDB double that never matches (drives the OMDb-by-title
    fallback)."""
    is_configured = True

    async def search_movie(self, title, year, *, summary=None, language=None):
        return TMDBSearchOutcome("nomatch", best=TMDBMatch(0, "x", None, 0.4))

    async def search_tv(self, title, year, *, summary=None, language=None):
        return TMDBSearchOutcome("nomatch", best=TMDBMatch(0, "x", None, 0.4))

    async def search_multi(self, *a, **k):
        return TMDBSearchOutcome("nomatch", best=TMDBMatch(0, "x", None, 0.4))

    async def get_movie_details(self, tmdb_id):  # pragma: no cover - must not run
        raise AssertionError("details must not be called on a nomatch")


class _FakeTmdbMatchLowConf:
    """Scenario-4 TMDB double that matches at confidence < 1.0 with an imdb_id
    (drives the by-id fetch + contradiction tie-break)."""
    is_configured = True

    async def search_movie(self, title, year, *, summary=None, language=None):
        return TMDBSearchOutcome(
            "matched",
            match=TMDBMatch(218, "Terminator", 1984, 0.8, title_score=0.95),
            best=TMDBMatch(218, "Terminator", 1984, 0.8),
        )

    async def search_multi(self, *a, **k):
        return TMDBSearchOutcome("nomatch")

    async def get_movie_details(self, tmdb_id):
        return _data(tmdb_id=tmdb_id)  # imdb tt0088247, year 1984


def _s2_item(rating_key="vod_s2.mp4", server_id="xtream_s2"):
    return EnrichmentQueue(
        rating_key=rating_key, server_id=server_id, media_type="movie",
        title="Terminator", year=1984, status="pending", attempts=0, created_at=0,
        existing_tmdb_id="218",
    )


def _s4_item(rating_key="vod_s4.mp4", server_id="xtream_s4"):
    return EnrichmentQueue(
        rating_key=rating_key, server_id=server_id, media_type="movie",
        title="Terminator", year=1984, status="pending", attempts=0, created_at=0,
    )


class TestResolveOmdbFetch:
    @pytest.mark.asyncio
    async def test_by_id_fetched_once_and_put_set(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbDetails())
        fake = _FakeOmdbSvc(by_id=_omdb())
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s2_item(), "movie", asyncio.Semaphore(1))

        assert fake.id_calls == 1 and fake.title_calls == 0  # single by-id fetch
        assert fr.omdb is not None and fr.omdb.imdb_rating == 8.1
        assert fr.omdb_put == ("tt0088247", "found")
        assert fr.omdb_identity is False  # by-id never asserts identity

    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_omdb_keeps_tmdb(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbDetails())
        fake = _FakeOmdbSvc(by_id=_omdb(), count=settings.OMDB_DAILY_LIMIT)
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s2_item(), "movie", asyncio.Semaphore(1))

        assert fake.id_calls == 0            # over budget -> no OMDb call
        assert fr.omdb is None
        assert fr.result == "matched" and fr.data is not None  # TMDB result kept

    @pytest.mark.asyncio
    async def test_unconfigured_skips_omdb(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbDetails())
        fake = _FakeOmdbSvc(by_id=_omdb(), configured=False)
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s2_item(), "movie", asyncio.Semaphore(1))

        assert fake.id_calls == 0
        assert fr.omdb is None
        assert fr.result == "matched"

    @pytest.mark.asyncio
    async def test_by_id_raises_fail_open(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbDetails())
        fake = _FakeOmdbSvc(raises=True)
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s2_item(), "movie", asyncio.Semaphore(1))

        assert fake.id_calls == 1            # it did fire, but raised
        assert fr.omdb is None               # fail-open: OMDb attach swallowed
        assert fr.result == "matched" and fr.data is not None  # TMDB result kept

    @pytest.mark.asyncio
    async def test_by_id_cache_hit_no_http_recall(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        # Pre-seed the persistent OMDb cache with a found row for tt0088247.
        async with factory() as s:
            await omdb_scrape_cache.put(s, "tt0088247", "found", _omdb(), now_ms())
            await s.commit()
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbDetails())
        fake = _FakeOmdbSvc(by_id=_omdb())  # would count if called
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s2_item(), "movie", asyncio.Semaphore(1))

        assert fake.id_calls == 0            # served from the OMDb scrape cache
        assert fr.omdb is not None and fr.omdb.imdb_rating == 8.1
        assert fr.omdb_put is None           # cache hit -> nothing new to write

    @pytest.mark.asyncio
    async def test_fresh_nomatch_by_title_strong(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbNomatch())
        fake = _FakeOmdbSvc(by_title=_omdb(title="Terminator", year="1984", type="movie"))
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s4_item(), "movie", asyncio.Semaphore(1))

        assert fake.title_calls == 1 and fake.id_calls == 0
        assert fr.result == "nomatch"
        assert fr.omdb is not None
        assert fr.omdb_identity is True                 # year-exact + high sim + type
        assert fr.omdb_put == ("tt0088247", "found")

    @pytest.mark.asyncio
    async def test_fresh_nomatch_by_title_weak_year_off(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbNomatch())
        fake = _FakeOmdbSvc(by_title=_omdb(title="Terminator", year="1990", type="movie"))
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s4_item(), "movie", asyncio.Semaphore(1))

        assert fake.title_calls == 1
        assert fr.omdb is not None
        assert fr.omdb_identity is False                # year not exact -> weak
        assert fr.omdb_put == ("tt0088247", "found")    # still cached under its id

    @pytest.mark.asyncio
    async def test_fresh_nomatch_by_title_discard_low_sim(self, db_engine, monkeypatch):
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbNomatch())
        fake = _FakeOmdbSvc(by_title=_omdb(title="Zzz Totally Unrelated Picture", year="1984"))
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s4_item(), "movie", asyncio.Semaphore(1))

        assert fake.title_calls == 1
        assert fr.omdb is None                          # sim < 0.60 -> discard
        assert fr.omdb_identity is False
        assert fr.omdb_put is None                      # nothing written for a discard

    @pytest.mark.asyncio
    async def test_cached_nomatch_does_not_attempt_by_title(self, db_engine, monkeypatch):
        """A TMDB nomatch already in the scrape cache short-circuits `_resolve`
        (from_cache) before any OMDb-by-title call — the title-miss is already
        negatively cached at the TMDB layer."""
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        key = scrape_cache.make_key("movie", "Terminator", 1984)
        async with factory() as s:
            await scrape_cache.put(s, key, "movie", "nomatch", 0.4, None, now_ms())
            await s.commit()
        monkeypatch.setattr(ew, "async_session_factory", factory)
        # TMDB must not even be searched (cache hit) — a boom double proves it.
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbNomatch())
        fake = _FakeOmdbSvc(by_title=_omdb())
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = await ew._resolve(_s4_item(), "movie", asyncio.Semaphore(1))

        assert fr.from_cache is True and fr.result == "nomatch"
        assert fake.title_calls == 0                    # OMDb-by-title NOT attempted
        assert fr.omdb is None

    @pytest.mark.asyncio
    async def test_single_omdb_call_serves_tiebreak_end_to_end(self, db_engine, monkeypatch):
        """The ONE OMDb fetch in `_resolve` feeds the apply-phase contradiction
        tie-break — no second call (requirement 6)."""
        from app.workers.enrichment_worker import _apply_enrichment_results
        from app.workers import enrichment_worker as ew
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)
        monkeypatch.setattr(ew, "tmdb_service", _FakeTmdbMatchLowConf())
        # Contradicting OMDb payload (year off by 3, unrelated title).
        fake = _FakeOmdbSvc(by_id=_omdb(title="Zzz Totally Unrelated Picture Qqq", year="1987"))
        monkeypatch.setattr(ew, "omdb_service", fake)

        item = _s4_item()
        async with factory() as s:
            s.add(Media(rating_key=item.rating_key, server_id=item.server_id,
                        library_section_id="1", title="Terminator", type="movie"))
            await s.commit()

        fr = await ew._resolve(item, "movie", asyncio.Semaphore(1))
        assert fake.id_calls == 1  # the single fetch happened in _resolve

        async with factory() as s:
            merged = await s.merge(item)
            fr.item = merged
            await _apply_enrichment_results(s, [fr])
            await s.commit()

        assert fake.id_calls == 1              # apply did NOT re-fetch OMDb
        assert fr.result == "ambiguous"        # contradiction downgraded the match
        async with factory() as s:
            row = (await s.execute(
                select(Media.tmdb_id).where(
                    Media.rating_key == item.rating_key,
                    Media.server_id == item.server_id,
                )
            )).scalar_one()
        assert row is None                     # no id written on a downgrade


# ─── run() end-of-run recompute (design C3: "heal `display_rating` from the
# durable imdb_rating/tmdb_rating columns before generation + rebuild_all")──


class TestRunEndOfRunRecompute:
    @pytest.mark.asyncio
    async def test_run_heals_clobbered_display_rating(self, db_engine, monkeypatch):
        """QA regression (adversarial gap, no design-list test covered this):
        `run()` must actually execute `recompute_display_rating_stmt()` after
        the enrichment batches, healing a `display_rating` that a provider
        `content_hash` flip clobbered back to a raw value in `sync_worker`
        (design doc "Risks: content_hash-flip clobber"). The SQL statement
        itself is proven correct in isolation by `tests/test_rating_blend.py`
        — this test proves `run()` actually reaches and executes it end to
        end. No `EnrichmentQueue` items are seeded (TMDB/OMDb unconfigured by
        default in tests) so Phase 1/2 process zero items, isolating the
        end-of-run recompute step."""
        from app.workers import enrichment_worker as ew

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(ew, "async_session_factory", factory)

        async with factory() as s:
            s.add(Media(
                rating_key="vod_clobbered.mp4", server_id="xtream_a",
                library_section_id="1", title="Terminator", type="movie",
                imdb_rating=9.0, tmdb_rating=7.0,
                # Clobbered back to the raw TMDB rating by a sync content_hash
                # flip (sync_worker.py `update_keys`) — NOT the blend of 9.0/7.0.
                display_rating=7.0,
            ))
            await s.commit()

        await ew.run()

        async with factory() as s:
            healed = (await s.execute(
                select(Media.display_rating).where(Media.rating_key == "vod_clobbered.mp4")
            )).scalar_one()
        assert healed == 8.0  # blend(9.0, 7.0) == 8.0 — healed, not the clobbered 7.0
