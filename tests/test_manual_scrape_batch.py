"""ADR 0005 D8 (wave W5) — `manual_scrape_batch_worker` end to end, plus the
`scrape_review` queue helpers in `manual_scrape_service`.

Network is faked by monkeypatching the singletons the SERVICE imports
(`manual_scrape_service.tmdb_service`/`.omdb_service`) — the worker
deliberately goes through `mss.tmdb_service` too, so one patch covers both
the pairing pass and the scraping pass. Poster comparison is patched at
`poster_match_service.compare_posters` (no image bytes, no HTTP).

The fakes increment the real `count_requests()` ContextVar tally, so the
TMDB-budget stop is exercised by the same mechanism production uses.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.database import Media, ScrapeReview, TmdbScrapeCache
from app.services import manual_scrape_service as mss
from app.services import poster_match_service, unified_group_service
from app.services import tmdb_service as tmdb_module
from app.services.poster_match_service import PosterComparison
from app.services.tmdb_service import (
    CandidateSearch,
    MatchExtras,
    ScoredCandidate,
    TMDBMatch,
    TMDBSearchOutcome,
)
from app.workers import manual_scrape_batch_worker as worker
from tests.test_manual_scrape_apply import FakeOMDb, _details

pytestmark = pytest.mark.asyncio


# ─── fakes ─────────────────────────────────────────────────────────────────


def _tick() -> None:
    """Count one real TMDB HTTP attempt into the ambient tally, exactly where
    `TMDBService._request` does."""
    tally = tmdb_module._request_tally.get()
    if tally is not None:
        tally.count += 1


class BatchTMDB:
    """TMDB double covering both batch passes."""

    is_configured = True

    def __init__(self, *, details_by_id=None, imdb_to_tmdb=None, hits=None,
                 matched_id=None):
        self.details_by_id = dict(details_by_id or {})
        self.imdb_to_tmdb = dict(imdb_to_tmdb or {})
        self.hits = list(hits or [])
        self.matched_id = matched_id
        self.calls: list[tuple] = []

    async def find_by_imdb_id(self, imdb_id, kind):
        _tick()
        self.calls.append(("find_by_imdb_id", imdb_id))
        return self.imdb_to_tmdb.get(imdb_id)

    async def get_movie_details(self, tmdb_id):
        _tick()
        self.calls.append(("get_movie_details", tmdb_id))
        return self.details_by_id.get(tmdb_id)

    async def get_tv_details(self, tmdb_id):
        _tick()
        self.calls.append(("get_tv_details", tmdb_id))
        return self.details_by_id.get(tmdb_id)

    async def get_match_extras(self, tmdb_id, kind):
        _tick()
        self.calls.append(("get_match_extras", tmdb_id))
        details = self.details_by_id.get(tmdb_id)
        if details is None:
            return None
        return MatchExtras(
            tmdb_id=tmdb_id, kind=kind, imdb_id=details.imdb_id, title="The Matrix",
            original_title=None, year=details.year, overview=details.overview,
            poster_urls=["http://image.tmdb.org/t/p/w185/a.jpg"],
        )

    async def search_candidates(self, kind, title, year, *, language=None, summary=None):
        _tick()
        self.calls.append(("search_candidates", title, year, language))
        verdict = TMDBSearchOutcome("ambiguous")
        if self.matched_id is not None:
            verdict = TMDBSearchOutcome(
                "matched",
                match=TMDBMatch(tmdb_id=self.matched_id, title=title, year=year,
                                confidence=0.95, title_score=1.0),
            )
        return CandidateSearch(verdict=verdict, candidates=list(self.hits))


def _hit(tmdb_id=603, year=1999, confidence=0.95) -> ScoredCandidate:
    return ScoredCandidate(
        tmdb_id=tmdb_id, kind="movie", title="The Matrix", original_title=None,
        year=year, overview="A computer hacker.", poster_path="/a.jpg",
        vote_count=1000, title_score=1.0, confidence=confidence,
    )


def _comparison(badge="identical") -> PosterComparison:
    return PosterComparison(
        badge=badge, phash_distance=2, dhash_distance=3, image_score=0.94,
        best_poster_url="http://image.tmdb.org/t/p/w185/a.jpg",
        variants_compared=1, xtream_generic=False, shortcut=False,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Never leak the process-local job store / run guard / debounced rebuild
    tasks between tests."""
    yield
    worker._jobs.clear()
    worker._running = False
    unified_group_service._pending_rebuild_tasks.clear()
    unified_group_service._pending_session_factories.clear()
    unified_group_service._running_rebuild_tasks.clear()


