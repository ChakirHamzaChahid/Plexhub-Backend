"""ADR 0005 D6/D9 (Wave W2) — `manual_scrape_service.apply_candidate` /
`lookup` / `unlock` / `clear_ids` / `load_row`, and
`unified_group_service.schedule_rebuild`/`flush_scheduled`.

Network (TMDB/OMDb) is mocked by monkeypatching the singleton clients the
service imports (`manual_scrape_service.tmdb_service`/`.omdb_service`) with
small fakes — same convention `tests/test_manual_scrape_lock.py` already
uses for `validate_id_consistency` — rather than respx, so the write phase
of `apply_candidate` (which must make ZERO network calls, CLAUDE.md piège 8)
can be asserted on directly via call-count.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.database import Base, EnrichmentQueue, Media, TmdbScrapeCache
from app.services import manual_scrape_service as mss
from app.services import unified_group_service
from app.services.omdb_service import OMDbData
from app.services.tmdb_service import TMDBEnrichmentData


# ─── fakes ───────────────────────────────────────────────────────────────


def _details(tmdb_id=603, imdb_id="tt0133093", **extra) -> TMDBEnrichmentData:
    base = dict(
        tmdb_id=tmdb_id, imdb_id=imdb_id, overview="A computer hacker.",
        poster_url="http://img/matrix.jpg", backdrop_url="http://img/matrix_bg.jpg",
        vote_average=8.2, genres="Action, Sci-Fi", year=1999, cast="Keanu Reeves",
        tmdb_rating=8.2, tmdb_votes=20000,
    )
    base.update(extra)
    return TMDBEnrichmentData(**base)


class FakeTMDB:
    is_configured = True

    def __init__(self, details_by_id=None, imdb_to_tmdb=None):
        self.details_by_id = dict(details_by_id or {})
        self.imdb_to_tmdb = dict(imdb_to_tmdb or {})
        self.calls: list[tuple] = []

    async def find_by_imdb_id(self, imdb_id, kind):
        self.calls.append(("find_by_imdb_id", imdb_id, kind))
        return self.imdb_to_tmdb.get(imdb_id)

    async def _details(self, tmdb_id):
        d = self.details_by_id.get(tmdb_id)
        if d is None:
            req = httpx.Request("GET", "https://api.themoviedb.org/3/movie/x")
            raise httpx.HTTPStatusError(
                "not found", request=req, response=httpx.Response(404, request=req),
            )
        return d

    async def get_movie_details(self, tmdb_id):
        self.calls.append(("get_movie_details", tmdb_id))
        return await self._details(tmdb_id)

    async def get_tv_details(self, tmdb_id):
        self.calls.append(("get_tv_details", tmdb_id))
        return await self._details(tmdb_id)

    async def get_match_extras(self, tmdb_id, kind):
        self.calls.append(("get_match_extras", tmdb_id, kind))
        d = self.details_by_id.get(tmdb_id)
        if d is None:
            return None
        from app.services.tmdb_service import MatchExtras

        return MatchExtras(
            tmdb_id=d.tmdb_id, kind=kind, imdb_id=d.imdb_id, title="Title",
            original_title=None, year=d.year, overview=d.overview,
            poster_urls=[d.poster_url] if d.poster_url else [],
        )


class FakeOMDb:
    is_configured = True

    def __init__(self, data_by_imdb=None):
        self.data_by_imdb = dict(data_by_imdb or {})
        self.calls: list[str] = []

    def get_request_count(self):
        return 0

    async def get_by_imdb_id(self, imdb_id):
        self.calls.append(imdb_id)
        return self.data_by_imdb.get(imdb_id)


@pytest.fixture(autouse=True)
def _flush_pending_rebuilds():
    """Never let a debounced background rebuild task from one test leak
    (pending or running) into the next."""
    yield
    unified_group_service._pending_rebuild_tasks.clear()
    unified_group_service._pending_session_factories.clear()
    unified_group_service._running_rebuild_tasks.clear()


# ─── apply_candidate: full success, fill mode ──────────────────────────────


async def test_apply_fill_mode_writes_identity_and_locks(db_session, db_factory, monkeypatch):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="The Matrix", type="movie", year=1999, page_offset=0,
    ))
    await db_session.commit()

    tmdb = FakeTMDB(details_by_id={603: _details()})
    omdb = FakeOMDb()
    monkeypatch.setattr(mss, "tmdb_service", tmdb)
    monkeypatch.setattr(mss, "omdb_service", omdb)

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory,
        schedule_rebuild=False,
    )
    assert outcome.status == "applied"
    assert outcome.mode == "fill"
    assert outcome.new_tmdb_id == "603"
    assert outcome.new_imdb_id == "tt0133093"
    assert tmdb.calls == [("get_movie_details", 603)]  # exactly one network call

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
    assert row.tmdb_id == "603"
    assert row.imdb_id == "tt0133093"
    assert row.unification_id == "imdb://tt0133093"
    assert row.history_group_key == "imdb://tt0133093"
    assert row.match_locked is True
    assert row.match_source == "manual"
    assert row.tmdb_match_confidence == pytest.approx(1.0)
    assert row.updated_at > 0
    assert row.summary == "A computer hacker."


async def test_apply_overwrites_the_title_scrape_cache_entry(db_session, db_factory, monkeypatch):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="The Matrix", type="movie", year=1999, page_offset=0,
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )

    async with db_factory() as s:
        row = (await s.execute(
            select(TmdbScrapeCache).where(TmdbScrapeCache.cache_key == "movie|matrix|1999")
        )).scalars().one()
    assert row.result == "matched"
    assert row.tmdb_id == "603"


async def test_apply_marks_enrichment_queue_item_done(db_session, db_factory, monkeypatch):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="The Matrix", type="movie", year=1999, page_offset=0,
    ))
    db_session.add(EnrichmentQueue(
        rating_key="vod_1.mp4", server_id="xtream_a", media_type="movie",
        title="The Matrix", status="pending", attempts=0, created_at=0,
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )

    async with db_factory() as s:
        q = (await s.execute(
            select(EnrichmentQueue).where(EnrichmentQueue.rating_key == "vod_1.mp4")
        )).scalars().one()
    assert q.status == "done"
    assert q.processed_at is not None


async def test_apply_schedules_a_snapshot_rebuild(db_session, db_factory, monkeypatch):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="The Matrix", type="movie", year=1999, page_offset=0,
        is_in_allowed_categories=True,
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())
    monkeypatch.setattr(unified_group_service.settings, "SCRAPE_REBUILD_DEBOUNCE_SECONDS", 0.01)

    from app.models.database import MediaGroup

    async with db_factory() as s:
        assert (await s.execute(select(MediaGroup))).scalars().all() == []

    await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory,
    )
    await unified_group_service.flush_scheduled()

    async with db_factory() as s:
        groups = (await s.execute(select(MediaGroup))).scalars().all()
    assert len(groups) == 1
    assert groups[0].group_key == "imdb://tt0133093"


# ─── apply_candidate: replace mode (correcting a wrong prior identity) ─────


async def test_apply_replace_mode_when_old_ids_differ(db_session, db_factory, monkeypatch):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Wrong Movie", type="movie", year=1999, page_offset=0,
        tmdb_id="999", imdb_id="tt9999999",
        resolved_thumb_url="http://old/wrong-thumb.jpg",
        resolved_art_url="http://old/wrong-art.jpg",
        tvdb_id="old-tvdb",
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.mode == "replace"

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
    assert row.tmdb_id == "603"
    assert row.imdb_id == "tt0133093"
    # A wrong match's image is fully replaced by the fresh one (not COALESCEd).
    assert row.resolved_thumb_url == "http://img/matrix.jpg"
    assert row.resolved_art_url == "http://img/matrix_bg.jpg"


async def test_apply_replace_mode_no_rating_at_all_nulls_display_rating(
    db_session, db_factory, monkeypatch,
):
    """W0 review follow-up (a): replace mode with neither an OMDb nor a TMDB
    rating in hand must NOT keep the wrong film's stale display_rating."""
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Wrong Movie", type="movie", year=1999, page_offset=0,
        tmdb_id="999", imdb_id="tt9999999", display_rating=9.9,
    ))
    await db_session.commit()

    monkeypatch.setattr(
        mss, "tmdb_service",
        FakeTMDB(details_by_id={603: _details(vote_average=None, tmdb_rating=None)}),
    )
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())  # unconfigured-shaped, no data

    await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
    assert row.display_rating == 0.0


