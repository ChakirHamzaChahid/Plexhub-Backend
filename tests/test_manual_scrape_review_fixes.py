"""ADR 0005 W2, code-review follow-ups (cycle 1).

One test per fix, each asserting the BUG would be caught, not just that the
happy path still works:

1. `propagate` must refuse a degenerate title key (non-Latin / "Unknown"
   titles all collapse to a shared `title__<year>`) — otherwise one film's
   identity, and its lock, lands on unrelated films.
2. A correction resolved through OMDb ALONE must apply replace-mode rules:
   the wrong film's poster/summary/tmdb_rating may not survive.
3. An operator-typed imdb id must survive a TMDB detail fetch that carries
   no `external_ids` (common on TV/obscure titles).
4. Rowcount 0 on a MANUAL apply means the row vanished -> `not_found`, not
   `skipped_locked` (a manual apply never filters on `match_locked`).
5. Two rebuilds of the same media_type must serialize, so the later one also
   commits later and wins with the fresher read.

Fakes/`_details` are reused from `test_manual_scrape_apply.py` rather than
re-declared, so a drift in the fake TMDB/OMDb shape can only ever break both
files at once.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.models.database import Media
from app.services import manual_scrape_service as mss
from app.services import unified_group_service
from app.services.omdb_service import OMDbData
from app.utils.unification import calculate_unification_id

from tests.test_manual_scrape_apply import FakeOMDb, FakeTMDB, _details


async def test_propagate_refuses_degenerate_title_key(
    db_session, db_factory, monkeypatch,
):
    degenerate_uid = calculate_unification_id("مسلسل", 1999)
    assert degenerate_uid == "title__1999", "precondition: the key is degenerate"

    db_session.add_all([
        Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="مسلسل", type="movie", year=1999, page_offset=0,
            unification_id=degenerate_uid,
        ),
        Media(
            rating_key="vod_unrelated.mp4", server_id="xtream_b", library_section_id="1",
            title="另一部电影", type="movie", year=1999, page_offset=1,
            unification_id=degenerate_uid,
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
    assert outcome.propagated == 0, "no fan-out over a degenerate title key"

    async with db_factory() as s:
        unrelated = (await s.execute(
            select(Media).where(Media.rating_key == "vod_unrelated.mp4")
        )).scalars().one()
    assert unrelated.tmdb_id is None
    assert unrelated.imdb_id is None
    assert unrelated.match_locked is False


async def test_omdb_only_correction_clears_wrong_film_metadata(
    db_session, db_factory, monkeypatch,
):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="Obscure Film", type="movie", year=2005, page_offset=0,
        thumb_url="http://xtream/poster.jpg", art_url="http://xtream/art.jpg",
        tmdb_id="999", imdb_id="tt0000001",
        summary="WRONG film summary", genres="Horror",
        resolved_thumb_url="http://img.tmdb.org/wrong.jpg",
        resolved_art_url="http://img.tmdb.org/wrong_bg.jpg",
        tmdb_rating=9.5, tmdb_votes=5000, scraped_rating=9.5,
        original_title="Wrong Original", tagline="Wrong tagline",
        display_rating=9.5,
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(imdb_to_tmdb={}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb(data_by_imdb={
        "tt9999999": OMDbData(
            title="Obscure", year="2005", runtime_minutes=90, genre="Drama",
            director=None, actors="Some Actor", plot="The RIGHT plot.",
            imdb_rating=6.4, imdb_votes=120, type="movie", imdb_id="tt9999999",
        ),
    }))

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=None, imdb_id="tt9999999", force=True,
        session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "applied"
    assert outcome.mode == "replace"

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4")
        )).scalars().one()

    assert row.imdb_id == "tt9999999"
    assert row.tmdb_id is None
    assert row.unification_id == "imdb://tt9999999"
    # The wrong film's provider-only leftovers are gone.
    assert row.tmdb_rating is None
    assert row.tmdb_votes is None
    assert row.scraped_rating is None
    assert row.original_title is None
    assert row.tagline is None
    # Images fall back to the raw Xtream ones, never the wrong film's poster.
    assert row.resolved_thumb_url == "http://xtream/poster.jpg"
    assert row.resolved_art_url == "http://xtream/art.jpg"
    # OMDb fills the Xtream-origin columns + ratings.
    assert row.summary == "The RIGHT plot."
    assert row.genres == "Drama"
    assert row.imdb_rating == 6.4
    assert row.display_rating == 6.4


async def test_operator_imdb_survives_when_tmdb_has_no_external_id(
    db_session, db_factory, monkeypatch,
):
    db_session.add(Media(
        rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
        title="No External Id", type="movie", year=2011, page_offset=0,
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(
        details_by_id={777: _details(tmdb_id=777, imdb_id=None)},
    ))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_1.mp4", media_type="movie",
        tmdb_id=777, imdb_id="tt5555555",
        session_factory=db_factory, schedule_rebuild=False,
    )
    assert outcome.status == "applied"
    assert outcome.new_imdb_id == "tt5555555"

    async with db_factory() as s:
        row = (await s.execute(
            select(Media).where(Media.rating_key == "vod_1.mp4")
        )).scalars().one()
    assert row.imdb_id == "tt5555555", "the operator's imdb id must not be dropped"
    assert row.tmdb_id == "777"
    assert row.unification_id == "imdb://tt5555555", (
        "imdb beats tmdb in calculate_unification_id — the key must agree with "
        "the id actually stored"
    )
    assert row.history_group_key == "imdb://tt5555555"


async def test_manual_apply_on_vanished_row_reports_not_found(
    db_session, db_factory, monkeypatch,
):
    db_session.add(Media(
        rating_key="vod_gone.mp4", server_id="xtream_a", library_section_id="1",
        title="Vanishing", type="movie", year=2000, page_offset=0,
    ))
    await db_session.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    real_fetch = mss._fetch_tmdb_details

    async def delete_row_then_fetch(tmdb_id, media_type):
        result = await real_fetch(tmdb_id, media_type)
        async with db_factory() as s:  # the row vanishes mid-apply
            row = (await s.execute(
                select(Media).where(Media.rating_key == "vod_gone.mp4")
            )).scalars().first()
            if row is not None:
                await s.delete(row)
                await s.commit()
        return result

    monkeypatch.setattr(mss, "_fetch_tmdb_details", delete_row_then_fetch)

    outcome = await mss.apply_candidate(
        server_id="xtream_a", rating_key="vod_gone.mp4", media_type="movie",
        tmdb_id=603, imdb_id=None, session_factory=db_factory,
        schedule_rebuild=False,
    )
    assert outcome.status == "not_found"


async def test_rebuild_scheduled_while_one_runs_does_not_overlap(
    db_factory, monkeypatch,
):
    async with db_factory() as s:
        s.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="X", type="movie", year=2020, page_offset=0,
            is_in_allowed_categories=True,
        ))
        await s.commit()

    concurrent = 0
    max_concurrent = 0
    orig = unified_group_service._rebuild_one

    async def slow_rebuild(media_type, session_factory):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        try:
            await asyncio.sleep(0.05)
            return await orig(media_type, session_factory)
        finally:
            concurrent -= 1

    monkeypatch.setattr(unified_group_service, "_rebuild_one", slow_rebuild)

    try:
        unified_group_service.schedule_rebuild(
            "movie", session_factory=db_factory, delay=0,
        )
        await asyncio.sleep(0.02)  # the first one is running, past its debounce
        unified_group_service.schedule_rebuild(
            "movie", session_factory=db_factory, delay=0,
        )
        await unified_group_service.flush_scheduled()

        assert max_concurrent == 1, "rebuilds of the same type must be serialized"
        assert not unified_group_service._pending_rebuild_tasks
        assert not unified_group_service._running_rebuild_tasks
    finally:
        unified_group_service._pending_rebuild_tasks.clear()
        unified_group_service._pending_session_factories.clear()
        unified_group_service._running_rebuild_tasks.clear()