@pytest.fixture
def patched(monkeypatch):
    """Default wiring: nothing resolves — individual tests override."""
    async def _compare(xtream_url, variants):
        return _comparison()

    monkeypatch.setattr(poster_match_service, "compare_posters", _compare)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())
    return monkeypatch


_page_offset = 0


async def _add(db_factory, **kwargs):
    # `media`'s unique pagination index is (server_id, library_section_id,
    # filter, sort_order, page_offset) — each fixture row needs its own slot.
    global _page_offset
    _page_offset += 1
    defaults = dict(
        library_section_id="1", type="movie", page_offset=_page_offset,
        is_in_allowed_categories=True, thumb_url="http://xtream/poster.jpg",
    )
    defaults.update(kwargs)
    async with db_factory() as s:
        s.add(Media(**defaults))
        await s.commit()


async def _run(db_factory, *, media_type="all", dry_run=False, max_items=None) -> dict:
    job_id = f"scrape_batch_test_{len(worker._jobs)}"
    worker._register(
        job_id, worker._new_job(job_id, media_type=media_type, dry_run=dry_run),
    )
    await worker.run(
        job_id, session_factory=db_factory, media_type=media_type,
        dry_run=dry_run, max_items=max_items,
    )
    return worker._jobs[job_id]


async def _row(db_factory, rating_key="vod_1.mp4", server_id="xtream_a") -> Media:
    async with db_factory() as s:
        return (await s.execute(
            select(Media).where(
                Media.rating_key == rating_key, Media.server_id == server_id,
            )
        )).scalars().first()


async def _reviews(db_factory) -> list[ScrapeReview]:
    async with db_factory() as s:
        return list((await s.execute(select(ScrapeReview))).scalars().all())


# ─── pass 1: pairing ───────────────────────────────────────────────────────


async def test_pairing_resolves_tmdb_from_a_lone_imdb_id(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999, imdb_id="tt0133093",
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, imdb_to_tmdb={"tt0133093": 603},
    ))

    job = await _run(db_factory)

    assert job["status"] == "completed"
    assert job["paired"] == 1
    row = await _row(db_factory)
    assert row.tmdb_id == "603"
    assert row.imdb_id == "tt0133093"
    assert row.match_source == "batch_pair"
    assert await _reviews(db_factory) == []


async def test_pairing_resolves_imdb_from_a_lone_tmdb_id(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999, tmdb_id="603",
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(details_by_id={603: _details()}))

    job = await _run(db_factory)

    assert job["paired"] == 1
    row = await _row(db_factory)
    assert row.imdb_id == "tt0133093"
    assert row.match_source == "batch_pair"


async def test_unpairable_item_goes_to_review(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="Obscure", year=1999, imdb_id="tt9999999",
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB())

    job = await _run(db_factory)

    assert job["paired"] == 0
    assert job["queuedForReview"] == 1
    reviews = await _reviews(db_factory)
    assert len(reviews) == 1
    assert reviews[0].reason == "no_tmdb_for_imdb"
    assert reviews[0].status == "pending"
    # The row keeps its lone id: a failed pairing writes nothing to `media`.
    row = await _row(db_factory)
    assert row.tmdb_id in (None, "")


# ─── pass 2: scraping ──────────────────────────────────────────────────────


async def test_rule_a_auto_applies(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999,
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, hits=[_hit()], matched_id=603,
    ))

    job = await _run(db_factory)

    assert job["autoAppliedA"] == 1
    assert job["queuedForReview"] == 0
    row = await _row(db_factory)
    assert (row.tmdb_id, row.imdb_id) == ("603", "tt0133093")
    assert row.match_source == "batch_auto"
    assert row.match_locked is True