async def test_apply_replace_mode_preserves_xxx_content_rating_for_adult_media(
    db_session, db_factory, monkeypatch,
):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Adult Film", type="movie", year=1999, page_offset=0,
        tmdb_id="999", imdb_id="tt9999999",
        is_adult=True, content_rating="XXX",
    ))
    await db_session.commit()

    monkeypatch.setattr(
        mss, "tmdb_service",
        FakeTMDB(details_by_id={603: _details(content_rating="R")}),
    )
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
    assert row.content_rating == "XXX"


# ─── apply_candidate: conflict / force ─────────────────────────────────────


async def test_apply_conflict_with_existing_row_blocks_without_force(
    db_session, db_factory, monkeypatch,
):
    db_session.add_all([
        Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Target", type="movie", year=1999, page_offset=0,
        ),
        Media(
            rating_key="vod_other.mp4", server_id="xtream_a", library_section_id="1",
            title="Other Row", type="movie", year=1999, page_offset=1,
            tmdb_id="603", imdb_id="tt7777777",  # same tmdb, DIFFERENT imdb
        ),
    ])
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "conflict"
    assert ("xtream_a", "vod_other.mp4", "603", "tt7777777") in outcome.conflicts

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
    assert row.tmdb_id is None  # zero writes on conflict
    assert row.match_locked is False


