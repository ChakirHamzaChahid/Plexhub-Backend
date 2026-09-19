"""ADR 0005 D7 — manual-scrape lock (`Media.match_locked`/`match_source`,
migration 026, Wave W1).

Covers every write-path guard the ADR lists (L1-L10): the enrichment worker's
two selection queries + its UPDATE guard, the sync upsert's CASE preservation,
the NFO importer's forced fill-missing on a locked row, the two
id-consistency scripts skipping locked rows, `enqueue_rescrape`'s "locked"
outcome (+ the JSON 409 and the admin locked-state fragment), and
`update_external_ids`'s unification recompute + auto-lock (including the
W1-deviation note: no `schedule_rebuild` call yet, that helper is W2).

Migration 026 itself is covered in `tests/test_migration_026.py`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest_asyncio
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.database import Base, EnrichmentQueue, Media
from app.scripts import dedup_resolved_twins
from app.scripts import validate_id_consistency as vic
from app.services import nfo_import_service
from app.services.media_service import media_service
from app.services.omdb_service import OMDbData
from app.services.tmdb_service import TMDBEnrichmentData
from app.workers import sync_worker as sync_worker_module
from app.workers.enrichment_worker import (
    FetchResult,
    MAX_ATTEMPTS,
    _apply_enrichment_results,
    _media_is_unlocked,
)


# ─── shared small helpers ───────────────────────────────────────────────────


def _locked_movie(rating_key="vod_locked.mp4", server_id="xtream_a", page_offset=1, **extra) -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id, library_section_id="1",
        title="Locked Movie", type="movie", page_offset=page_offset,
        match_locked=True, match_source="manual",
        imdb_id="tt1111111", tmdb_id="111",
        **extra,
    )


# ─── L1/L2: enrichment selection queries exclude locked rows ───────────────


class TestEnrichmentSelectionExcludesLocked:
    async def _phase_query(self, s, media_type):
        from sqlalchemy import or_

        result = await s.execute(
            select(EnrichmentQueue)
            .where(
                or_(
                    EnrichmentQueue.status == "pending",
                    (EnrichmentQueue.status == "skipped") & (EnrichmentQueue.attempts < MAX_ATTEMPTS),
                ),
                EnrichmentQueue.media_type == media_type,
                _media_is_unlocked(),
            )
        )
        return list(result.scalars().all())

    async def test_locked_movie_excluded_from_phase1(self, db_session):
        db_session.add_all([
            Media(
                rating_key="vod_free.mp4", server_id="xtream_a", library_section_id="1",
                title="Free Movie", type="movie", page_offset=0,
            ),
            _locked_movie(rating_key="vod_locked.mp4"),
            EnrichmentQueue(
                rating_key="vod_free.mp4", server_id="xtream_a", media_type="movie",
                title="Free Movie", status="pending", attempts=0, created_at=0,
            ),
            EnrichmentQueue(
                rating_key="vod_locked.mp4", server_id="xtream_a", media_type="movie",
                title="Locked Movie", status="pending", attempts=0, created_at=0,
            ),
        ])
        await db_session.commit()

        rows = await self._phase_query(db_session, "movie")
        assert {r.rating_key for r in rows} == {"vod_free.mp4"}

    async def test_locked_show_excluded_from_phase2(self, db_session):
        db_session.add_all([
            Media(
                rating_key="series_free", server_id="xtream_a", library_section_id="1",
                title="Free Show", type="show", page_offset=0,
            ),
            Media(
                rating_key="series_locked", server_id="xtream_a", library_section_id="1",
                title="Locked Show", type="show", page_offset=1,
                match_locked=True, match_source="manual",
            ),
            EnrichmentQueue(
                rating_key="series_free", server_id="xtream_a", media_type="show",
                title="Free Show", status="pending", attempts=0, created_at=0,
            ),
            EnrichmentQueue(
                rating_key="series_locked", server_id="xtream_a", media_type="show",
                title="Locked Show", status="pending", attempts=0, created_at=0,
            ),
        ])
        await db_session.commit()

        rows = await self._phase_query(db_session, "show")
        assert {r.rating_key for r in rows} == {"series_free"}


# ─── L3: UPDATE guard in _apply_enrichment_results ─────────────────────────


class TestApplyEnrichmentResultsRespectsLock:
    async def test_locked_row_is_untouched_even_with_a_fresh_match(self, db_session):
        """A race between the (now-passed) selection guard and the UPDATE:
        the row got locked after being selected. The UPDATE's own
        `match_locked == False` guard must make the write a no-op."""
        item = EnrichmentQueue(
            rating_key="vod_locked.mp4", server_id="xtream_a", media_type="movie",
            title="Locked Movie", year=1984, status="pending", attempts=0, created_at=0,
        )
        db_session.add(_locked_movie(rating_key="vod_locked.mp4"))
        await db_session.flush()

        data = TMDBEnrichmentData(
            tmdb_id=999, imdb_id="tt9999999", overview="A wrong match.",
            poster_url="http://img/p.jpg", backdrop_url="http://img/b.jpg",
            vote_average=5.0, genres="Drama", year=1984, cast="Nobody",
            tmdb_rating=5.0, tmdb_votes=10,
        )
        fr = FetchResult(item=item, data=data, confidence=1.0, result="matched",
                         api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = (await db_session.execute(
            select(Media.tmdb_id, Media.imdb_id).where(
                Media.rating_key == "vod_locked.mp4", Media.server_id == "xtream_a",
            )
        )).one()
        # Original locked identity survives; the fresh (wrong) match never lands.
        assert row.tmdb_id == "111"
        assert row.imdb_id == "tt1111111"
        # The EnrichmentQueue item itself is still marked processed (the
        # in-loop bookkeeping is independent of whether the Media UPDATE
        # actually wrote anything).
        assert item.status == "done"


# ─── L4: sync upsert preserves locked rich metadata across a content_hash flip ──


class TestSyncUpsertPreservesLockedColumns:
    async def test_locked_row_keeps_scraper_metadata_on_provider_change(self, db_session):
        sid = "xtream_acc1"
        locked = Media(
            rating_key="vod_locked.mp4", server_id=sid, library_section_id="xtream_vod",
            filter="all", sort_order="default", page_offset=0,
            title="Locked Movie", type="movie", year=1999,
            resolved_thumb_url="http://scraper/thumb.jpg",
            resolved_art_url="http://scraper/art.jpg",
            summary="Correct scraped summary", genres="Action",
            content_hash="old", dto_hash="old",
            match_locked=True, match_source="manual",
        )
        db_session.add(locked)
        await db_session.commit()

        incoming = {
            "rating_key": "vod_locked.mp4", "server_id": sid, "library_section_id": "xtream_vod",
            "filter": "all", "sort_order": "default", "page_offset": 0,
            "title": "Provider Renamed It", "title_sortable": "provider renamed it",
            "type": "movie", "year": 2020,
            "resolved_thumb_url": "http://provider/wrong-thumb.jpg",
            "resolved_art_url": "http://provider/wrong-art.jpg",
            "summary": "Wrong provider summary", "genres": "Wrong Genre",
            "unification_id": "title_provider_2020", "history_group_key": "hg_provider",
            "media_parts": "[]", "added_at": 1000, "updated_at": 1000,
        }

        await sync_worker_module.upsert_media_batch(db_session, [incoming])
        await db_session.commit()
        # The raw UPDATE inside upsert_media_batch bypasses the ORM unit of
        # work, so the already-identity-mapped `locked` instance keeps its
        # stale in-memory attributes (expire_on_commit=False) — expire it so
        # the assertions below read the real post-UPDATE row from the DB.
        db_session.expire_all()

        row = (await db_session.execute(
            select(Media).where(Media.rating_key == "vod_locked.mp4", Media.server_id == sid)
        )).scalars().one()

        # Locked columns survive the provider's content change...
        assert row.resolved_thumb_url == "http://scraper/thumb.jpg"
        assert row.resolved_art_url == "http://scraper/art.jpg"
        assert row.summary == "Correct scraped summary"
        assert row.genres == "Action"
        assert row.year == 1999
        # ...while an unprotected column (title) still follows the provider,
        # proving the UPDATE genuinely fired (content_hash did change).
        assert row.title == "Provider Renamed It"

    async def test_locked_row_with_cleared_tmdb_id_does_not_get_reseeded(self, db_session):
        """BLOCKING review fix: an operator who cleared a wrong provider
        tmdb_id (locked row, tmdb_id=NULL, title-based unification_id) must
        not see the provider's id silently come back on the next
        content_hash change. The plain COALESCE(Media.tmdb_id, provider)
        used for UNLOCKED rows would reseed it since Media.tmdb_id is NULL
        here; a locked row must keep its current (cleared) value outright."""
        sid = "xtream_acc1"
        locked = Media(
            rating_key="vod_cleared.mp4", server_id=sid, library_section_id="xtream_vod",
            filter="all", sort_order="default", page_offset=0,
            title="Cleared Wrong Match", type="movie", year=1999,
            tmdb_id=None,
            unification_id="title_clearedwrongmatch_1999",
            history_group_key="title_clearedwrongmatch_1999",
            content_hash="old", dto_hash="old",
            match_locked=True, match_source="manual",
        )
        db_session.add(locked)
        await db_session.commit()

        incoming = {
            "rating_key": "vod_cleared.mp4", "server_id": sid, "library_section_id": "xtream_vod",
            "filter": "all", "sort_order": "default", "page_offset": 0,
            "title": "Provider Renamed It Again", "title_sortable": "provider renamed it again",
            "type": "movie", "year": 2020,
            "tmdb_id": "77777",  # the provider's (wrong) id, would reseed an unlocked row
            "unification_id": "title_providerrenameditagain_2020",
            "history_group_key": "hg_provider2",
            "media_parts": "[]", "added_at": 1000, "updated_at": 1000,
        }

        await sync_worker_module.upsert_media_batch(db_session, [incoming])
        await db_session.commit()
        db_session.expire_all()

        row = (await db_session.execute(
            select(Media).where(Media.rating_key == "vod_cleared.mp4", Media.server_id == sid)
        )).scalars().one()

        assert row.tmdb_id is None  # stays cleared, not reseeded
        assert row.unification_id == "title_clearedwrongmatch_1999"  # stays title-based
        assert row.history_group_key == "title_clearedwrongmatch_1999"
        # Unprotected column still follows the provider (UPDATE genuinely fired).
        assert row.title == "Provider Renamed It Again"

    async def test_unlocked_row_still_takes_provider_tmdb_id_and_unification(self, db_session):
        """Non-regression (L4): an UNLOCKED row with no tmdb_id yet still
        gets the provider's id (and a title-based unification_id still
        follows a rename) — the blocking fix must not change this existing
        behaviour."""
        sid = "xtream_acc1"
        unlocked = Media(
            rating_key="vod_free.mp4", server_id=sid, library_section_id="xtream_vod",
            filter="all", sort_order="default", page_offset=0,
            title="Unlocked Movie", type="movie", year=1999,
            tmdb_id=None,
            unification_id="title_unlockedmovie_1999",
            history_group_key="title_unlockedmovie_1999",
            content_hash="old", dto_hash="old",
        )
        db_session.add(unlocked)
        await db_session.commit()

        incoming = {
            "rating_key": "vod_free.mp4", "server_id": sid, "library_section_id": "xtream_vod",
            "filter": "all", "sort_order": "default", "page_offset": 0,
            "title": "Unlocked Movie Renamed", "title_sortable": "unlocked movie renamed",
            "type": "movie", "year": 2020,
            "tmdb_id": "88888",
            "unification_id": "title_unlockedmovierenamed_2020",
            "history_group_key": "hg_unlocked_renamed",
            "media_parts": "[]", "added_at": 1000, "updated_at": 1000,
        }

        await sync_worker_module.upsert_media_batch(db_session, [incoming])
        await db_session.commit()
        db_session.expire_all()

        row = (await db_session.execute(
            select(Media).where(Media.rating_key == "vod_free.mp4", Media.server_id == sid)
        )).scalars().one()

        assert row.tmdb_id == "88888"
        assert row.unification_id == "title_unlockedmovierenamed_2020"
        assert row.history_group_key == "hg_unlocked_renamed"


# ─── L5: NFO import forces fill-missing on ids/images for a locked row ─────


class TestNfoImportRespectsLock:
    def _nfo(self, **ids):
        return nfo_import_service.NfoEntry(
            path=Path("x"), folder_name="x", media_type="movie", **ids
        )

    def test_locked_row_ignores_overwrite_for_ids_and_images(self):
        row = Media(
            rating_key="vod_1", server_id="xtream_a", library_section_id="1",
            title="Locked", type="movie", year=2020,
            imdb_id="tt0000001", tmdb_id="1",
            resolved_thumb_url="http://old/thumb.jpg",
            resolved_art_url="http://old/art.jpg",
            match_locked=True,
        )
        parsed = self._nfo(
            imdb_id="tt9999999", tmdb_id="99999",
            poster_url="http://new/thumb.jpg", fanart_url="http://new/art.jpg",
            summary="A brand-new summary from the NFO",
        )
        updates = nfo_import_service._compute_updates(row, parsed, overwrite=True)

        # Locked fill-only columns: already set -> NOT overwritten even though
        # overwrite=True.
        assert "imdb_id" not in updates
        assert "tmdb_id" not in updates
        assert "resolved_thumb_url" not in updates
        assert "resolved_art_url" not in updates
        # A column outside the locked fill-only set still honors overwrite=True.
        assert updates["summary"] == "A brand-new summary from the NFO"

    def test_locked_row_still_fills_a_genuinely_missing_id(self):
        """The lock forces fill-MISSING (not "never touch") for ids/images —
        an empty slot on a locked row is still fillable."""
        row = Media(
            rating_key="vod_2", server_id="xtream_a", library_section_id="1",
            title="Locked No Tmdb", type="movie", year=2020,
            imdb_id="tt0000002", tmdb_id=None,
            match_locked=True,
        )
        parsed = self._nfo(tmdb_id="424242")
        updates = nfo_import_service._compute_updates(row, parsed, overwrite=True)
        assert updates["tmdb_id"] == "424242"

    def test_unlocked_row_still_overwrites_ids_normally(self):
        """Control: an UNLOCKED row is unaffected by the new guard — a
        pre-existing id is still replaced under overwrite=True."""
        row = Media(
            rating_key="vod_3", server_id="xtream_a", library_section_id="1",
            title="Unlocked", type="movie", year=2020,
            imdb_id="tt0000003", match_locked=False,
        )
        parsed = self._nfo(imdb_id="tt7777777")
        updates = nfo_import_service._compute_updates(row, parsed, overwrite=True)
        assert updates["imdb_id"] == "tt7777777"


# ─── L6: validate_id_consistency --apply skips locked members ─────────────


_PAGE = [0]


def _seed_vic(db, *, rk, tmdb, imdb, uid, title, sid="a", dur_min=None, year=2000,
              locked=False):
    _PAGE[0] += 1
    m = Media(
        rating_key=rk, server_id=sid, library_section_id="1", title=title,
        type="movie", page_offset=_PAGE[0],
        tmdb_id=str(tmdb) if tmdb is not None else None, imdb_id=imdb,
        unification_id=uid, history_group_key=uid, year=year,
        duration=(dur_min * 60000 if dur_min is not None else None),
        is_in_allowed_categories=True,
        match_locked=locked, match_source=("manual" if locked else None),
    )
    db.add(m)
    return m


class _FakeTMDBForLock:
    is_configured = True

    def __init__(self, imdb_by_tmdb):
        self.imdb_by_tmdb = imdb_by_tmdb

    async def _details(self, tmdb_id):
        tid = int(tmdb_id)
        return TMDBEnrichmentData(
            tmdb_id=tid, imdb_id=self.imdb_by_tmdb.get(tid), overview=None,
            poster_url=None, backdrop_url=None, vote_average=None, genres=None,
            year=None, cast=None,
        )

    async def get_movie_details(self, tmdb_id):
        return await self._details(tmdb_id)

    async def get_tv_details(self, tmdb_id):
        return await self._details(tmdb_id)


class _FakeOMDbForLock:
    is_configured = True

    def __init__(self, data_by_imdb):
        self.data_by_imdb = data_by_imdb

    def get_request_count(self):
        return 0

    async def get_by_imdb_id(self, imdb_id):
        return self.data_by_imdb.get(imdb_id)


class TestValidateIdConsistencySkipsLocked:
    async def test_locked_member_skipped_not_reclassified_or_written(self, db_session):
        # Suspect group: member 1 genuine, member 2 mislabeled BUT locked.
        _seed_vic(db_session, rk="1", tmdb=100, imdb="tt1000", uid="imdb://tt1000",
                  title="Right Movie", dur_min=90)
        _seed_vic(db_session, rk="2", tmdb=200, imdb="tt1000", uid="imdb://tt1000",
                  title="Right Movie", dur_min=90, locked=True)
        await db_session.commit()

        tmdb = _FakeTMDBForLock(imdb_by_tmdb={100: "tt1000"})
        omdb = _FakeOMDbForLock(data_by_imdb={
            "tt1000": OMDbData(
                title="Right Movie", year="", runtime_minutes=90, genre=None,
                director=None, actors=None, plot=None, imdb_rating=None,
                imdb_votes=None, type="movie",
            ),
        })

        report = await vic.run(db_session, media_type="movie", tmdb=tmdb, omdb=omdb, apply=True)

        assert report.skipped_locked == 1
        # Not counted among examined/classified members.
        assert not any(v.rating_key == "2" for v in report.verdicts)
        # Untouched in the DB.
        row = (await db_session.execute(
            select(Media.tmdb_id, Media.match_locked).where(
                Media.rating_key == "2", Media.server_id == "a",
            )
        )).one()
        assert row.tmdb_id == "200"
        assert row.match_locked is True


# ─── L7: dedup_resolved_twins --apply skips locked rows ────────────────────


class TestDedupResolvedTwinsSkipsLocked:
    def _build_db(self, tmp_path) -> Path:
        db_path = tmp_path / "dedup.db"
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            # A resolved "source" row.
            s.add(Media(
                rating_key="src", server_id="a", library_section_id="1",
                title="Some Movie", type="movie", page_offset=0, year=2020,
                imdb_id="tt5555555", tmdb_id="5555",
                unification_id="imdb://tt5555555", history_group_key="imdb://tt5555555",
            ))
            # An unresolved LOCKED twin (same normalized title+year) — must be
            # left alone even though it would otherwise match.
            s.add(Media(
                rating_key="twin_locked", server_id="a", library_section_id="1",
                title="Some Movie", type="movie", page_offset=1, year=2020,
                unification_id="title_somemovie_2020", history_group_key="title_somemovie_2020",
                match_locked=True, match_source="manual",
            ))
            # An unresolved UNLOCKED twin — control, must still get linked.
            s.add(Media(
                rating_key="twin_free", server_id="a", library_section_id="1",
                title="Some Movie", type="movie", page_offset=2, year=2020,
                unification_id="title_somemovie_2020b", history_group_key="title_somemovie_2020b",
            ))
            s.commit()
        engine.dispose()
        return db_path

    def test_locked_twin_untouched_unlocked_twin_linked(self, tmp_path):
        db_path = self._build_db(tmp_path)

        rc = dedup_resolved_twins.main(["--db", str(db_path), "--apply", "--types", "movie"])
        assert rc == 0

        con = sqlite3.connect(str(db_path))
        try:
            locked_imdb = con.execute(
                "SELECT imdb_id, match_locked FROM media WHERE rating_key='twin_locked'"
            ).fetchone()
            free_imdb = con.execute(
                "SELECT imdb_id FROM media WHERE rating_key='twin_free'"
            ).fetchone()
        finally:
            con.close()

        assert locked_imdb == (None, 1)  # untouched
        assert free_imdb == ("tt5555555",)  # linked to the resolved twin


# ─── L8/L9: enqueue_rescrape / rescrape endpoints respect the lock ─────────


class TestEnqueueRescrapeRespectsLock:
    async def test_locked_media_returns_locked_and_writes_nothing(self, db_session):
        db_session.add(_locked_movie(rating_key="vod_locked.mp4"))
        await db_session.commit()

        outcome = await media_service.enqueue_rescrape(db_session, "vod_locked.mp4", "xtream_a")
        assert outcome == "locked"

        n = (await db_session.execute(
            select(EnrichmentQueue).where(EnrichmentQueue.rating_key == "vod_locked.mp4")
        )).scalars().first()
        assert n is None

    async def test_unlocked_media_still_queues(self, db_session):
        db_session.add(Media(
            rating_key="vod_free.mp4", server_id="xtream_a", library_section_id="1",
            title="Free", type="movie", page_offset=0,
        ))
        await db_session.commit()

        outcome = await media_service.enqueue_rescrape(db_session, "vod_free.mp4", "xtream_a")
        assert outcome == "queued"

    async def test_unknown_media_returns_not_found(self, db_session):
        outcome = await media_service.enqueue_rescrape(db_session, "nope", "xtream_a")
        assert outcome == "not_found"


API_KEY = "test-master-key-manual-scrape-lock"
API_HEADERS = {"X-API-Key": API_KEY}


ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-pass-manual-scrape-lock"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)


@pytest_asyncio.fixture(autouse=True)
def _wire_test_db(monkeypatch, db_factory):
    from app.config import settings
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    # /admin is Basic-Auth gated (503 when ADMIN_PASSWORD is empty).
    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)
    return db_factory


class TestRescrapeHttpRespectsLock:
    async def test_json_post_rescrape_locked_returns_409(self, api_client, db_factory):
        async with db_factory() as s:
            s.add(_locked_movie(rating_key="vod_locked.mp4"))
            await s.commit()

        resp = await api_client.post(
            "/api/media/vod_locked.mp4/rescrape",
            params={"server_id": "xtream_a"},
            headers=API_HEADERS,
        )
        assert resp.status_code == 409
        assert "locked" in resp.json()["detail"].lower()

    async def test_admin_rescrape_locked_renders_locked_state(self, api_client, db_factory):
        async with db_factory() as s:
            s.add(_locked_movie(rating_key="vod_locked.mp4"))
            await s.commit()

        resp = await api_client.post(
            "/admin/movies/vod_locked.mp4/rescrape",
            data={"server_id": "xtream_a"},
            auth=ADMIN_AUTH,
        )
        assert resp.status_code == 200, resp.text
        assert "verrouill" in resp.text.lower()


# ─── L10: update_external_ids recomputes unification + auto-locks ─────────


class TestUpdateExternalIdsLocksAndRecomputesUnification:
    async def test_setting_imdb_id_recomputes_unification_and_locks(self, db_session):
        db_session.add(Media(
            rating_key="vod_1.mp4", server_id="xtream_a", library_section_id="1",
            title="Some Movie", type="movie", year=2020,
            unification_id="title_somemovie_2020", history_group_key="title_somemovie_2020",
        ))
        await db_session.commit()

        updated = await media_service.update_external_ids(
            db_session, "vod_1.mp4", "xtream_a", fields={"imdb_id": "tt1234567"},
        )
        await db_session.commit()

        assert updated.imdb_id == "tt1234567"
        assert updated.unification_id == "imdb://tt1234567"
        assert updated.history_group_key == "imdb://tt1234567"
        assert updated.match_locked is True
        assert updated.match_source == "manual"

    async def test_empty_fields_is_a_true_noop_no_lock(self, db_session):
        db_session.add(Media(
            rating_key="vod_2.mp4", server_id="xtream_a", library_section_id="1",
            title="Untouched", type="movie", year=2020,
        ))
        await db_session.commit()

        updated = await media_service.update_external_ids(
            db_session, "vod_2.mp4", "xtream_a", fields={},
        )
        assert updated.match_locked is False
        assert updated.match_source is None

    async def test_unknown_media_returns_none(self, db_session):
        result = await media_service.update_external_ids(
            db_session, "nope", "xtream_a", fields={"imdb_id": "tt0000000"},
        )
        assert result is None

    async def test_setting_tmdb_only_without_imdb_uses_tmdb_based_unification(self, db_session):
        """No imdb_id in play at all: patching tmdb_id alone yields a
        tmdb-based unification key."""
        db_session.add(Media(
            rating_key="vod_3.mp4", server_id="xtream_a", library_section_id="1",
            title="Another Movie", type="movie", year=2021,
            unification_id="title_anothermovie_2021",
        ))
        await db_session.commit()

        updated = await media_service.update_external_ids(
            db_session, "vod_3.mp4", "xtream_a", fields={"tmdb_id": "42"},
        )
        assert updated.unification_id == "tmdb://42"
        assert updated.match_locked is True

    async def test_patching_tmdb_when_imdb_already_present_keeps_imdb_priority(self, db_session):
        """imdb>tmdb priority (`calculate_unification_id`): a row that ALREADY
        carries an imdb_id keeps an imdb-based unification key even when the
        patch only touches tmdb_id — the recompute must merge the patch with
        the row's EXISTING ids, not just the patched field(s)."""
        db_session.add(Media(
            rating_key="vod_4.mp4", server_id="xtream_a", library_section_id="1",
            title="Imdb Already Set", type="movie", year=2022,
            imdb_id="tt2222222", unification_id="imdb://tt2222222",
            history_group_key="imdb://tt2222222",
        ))
        await db_session.commit()

        updated = await media_service.update_external_ids(
            db_session, "vod_4.mp4", "xtream_a", fields={"tmdb_id": "99"},
        )
        assert updated.tmdb_id == "99"
        assert updated.imdb_id == "tt2222222"
        assert updated.unification_id == "imdb://tt2222222"
        assert updated.history_group_key == "imdb://tt2222222"
        assert updated.match_locked is True

    async def test_updates_every_filter_sort_order_variant(self, db_session):
        """The composite PK is (rating_key, server_id, filter, sort_order) —
        the same physical item can have several `media` rows differing only
        on filter/sort_order (one per category listing). The UPDATE must
        apply to every variant, not just the first one found."""
        db_session.add_all([
            Media(
                rating_key="vod_5.mp4", server_id="xtream_a", filter="all",
                sort_order="default", library_section_id="1",
                title="Multi Variant", type="movie", year=2023,
            ),
            Media(
                rating_key="vod_5.mp4", server_id="xtream_a", filter="7",
                sort_order="added", library_section_id="1",
                title="Multi Variant", type="movie", year=2023,
            ),
        ])
        await db_session.commit()

        await media_service.update_external_ids(
            db_session, "vod_5.mp4", "xtream_a", fields={"imdb_id": "tt3333333"},
        )
        await db_session.commit()
        db_session.expire_all()

        rows = (await db_session.execute(
            select(Media.filter, Media.imdb_id, Media.match_locked).where(
                Media.rating_key == "vod_5.mp4", Media.server_id == "xtream_a",
            )
        )).all()
        assert len(rows) == 2
        for _filter, imdb_id, locked in rows:
            assert imdb_id == "tt3333333"
            assert locked is True