async def test_rule_b_is_gated_by_the_flag(db_factory, patched):
    """Identical poster, no text-safe verdict: review with the flag off,
    auto-apply with it on."""
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999,
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, hits=[_hit()],  # matched_id=None
    ))
    patched.setattr(settings, "SCRAPE_BATCH_POSTER_ONLY_AUTO", False)

    job = await _run(db_factory)
    assert (job["autoAppliedB"], job["queuedForReview"]) == (0, 1)
    assert (await _row(db_factory)).tmdb_id in (None, "")

    # Clear the review so the item is selectable again, then flip the flag.
    async with db_factory() as s:
        for r in (await s.execute(select(ScrapeReview))).scalars().all():
            await s.delete(r)
        await s.commit()
    patched.setattr(settings, "SCRAPE_BATCH_POSTER_ONLY_AUTO", True)

    job = await _run(db_factory)
    assert job["autoAppliedB"] == 1
    assert (await _row(db_factory)).match_source == "batch_auto"


async def test_review_row_stores_the_top_candidates(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999,
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()},
        hits=[_hit(tmdb_id=600 + i, confidence=0.9 - i / 100) for i in range(8)],
    ))

    async def _different(xtream_url, variants):
        return _comparison("different")

    patched.setattr(poster_match_service, "compare_posters", _different)

    job = await _run(db_factory)

    assert job["queuedForReview"] == 1
    review = (await _reviews(db_factory))[0]
    candidates = json.loads(review.candidates_json)
    # Capped at 5 (ADR 0005 D8) with the frozen key set.
    assert len(candidates) == 5
    assert set(candidates[0]) >= {
        "provider", "tmdb_id", "imdb_id", "media_type", "title", "year",
        "poster_url", "title_score", "text_confidence", "combined_score",
        "text_safe", "recommended", "badge", "phash_distance", "image_score",
    }
    assert review.best_confidence is not None


async def test_items_with_an_open_review_are_skipped(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999,
    )
    await mss.upsert_review(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        title="The Matrix", year=1999, reason="no_candidates", candidates=[],
        session_factory=db_factory,
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, hits=[_hit()], matched_id=603,
    ))

    job = await _run(db_factory)

    assert job["scanned"] == 0
    assert (await _row(db_factory)).tmdb_id in (None, "")


async def test_locked_rows_are_never_touched(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999, match_locked=True, match_source="manual",
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, hits=[_hit()], matched_id=603,
    ))

    job = await _run(db_factory)

    assert job["scanned"] == 0
    assert (await _row(db_factory)).tmdb_id in (None, "")


# ─── budgets, cancellation, dry run ────────────────────────────────────────


async def test_tmdb_budget_stops_the_run(db_factory, patched):
    for i in range(5):
        await _add(
            db_factory, rating_key=f"vod_{i}.mp4", server_id="xtream_a",
            title=f"Film {i}", year=1999,
        )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, hits=[_hit()], matched_id=603,
    ))
    patched.setattr(settings, "SCRAPE_BATCH_TMDB_LIMIT", 1)
    patched.setattr(settings, "SCRAPE_BATCH_CONCURRENCY", 1)

    job = await _run(db_factory)

    assert job["budgetExhausted"] is True
    assert job["scanned"] < 5
    assert job["status"] == "completed"


async def test_max_items_caps_the_scan(db_factory, patched):
    for i in range(4):
        await _add(
            db_factory, rating_key=f"vod_{i}.mp4", server_id="xtream_a",
            title=f"Film {i}", year=1999,
        )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, hits=[_hit()], matched_id=603,
    ))

    job = await _run(db_factory, max_items=2)
    assert job["scanned"] == 2


async def test_cancel_stops_between_items(db_factory, patched):
    for i in range(4):
        await _add(
            db_factory, rating_key=f"vod_{i}.mp4", server_id="xtream_a",
            title=f"Film {i}", year=1999,
        )
    patched.setattr(settings, "SCRAPE_BATCH_CONCURRENCY", 1)

    class CancellingTMDB(BatchTMDB):
        def __init__(self, job_getter, **kwargs):
            super().__init__(**kwargs)
            self._job_getter = job_getter

        async def search_candidates(self, *args, **kwargs):
            worker.cancel(self._job_getter())
            return await super().search_candidates(*args, **kwargs)

    job_id = "scrape_batch_cancel"
    worker._register(job_id, worker._new_job(job_id, media_type="all", dry_run=False))
    patched.setattr(mss, "tmdb_service", CancellingTMDB(
        lambda: job_id, details_by_id={603: _details()}, hits=[_hit()], matched_id=603,
    ))

    await worker.run(
        job_id, session_factory=db_factory, media_type="all", dry_run=False,
    )
    job = worker._jobs[job_id]
    assert job["status"] == "canceled"
    assert job["scanned"] < 4