async def test_apply_conflict_force_true_writes_only_the_target(
    db_session, db_factory, monkeypatch,
):
    db_session.add_all([
        Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Target", type="movie", year=1999, page_offset=0,
        ),
        Media(
            rating_key="vod_other.mp4", server_id="xtream_a", library_section_id="1",
            title="Other Row", type="movie", year=1999, page_offset=1,
            tmdb_id="603", imdb_id="tt7777777",
        ),
    ])
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, force=True,
        session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "applied"

    async with db_factory() as s:
        target = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
        other = (await s.execute(
            select(Media).where(Media.rating_key == "vod_other.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
    assert target.tmdb_id == "603"
    assert target.imdb_id == "tt0133093"
    # The other conflicting row is NEVER touched, even with force=True.
    assert other.imdb_id == "tt7777777"
    assert other.match_locked is False


# ─── apply_candidate: propagate ────────────────────────────────────────────


async def test_apply_propagate_fills_unidentified_twins_only(
    db_session, db_factory, monkeypatch,
):
    db_session.add_all([
        Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Some Show", type="movie", year=1999, page_offset=0,
            unification_id="title_some_show_1999",
        ),
        Media(
            rating_key="vod_twin.mp4", server_id="xtream_b", library_section_id="1",
            title="Some Show", type="movie", year=1999, page_offset=1,
            unification_id="title_some_show_1999",
        ),
        Media(
            rating_key="vod_locked_twin.mp4", server_id="xtream_c", library_section_id="1",
            title="Some Show", type="movie", year=1999, page_offset=2,
            unification_id="title_some_show_1999",
            match_locked=True, match_source="manual",
        ),
        Media(
            rating_key="vod_has_id.mp4", server_id="xtream_d", library_section_id="1",
            title="Some Show", type="movie", year=1999, page_offset=3,
            unification_id="title_some_show_1999", tmdb_id="42",
        ),
    ])
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, propagate=True,
        session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "applied"
    assert outcome.propagated == 1

    async with db_factory() as s:
        twin = (await s.execute(
            select(Media).where(Media.rating_key == "vod_twin.mp4")
        )).scalars().one()
        locked_twin = (await s.execute(
            select(Media).where(Media.rating_key == "vod_locked_twin.mp4")
        )).scalars().one()
        has_id = (await s.execute(
            select(Media).where(Media.rating_key == "vod_has_id.mp4")
        )).scalars().one()
    assert twin.tmdb_id == "603"
    assert twin.imdb_id == "tt0133093"
    assert twin.match_locked is True
    assert twin.match_source == "manual"
    assert locked_twin.tmdb_id is None  # locked twin untouched
    assert has_id.tmdb_id == "42"  # already-identified row untouched


async def test_apply_without_propagate_leaves_twins_alone(db_session, db_factory, monkeypatch):
    db_session.add_all([
        Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Some Show", type="movie", year=1999, page_offset=0,
            unification_id="title_some_show_1999",
        ),
        Media(
            rating_key="vod_twin.mp4", server_id="xtream_b", library_section_id="1",
            title="Some Show", type="movie", year=1999, page_offset=1,
            unification_id="title_some_show_1999",
        ),
    ])
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.propagated == 0

    async with db_factory() as s:
        twin = (await s.execute(select(Media).where(Media.rating_key == "vod_twin.mp4"))).scalars().one()
    assert twin.tmdb_id is None


# ─── apply_candidate: not_found / provider_not_found / skipped_locked ─────


async def test_apply_not_found(db_factory, monkeypatch):
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB())
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())
    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="nope", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "not_found"


async def test_apply_provider_not_found_on_404(db_session, db_factory, monkeypatch):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Target", type="movie", year=1999, page_offset=0,
    ))
    await db_session.commit()
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB())  # empty -> 404
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=999999, imdb_id=None, session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "provider_not_found"


async def test_apply_skipped_locked_for_non_manual_source(db_session, db_factory, monkeypatch):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Already Locked", type="movie", year=1999, page_offset=0,
        match_locked=True, match_source="manual",
        tmdb_id="1", imdb_id="tt0000001",
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, source="batch_auto",
        session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "skipped_locked"

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
    assert row.tmdb_id == "1"  # untouched


async def test_apply_manual_source_overrides_an_existing_lock(
    db_session, db_factory, monkeypatch,
):
    """A manual apply may always re-lock/override a previously locked row —
    only non-"manual" sources respect the lock guard."""
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Already Locked", type="movie", year=1999, page_offset=0,
        match_locked=True, match_source="manual",
        tmdb_id="1", imdb_id="tt0000001",
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, source="manual",
        session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "applied"


# ─── apply_candidate: imdb-only identity via OMDb (no TMDB match at all) ──


async def test_apply_imdb_only_via_omdb_when_tmdb_cannot_resolve(
    db_session, db_factory, monkeypatch,
):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Obscure Film", type="movie", year=1999, page_offset=0,
        tmdb_id="999",  # a stale wrong tmdb id
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(imdb_to_tmdb={}))  # never resolves
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb(data_by_imdb={
        "tt1234567": OMDbData(
            title="Obscure Film", year="1999", runtime_minutes=90, genre="Drama",
            director=None, actors=None, plot="An obscure film.",
            imdb_rating=6.5, imdb_votes=100, type="movie", imdb_id="tt1234567",
        ),
    }))

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=None, imdb_id="tt1234567",
        session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "applied"
    assert outcome.new_tmdb_id is None
    assert outcome.new_imdb_id == "tt1234567"

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4", Media.server_id == "xtream_a")
        )).scalars().one()
    assert row.tmdb_id is None  # stale wrong id explicitly cleared
    assert row.imdb_id == "tt1234567"
    assert row.unification_id == "imdb://tt1234567"
    assert row.imdb_rating == pytest.approx(6.5)
    assert row.match_locked is True


async def test_apply_no_id_at_all_raises_value_error(db_factory):
    with pytest.raises(ValueError):
        await mss.apply_candidate(
            server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
            tmdb_id=None, imdb_id=None, session_factory=db_factory,
        )


# ─── unlock / clear_ids / load_row ─────────────────────────────────────────


async def test_unlock_clears_lock_keeps_ids(db_session, db_factory):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Locked", type="movie", year=1999, page_offset=0,
        match_locked=True, match_source="manual", tmdb_id="603", imdb_id="tt0133093",
    ))
    await db_session.commit()

    ok = await mss.unlock("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert ok is True

    row = await mss.load_row("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert row.match_locked is False
    assert row.match_source is None
    assert row.tmdb_id == "603"  # ids untouched


async def test_unlock_unknown_row_returns_false(db_factory):
    assert await mss.unlock("xtream_a", "nope", session_factory=db_factory) is False


async def test_clear_ids_nulls_ids_and_recomputes_title_unification(db_session, db_factory):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Some Movie", type="movie", year=2020, page_offset=0,
        match_locked=True, match_source="manual", tmdb_id="603", imdb_id="tt0133093",
        unification_id="imdb://tt0133093", history_group_key="imdb://tt0133093",
    ))
    await db_session.commit()

    ok = await mss.clear_ids("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert ok is True

    row = await mss.load_row("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert row.tmdb_id is None
    assert row.imdb_id is None
    assert row.unification_id == "title_some_movie_2020"
    assert row.history_group_key == "title_some_movie_2020"
    assert row.match_locked is False  # default lock=False
    assert row.match_source is None


async def test_clear_ids_with_lock_true_relocks(db_session, db_factory):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Some Movie", type="movie", year=2020, page_offset=0,
        tmdb_id="603", imdb_id="tt0133093",
    ))
    await db_session.commit()

    await mss.clear_ids("xtream_a", "vod_1.mp4", lock=True, session_factory=db_factory)
    row = await mss.load_row("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert row.match_locked is True
    assert row.match_source == "manual"


async def test_clear_ids_unknown_row_returns_false(db_factory):
    assert await mss.clear_ids("xtream_a", "nope", session_factory=db_factory) is False


async def test_clear_ids_schedules_rebuild(db_session, db_factory, monkeypatch):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Some Movie", type="movie", year=2020, page_offset=0,
        tmdb_id="603", imdb_id="tt0133093", is_in_allowed_categories=True,
    ))
    await db_session.commit()
    monkeypatch.setattr(unified_group_service.settings, "SCRAPE_REBUILD_DEBOUNCE_SECONDS", 0.01)

    await mss.clear_ids("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert "movie" in unified_group_service._pending_rebuild_tasks
    await unified_group_service.flush_scheduled()


async def test_load_row_missing_returns_none(db_factory):
    assert await mss.load_row("xtream_a", "nope", session_factory=db_factory) is None


# ─── lookup ─────────────────────────────────────────────────────────────


async def test_lookup_by_bare_tt_id(monkeypatch):
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(
        details_by_id={603: _details()}, imdb_to_tmdb={"tt0133093": 603},
    ))
    result = await mss.lookup("tt0133093", media_type="movie", xtream_poster_url=None)
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert c.provider == "tmdb"
    assert c.tmdb_id == 603
    assert c.imdb_id == "tt0133093"
    assert c.recommended is False
    assert c.poster is None