async def test_dry_run_writes_absolutely_nothing(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999,
    )
    await _add(
        db_factory, rating_key="vod_2.mp4", server_id="xtream_a",
        title="Obscure", year=1988, imdb_id="tt9999999",
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, hits=[_hit()], matched_id=603,
    ))

    async def _counts() -> dict[str, int]:
        async with db_factory() as s:
            return {
                "media_ids": (await s.execute(
                    select(func.count()).select_from(Media).where(
                        Media.tmdb_id.isnot(None)
                    )
                )).scalar(),
                "locked": (await s.execute(
                    select(func.count()).select_from(Media).where(
                        Media.match_locked == True  # noqa: E712
                    )
                )).scalar(),
                "reviews": (await s.execute(
                    select(func.count()).select_from(ScrapeReview)
                )).scalar(),
                "scrape_cache": (await s.execute(
                    select(func.count()).select_from(TmdbScrapeCache)
                )).scalar(),
            }

    before = await _counts()
    job = await _run(db_factory, dry_run=True)
    after = await _counts()

    assert before == after
    # …but the run still REPORTS what it would have done, and the histogram
    # that calibrates rule B.
    assert job["autoAppliedA"] == 1
    assert job["queuedForReview"] == 1
    assert sum(job["distanceHistogram"].values()) == 1
    assert job["distanceHistogram"]["0-3"] == 1


# ─── job registry ──────────────────────────────────────────────────────────


async def test_start_refuses_a_second_run(db_factory, patched):
    patched.setattr(mss, "tmdb_service", BatchTMDB())
    job_id = worker.start(
        media_type="all", dry_run=True, session_factory=db_factory,
    )
    assert worker.is_running() is True
    with pytest.raises(worker.BatchAlreadyRunningError):
        worker.start(media_type="all", dry_run=True, session_factory=db_factory)

    # Let the background task drain so the guard is released.
    import asyncio

    for _ in range(50):
        if worker.get(job_id)["status"] in ("completed", "failed", "canceled"):
            break
        await asyncio.sleep(0.01)
    assert worker.is_running() is False


async def test_jobs_cap_evicts_oldest():
    for i in range(worker.JOBS_CAP + 3):
        worker._register(f"j{i}", worker._new_job(f"j{i}", media_type="all", dry_run=True))
    assert len(worker._jobs) == worker.JOBS_CAP
    assert worker.get("j0") is None
    assert worker.get_latest()["jobId"] == f"j{worker.JOBS_CAP + 2}"


async def test_cancel_unknown_job_is_false():
    assert worker.cancel("nope") is False


# ─── review queue helpers ──────────────────────────────────────────────────


async def test_reviews_are_listed_paginated_and_orphan_masked(db_session, db_factory):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a", title="Kept",
    )
    await mss.upsert_review(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        title="Kept", year=1999, reason="no_candidates", candidates=[],
        session_factory=db_factory,
    )
    # Orphan: no `media` row behind it.
    await mss.upsert_review(
        server_id="xtream_a", rating_key="gone.mp4", media_type="movie",
        title="Gone", year=1999, reason="no_candidates", candidates=[],
        session_factory=db_factory,
    )

    async with db_factory() as s:
        rows, total = await mss.list_reviews(s, media_type="movie", page=1, page_size=10)
    assert total == 1
    assert rows[0].rating_key == "vod_1.mp4"


async def test_dismiss_then_batch_skips_the_item(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999,
    )
    await mss.upsert_review(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        title="The Matrix", year=1999, reason="no_candidates", candidates=[],
        session_factory=db_factory,
    )
    assert await mss.dismiss_review(
        "xtream_a", "vod_1.mp4", session_factory=db_factory,
    ) is True

    async with db_factory() as s:
        _rows, total = await mss.list_reviews(s, media_type=None, page=1, page_size=10)
    assert total == 0

    patched.setattr(mss, "tmdb_service", BatchTMDB(
        details_by_id={603: _details()}, hits=[_hit()], matched_id=603,
    ))
    job = await _run(db_factory)
    assert job["scanned"] == 0  # dismissed means "leave it alone", for good