async def test_lookup_by_imdb_url(monkeypatch):
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(
        details_by_id={603: _details()}, imdb_to_tmdb={"tt0133093": 603},
    ))
    result = await mss.lookup(
        "https://www.imdb.com/title/tt0133093/", media_type="movie", xtream_poster_url=None,
    )
    assert result.candidates[0].tmdb_id == 603


async def test_lookup_by_tmdb_int_id(monkeypatch):
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    result = await mss.lookup("603", media_type="movie", xtream_poster_url=None)
    assert result.candidates[0].tmdb_id == 603


async def test_lookup_by_tmdb_url(monkeypatch):
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    result = await mss.lookup(
        "https://www.themoviedb.org/movie/603", media_type="movie", xtream_poster_url=None,
    )
    assert result.candidates[0].tmdb_id == 603


async def test_lookup_imdb_only_falls_back_to_omdb_when_tmdb_unresolvable(
    monkeypatch, db_factory,
):
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(imdb_to_tmdb={}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb(data_by_imdb={
        "tt9999999": OMDbData(
            title="Obscure", year="2005", runtime_minutes=90, genre=None,
            director=None, actors=None, plot="An obscure plot.",
            imdb_rating=5.0, imdb_votes=10, type="movie", imdb_id="tt9999999",
        ),
    }))

    result = await mss.lookup("tt9999999", media_type="movie", xtream_poster_url=None)
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert c.provider == "omdb"
    assert c.tmdb_id is None
    assert c.imdb_id == "tt9999999"
    assert c.year == 2005
    assert c.recommended is False


async def test_lookup_unrecognized_id_raises_value_error():
    with pytest.raises(ValueError):
        await mss.lookup("not an id at all !!", media_type="movie", xtream_poster_url=None)


async def test_lookup_nothing_found_returns_empty_candidates(monkeypatch, db_factory):
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(imdb_to_tmdb={}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb(data_by_imdb={}))

    result = await mss.lookup("tt0000000", media_type="movie", xtream_poster_url=None)
    assert result.candidates == []
    assert result.text_verdict == "nomatch"


# ─── unified_group_service.schedule_rebuild / flush_scheduled ────────────


async def test_schedule_rebuild_debounces_a_burst_into_one_rebuild(db_factory, monkeypatch):
    from app.models.database import Media as M

    async with db_factory() as s:
        s.add(M(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="X", type="movie", year=2020, page_offset=0,
            is_in_allowed_categories=True,
        ))
        await s.commit()

    calls = []
    orig = unified_group_service._rebuild_one

    async def _counted(media_type, session_factory):
        calls.append(media_type)
        return await orig(media_type, session_factory)

    monkeypatch.setattr(unified_group_service, "_rebuild_one", _counted)
    monkeypatch.setattr(unified_group_service.settings, "SCRAPE_REBUILD_DEBOUNCE_SECONDS", 0.05)

    unified_group_service.schedule_rebuild("movie", session_factory=db_factory)
    unified_group_service.schedule_rebuild("movie", session_factory=db_factory)
    unified_group_service.schedule_rebuild("movie", session_factory=db_factory)
    await unified_group_service.flush_scheduled()

    assert calls == ["movie"]  # 3 calls in a burst -> exactly 1 rebuild


async def test_schedule_rebuild_ignores_unsupported_type(db_factory):
    unified_group_service.schedule_rebuild("episode", session_factory=db_factory)
    assert "episode" not in unified_group_service._pending_rebuild_tasks


async def test_flush_scheduled_awaits_an_already_running_rebuild(db_factory, monkeypatch):
    from app.models.database import Media as M

    async with db_factory() as s:
        s.add(M(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="X", type="movie", year=2020, page_offset=0,
            is_in_allowed_categories=True,
        ))
        await s.commit()

    monkeypatch.setattr(unified_group_service.settings, "SCRAPE_REBUILD_DEBOUNCE_SECONDS", 0.0)
    unified_group_service.schedule_rebuild("movie", session_factory=db_factory)
    # Give the debounce task a moment to start executing (past its sleep).
    import asyncio
    await asyncio.sleep(0.05)
    await unified_group_service.flush_scheduled()

    from app.models.database import MediaGroup

    async with db_factory() as s:
        groups = (await s.execute(select(MediaGroup))).scalars().all()
    assert len(groups) == 1


# ─── REAL WAL lock test (modeled on tests/test_worker_write_retry_real_lock.py) ──


async def _init_wal_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal", f"WAL mode did not stick: {mode}"
    finally:
        conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _make_short_timeout_engine(db_path: str, busy_timeout_ms: int = 50):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_conn, _record):
        dbapi_conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

    return engine


def _hold_write_lock(db_path: str, lock_acquired: threading.Event, hold_seconds: float) -> None:
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO media (rating_key, server_id, \"filter\", sort_order, "
            " library_section_id, title, title_sortable, page_offset, type, "
            " media_parts, unification_id, history_group_key, added_at, updated_at, "
            " display_rating, view_offset, view_count, last_viewed_at, "
            " stream_error_count, is_broken, is_in_allowed_categories, is_adult, "
            " match_locked) "
            "VALUES ('blocker', 'blocker_srv', 'all', 'default', '1', 'Blocker', "
            " '', 0, 'movie', '[]', '', '', 0, 0, 0.0, 0, 0, 0, 0, 0, 1, 0, 0)"
        )
        lock_acquired.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


def _start_blocker(db_path: str, hold_seconds: float) -> threading.Thread:
    lock_acquired = threading.Event()
    blocker = threading.Thread(
        target=_hold_write_lock, args=(db_path, lock_acquired, hold_seconds), daemon=True,
    )
    blocker.start()
    assert lock_acquired.wait(timeout=5), "blocker thread never acquired the write lock"
    return blocker


async def test_apply_candidate_write_survives_real_wal_lock(tmp_path, monkeypatch):
    db_path = str(tmp_path / "manual_scrape_apply_lock.db")
    await _init_wal_schema(db_path)

    engine = _make_short_timeout_engine(db_path)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as s:
        s.add(Media(
            rating_key="vod_lock.mp4", server_id="xtream_wrlock", library_section_id="1",
            title="Wr Lock Movie", type="movie", year=1999, page_offset=0,
        ))
        await s.commit()

    tmdb = FakeTMDB(details_by_id={603: _details()})
    monkeypatch.setattr(mss, "tmdb_service", tmdb)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    blocker = _start_blocker(db_path, hold_seconds=0.35)

    outcome = await mss.apply_candidate(
        server_id="xtream_wrlock", rating_key="vod_lock.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=factory, schedule_rebuild=False,
    )

    blocker.join(timeout=5)
    assert not blocker.is_alive()

    assert outcome.status == "applied", (
        "apply_candidate must survive a real WAL writer lock via write_with_retry"
    )
    # Phase 2 (network) happens exactly once, BEFORE the retried write phase —
    # a retry must never re-hit TMDB/OMDb (CLAUDE.md piège 8).
    assert tmdb.calls == [("get_movie_details", 603)]

    async with factory() as s:
        row = (await s.execute(
            select(Media).where(
                Media.rating_key == "vod_lock.mp4", Media.server_id == "xtream_wrlock",
            )
        )).scalars().one()
    assert row.tmdb_id == "603", (
        "the write must have actually landed despite the real lock -- proves "
        "write_with_retry actually retried, not a silently-swallowed failure"
    )
    assert row.match_locked is True