async def test_apply_resolves_the_pending_review_in_the_same_write(db_factory, patched):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a",
        title="The Matrix", year=1999,
    )
    await mss.upsert_review(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        title="The Matrix", year=1999, reason="no_identical_poster", candidates=[],
        session_factory=db_factory,
    )
    patched.setattr(mss, "tmdb_service", BatchTMDB(details_by_id={603: _details()}))

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory,
        schedule_rebuild=False,
    )
    assert outcome.status == "applied"

    review = await mss.load_review(
        "xtream_a", "vod_1.mp4", session_factory=db_factory,
    )
    assert review.status == "resolved"
    assert review.resolved_at is not None


async def test_catalogue_stats_and_filter_see_pending_reviews(db_factory):
    await _add(
        db_factory, rating_key="vod_1.mp4", server_id="xtream_a", title="Flagged",
    )
    await _add(
        db_factory, rating_key="vod_2.mp4", server_id="xtream_a", title="Clean",
    )
    await mss.upsert_review(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        title="Flagged", year=None, reason="no_candidates", candidates=[],
        session_factory=db_factory,
    )

    async with db_factory() as s:
        stats = await mss.catalogue_stats(s)
        page = await mss.list_catalogue(s, media_type="movie", id_filter="review")
        all_page = await mss.list_catalogue(s, media_type="movie", id_filter="all")

    assert stats["movie"].review_pending == 1
    assert [i.rating_key for i in page.items] == ["vod_1.mp4"]
    assert all_page.review_keys == {("xtream_a", "vod_1.mp4")}


async def test_deleting_an_account_purges_its_reviews(db_session, db_factory):
    from app.models.database import XtreamAccount
    from app.services import account_service

    async with db_factory() as s:
        s.add(XtreamAccount(
            id="acc1", label="Acc", base_url="http://a.test", port=80,
            username="u", password="p", status="Active", max_connections=1,
            allowed_formats="", last_synced_at=0, is_active=True, created_at=0,
            category_filter_mode="all",
        ))
        await s.commit()
    await mss.upsert_review(
        server_id="xtream_acc1", rating_key="vod_1.mp4", media_type="movie",
        title="Gone", year=None, reason="no_candidates", candidates=[],
        session_factory=db_factory,
    )

    async with db_factory() as s:
        await account_service.delete_account_cascade(s, "acc1")
        await s.commit()

    assert await _reviews(db_factory) == []


# ─── real WAL lock (modelled on tests/test_manual_scrape_apply.py) ─────────


async def test_upsert_review_survives_a_real_wal_lock(tmp_path):
    """`upsert_review` is a new writer, so it gets the same real-lock proof
    every critical writer in this repo carries (CLAUDE.md piège 8): a genuine
    concurrent WAL writer, not a synthetic exception — that is what caught
    two real bugs during the ADR 0004 conversion."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from tests.test_manual_scrape_apply import (
        _init_wal_schema,
        _make_short_timeout_engine,
        _start_blocker,
    )

    db_path = str(tmp_path / "scrape_review_lock.db")
    await _init_wal_schema(db_path)
    engine = _make_short_timeout_engine(db_path)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    blocker = _start_blocker(db_path, hold_seconds=0.35)
    try:
        await mss.upsert_review(
            server_id="xtream_a", rating_key="vod_lock.mp4", media_type="movie",
            title="Locked Write", year=1999, reason="no_candidates",
            candidates=[], session_factory=factory,
        )
    finally:
        blocker.join(timeout=5)

    row = await mss.load_review(
        "xtream_a", "vod_lock.mp4", session_factory=factory,
    )
    assert row is not None and row.status == "pending"
    await engine.dispose()


async def test_upsert_review_reopens_a_resolved_row(db_factory):
    """Same (rating_key, server_id) twice: the PK conflict must UPDATE and put
    the row back to `pending` with the fresh reason — never raise, never
    duplicate."""
    await mss.upsert_review(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        title="One", year=1999, reason="no_candidates", candidates=[],
        session_factory=db_factory,
    )
    await mss.resolve_review("xtream_a", "vod_1.mp4", session_factory=db_factory)

    await mss.upsert_review(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        title="One", year=1999, reason="ambiguous_posters",
        candidates=[], session_factory=db_factory,
    )

    rows = await _reviews(db_factory)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].reason == "ambiguous_posters"
    assert rows[0].resolved_at is None
