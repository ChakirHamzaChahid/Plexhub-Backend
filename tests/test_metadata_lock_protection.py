"""B2-08 — le verrou `match_locked` protège TOUTES les colonnes que l'API metadata écrit,
contre tous les écrivains automatiques (ADR 0012 § D2.1–D2.2, contrat B2 § 8.2).

Un test par écrivain, chacun avec une ligne verrouillée (rien ne bouge sur les colonnes de
`METADATA_WRITABLE_COLUMNS`) ET une ligne libre (le comportement historique est inchangé).
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.database import Media
from app.services.metadata_columns import METADATA_WRITABLE_COLUMNS

SID = "xtream_acc1"
_offsets = itertools.count(1)

# Original values of a row, one distinct value per protected column.
ORIGINAL = {
    "title": "Original Title", "title_sortable": "original title", "original_title": "Orig Orig",
    "summary": "Original summary", "tagline": "Original tagline", "genres": "Original Genre",
    "studio": "Original Studio", "country": "OR", "content_rating": "PG", "year": 1999,
    "premiered": "1999-01-01", "status": "Ended", "duration": 5_400_000,
    "imdb_id": "tt1111111", "tmdb_id": "111", "tvdb_id": "222", "wikidata_id": "Q1",
    "imdb_rating": 7.0, "imdb_votes": 70, "tmdb_rating": 6.0, "tmdb_votes": 60,
    "cast": "Original Actor", "cast_json": '[{"name": "Original Actor"}]',
    "youtube_trailer": "aaaaaaaaaaa", "unification_id": "imdb://tt1111111",
    "history_group_key": "imdb://tt1111111", "display_rating": 6.5,
    # lot B4
    "poster_url": "https://orig.example/p.jpg", "fanart_url": "https://orig.example/f.jpg",
    "poster_gallery_json": '["https://orig.example/p1.jpg"]',
    "fanart_gallery_json": '["https://orig.example/f1.jpg"]',
    "available_trailers_json": '[{"name": "O", "url": "https://orig.example/t", "isPrimary": true}]',
    "directors": "Original Director", "writers": "Original Writer", "spoken_languages": "en",
    "metacritic_rating": 7.1, "metacritic_votes": 71, "rt_rating": 8.1, "rt_votes": 81,
}
# What a provider / script would try to write instead.
INCOMING = {
    "title": "Provider Title", "title_sortable": "provider title", "original_title": "Prov Orig",
    "summary": "Provider summary", "tagline": "Provider tagline", "genres": "Provider Genre",
    "studio": "Provider Studio", "country": "PR", "content_rating": "R", "year": 2020,
    "premiered": "2020-02-02", "status": "Continuing", "duration": 7_200_000,
    "imdb_id": "tt9999999", "tmdb_id": "999", "tvdb_id": "888", "wikidata_id": "Q9",
    "imdb_rating": 1.5, "imdb_votes": 11, "tmdb_rating": 2.5, "tmdb_votes": 22,
    "cast": "Provider Actor", "cast_json": '[{"name": "Provider Actor"}]',
    "youtube_trailer": "bbbbbbbbbbb", "unification_id": "imdb://tt9999999",
    "history_group_key": "imdb://tt9999999", "display_rating": 3.3,
    # lot B4
    "poster_url": "https://prov.example/p.jpg", "fanart_url": "https://prov.example/f.jpg",
    "poster_gallery_json": '["https://prov.example/p1.jpg"]',
    "fanart_gallery_json": '["https://prov.example/f1.jpg"]',
    "available_trailers_json": '[{"name": "P", "url": "https://prov.example/t", "isPrimary": false}]',
    "directors": "Provider Director", "writers": "Provider Writer", "spoken_languages": "fr",
    "metacritic_rating": 1.1, "metacritic_votes": 11, "rt_rating": 2.1, "rt_votes": 21,
}


def row(rating_key: str, *, locked: bool, type: str = "movie", **extra) -> Media:
    values = dict(
        rating_key=rating_key, server_id=SID, filter="all", sort_order="default",
        library_section_id="xtream_vod" if type == "movie" else "xtream_series",
        page_offset=next(_offsets), type=type, thumb_url="http://old/thumb.jpg",
        content_hash="OLD", dto_hash="OLD", match_locked=locked,
        match_source="plexhubtmm" if locked else None, **ORIGINAL,
    )
    values.update(extra)
    return Media(**values)


async def reload(db, rating_key: str) -> SimpleNamespace:
    """A detached copy of the row as stored NOW (raw UPDATEs bypass the identity map, and a
    later `expire_all()` must not invalidate a copy the test still holds)."""
    db.expire_all()
    media = (await db.execute(
        select(Media).where(Media.rating_key == rating_key, Media.server_id == SID)
    )).scalars().one()
    return SimpleNamespace(**{c.name: getattr(media, c.name) for c in Media.__table__.columns})


def snapshot(media, columns=METADATA_WRITABLE_COLUMNS) -> dict:
    return {c: getattr(media, c) for c in sorted(columns)}


def test_every_original_and_incoming_value_covers_the_protected_set():
    """Guard of the test data itself: a new protected column must get a value here."""
    assert set(ORIGINAL) == set(INCOMING) == set(METADATA_WRITABLE_COLUMNS)
    assert all(ORIGINAL[c] != INCOMING[c] for c in ORIGINAL)


# ─── sync_worker.upsert_media_batch ─────────────────────────────────────────


def _sync_row(rating_key: str, **overrides) -> dict:
    base = {
        "rating_key": rating_key, "server_id": SID, "library_section_id": "xtream_vod",
        "filter": "all", "sort_order": "default", "page_offset": 0, "type": "movie",
        "media_parts": "[]", "added_at": 1, "updated_at": 1, "content_hash": "NEW", "dto_hash": "NEW",
        "thumb_url": "http://provider/new-thumb.jpg", **INCOMING,
    }
    base.update(overrides)
    return base


class TestSyncUpsert:
    async def test_a_locked_row_keeps_every_protected_column(self, db_session):
        from app.workers import sync_worker

        db_session.add(row("vod_locked.mkv", locked=True))
        await db_session.commit()
        before = snapshot(await reload(db_session, "vod_locked.mkv"))

        await sync_worker.upsert_media_batch(db_session, [_sync_row("vod_locked.mkv")])
        await db_session.commit()

        after = await reload(db_session, "vod_locked.mkv")
        assert snapshot(after) == before
        # the UPDATE really fired (content_hash flipped) and unprotected columns follow the provider
        assert after.content_hash != "OLD" and after.thumb_url == "http://provider/new-thumb.jpg"
        assert after.match_locked is True and after.match_source == "plexhubtmm"

    async def test_a_free_row_still_follows_the_provider(self, db_session):
        from app.workers import sync_worker

        db_session.add(row("vod_free.mkv", locked=False))
        await db_session.commit()
        await sync_worker.upsert_media_batch(db_session, [_sync_row("vod_free.mkv")])
        await db_session.commit()
        after = await reload(db_session, "vod_free.mkv")
        for column in ("title", "title_sortable", "duration", "content_rating", "youtube_trailer",
                       "display_rating", "summary", "genres", "year"):
            assert getattr(after, column) == INCOMING[column], column

    async def test_a_cleared_column_stays_cleared_on_a_locked_row(self, db_session):
        """B2 can write `null`: the provider must not re-seed an emptied field."""
        from app.workers import sync_worker

        db_session.add(row("vod_cleared.mkv", locked=True, summary=None, content_rating=None,
                           youtube_trailer=None, duration=None))
        await db_session.commit()
        await sync_worker.upsert_media_batch(db_session, [_sync_row("vod_cleared.mkv")])
        await db_session.commit()
        after = await reload(db_session, "vod_cleared.mkv")
        assert (after.summary, after.content_rating, after.youtube_trailer, after.duration) == (None,) * 4

    async def test_series_trailer_update_skips_a_locked_show(self, db_session):
        from app.workers import sync_worker

        db_session.add_all([
            row("series_locked", locked=True, type="show"),
            row("series_free", locked=False, type="show"),
        ])
        await db_session.commit()
        await sync_worker.apply_series_trailers(
            db_session, SID, [("series_locked", "ccccccccccc"), ("series_free", "ddddddddddd")],
        )
        await db_session.commit()
        assert (await reload(db_session, "series_locked")).youtube_trailer == "aaaaaaaaaaa"
        assert (await reload(db_session, "series_free")).youtube_trailer == "ddddddddddd"


# ─── strip_titles_pollution ─────────────────────────────────────────────────


class TestStripTitlesPollution:
    async def test_locked_titles_are_not_renamed_free_ones_are(self, db_session):
        from app.scripts import strip_titles_pollution as mig

        db_session.add_all([
            row("vod_locked.mkv", locked=True, title="Heat (1995)", title_sortable="heat 1995", year=None),
            row("vod_free.mkv", locked=False, title="Heat (1995)", title_sortable="heat 1995", year=None),
            row("ep_locked.mkv", locked=True, type="episode", title="Pilot (2020)",
                parent_title="Season 1", grandparent_title="Show (2020)"),
            row("ep_free.mkv", locked=False, type="episode", title="Pilot (2020)",
                parent_title="Season 1", grandparent_title="Show (2020)"),
        ])
        await db_session.commit()
        report = mig.MigrationReport()
        await mig._update_db_for_account(db_session, "acc1", False, report)
        await db_session.commit()

        locked = await reload(db_session, "vod_locked.mkv")
        free = await reload(db_session, "vod_free.mkv")
        assert (locked.title, locked.title_sortable, locked.year) == ("Heat (1995)", "heat 1995", None)
        assert free.title == "Heat" and free.year == 1995
        ep_locked = await reload(db_session, "ep_locked.mkv")
        ep_free = await reload(db_session, "ep_free.mkv")
        assert (ep_locked.title, ep_locked.title_sortable) == ("Pilot (2020)", "original title")
        assert ep_free.title == "Pilot"


# ─── backfill_certifications ────────────────────────────────────────────────


class TestBackfillCertifications:
    async def test_locked_rows_are_not_candidates_nor_written(self, db_factory, monkeypatch):
        from types import SimpleNamespace

        from app.scripts import backfill_certifications as bc

        async def fake_cert(*_a, **_k):
            return "TV-MA"

        monkeypatch.setattr(bc, "async_session_factory", db_factory)
        monkeypatch.setattr(bc, "tmdb_service", SimpleNamespace(is_configured=True))
        monkeypatch.setattr(bc, "_fetch_cert", fake_cert)
        monkeypatch.setattr(bc, "_INTER_BATCH_SLEEP", 0)
        async with db_factory() as db:
            db.add_all([
                row("vod_locked.mkv", locked=True, content_rating=None),
                row("vod_free.mkv", locked=False, content_rating=None),
            ])
            await db.commit()
        stats = await bc.run()
        assert stats.candidates == 1 and stats.updated == 1
        async with db_factory() as db:
            assert (await reload(db, "vod_locked.mkv")).content_rating is None
            assert (await reload(db, "vod_free.mkv")).content_rating == "TV-MA"


# ─── trailer_service write-back ─────────────────────────────────────────────


class TestTrailerWriteBack:
    async def test_a_locked_row_never_gets_a_trailer_written_back(self, db_factory, monkeypatch):
        from app.db import database as db_module
        from app.services import trailer_service

        monkeypatch.setattr(db_module, "async_session_factory", db_factory)
        async with db_factory() as db:
            db.add_all([
                row("vod_locked.mkv", locked=True, youtube_trailer=None),   # B2 cleared it on purpose
                row("vod_free.mkv", locked=False, youtube_trailer=None),
            ])
            await db.commit()
        await trailer_service._write_back_youtube_trailer("vod_locked.mkv", SID, "eeeeeeeeeee")
        await trailer_service._write_back_youtube_trailer("vod_free.mkv", SID, "eeeeeeeeeee")
        async with db_factory() as db:
            assert (await reload(db, "vod_locked.mkv")).youtube_trailer is None
            assert (await reload(db, "vod_free.mkv")).youtube_trailer == "eeeeeeeeeee"


# ─── enrichment_backfill_worker (OMDb ratings) ──────────────────────────────


class TestOmdbRatingsBackfill:
    async def test_a_locked_row_keeps_its_ratings(self, db_factory, monkeypatch):
        from app.workers import enrichment_backfill_worker as backfill
        from tests.test_enrichment_backfill import _FakeOmdbSvc

        monkeypatch.setattr(backfill, "omdb_service", _FakeOmdbSvc(ratings={
            "tt1111111": (9.1, 900), "tt2222222": (9.1, 900),
        }))
        backfill._jobs.clear()
        async with db_factory() as db:
            db.add_all([
                row("vod_locked.mkv", locked=True, imdb_rating=None, imdb_votes=None, tmdb_rating=None,
                    display_rating=0.0, imdb_id="tt1111111", unification_id="imdb://tt1111111"),
                row("vod_free.mkv", locked=False, imdb_rating=None, imdb_votes=None, tmdb_rating=None,
                    display_rating=0.0, imdb_id="tt2222222", unification_id="imdb://tt2222222"),
            ])
            await db.commit()
        await backfill.run_backfill("job-lock", db_factory, media_type="all", limit=None)
        async with db_factory() as db:
            locked = await reload(db, "vod_locked.mkv")
            locked_values = (locked.imdb_rating, locked.imdb_votes, locked.display_rating)
            free = await reload(db, "vod_free.mkv")
            free_values = (free.imdb_rating, free.imdb_votes)
        assert locked_values == (None, None, 0.0)
        assert free_values == (9.1, 900)
        backfill._jobs.clear()


# ─── nfo_import_service ─────────────────────────────────────────────────────


def _nfo(**kw):
    from pathlib import Path

    from app.services.nfo_import_service import NfoEntry

    return NfoEntry(path=Path("movie.nfo"), media_type="movie", **kw)


class TestNfoImport:
    @pytest.mark.parametrize("overwrite", [False, True])
    def test_a_locked_row_takes_no_protected_column_from_a_nfo(self, overwrite):
        from app.services.nfo_import_service import _compute_updates

        locked = row("vod_locked.mkv", locked=True, summary=None, tagline=None, content_rating=None,
                     imdb_rating=None, imdb_votes=None, original_title=None, genres=None, cast=None)
        entry = _nfo(
            summary="NFO summary", tagline="NFO tagline", content_rating="R", imdb_rating=8.0,
            imdb_votes=5, original_title="NFO Orig", genres_csv="Action", cast_csv="NFO Actor",
            nfo_year=2001, imdb_id="tt5555555", tmdb_id="555", poster_url="http://nfo/p.jpg",
        )
        updates = _compute_updates(locked, entry, overwrite)
        assert not (set(updates) & METADATA_WRITABLE_COLUMNS), updates
        assert "unification_id" not in updates and "display_rating" not in updates

    def test_a_cleared_id_is_not_refilled_by_a_nfo_on_a_locked_row(self):
        from app.services.nfo_import_service import _compute_updates

        locked = row("vod_locked.mkv", locked=True, imdb_id=None, tmdb_id=None,
                     unification_id="title_original_title_1999")
        assert _compute_updates(locked, _nfo(imdb_id="tt5555555", tmdb_id="555"), False) == {}

    def test_a_free_row_is_unchanged_in_behaviour(self):
        from app.services.nfo_import_service import _compute_updates

        free = row("vod_free.mkv", locked=False, summary=None, imdb_rating=None, imdb_votes=None)
        updates = _compute_updates(free, _nfo(summary="NFO summary", imdb_rating=8.0, imdb_votes=5), False)
        assert updates["summary"] == "NFO summary" and updates["imdb_rating"] == 8.0
        assert "display_rating" in updates


# ─── cli recompute-unification ──────────────────────────────────────────────


class TestRecomputeUnification:
    async def test_locked_rows_keep_their_unification(self, db_factory, monkeypatch):
        from app import cli
        from app.db import database as db_module

        async def no_init():
            return None

        monkeypatch.setattr(db_module, "async_session_factory", db_factory)
        monkeypatch.setattr(db_module, "init_db", no_init)
        async with db_factory() as db:
            db.add_all([
                row("vod_locked.mkv", locked=True, unification_id="stale-locked"),
                row("vod_free.mkv", locked=False, unification_id="stale-free"),
            ])
            await db.commit()
        scanned, fixed = await cli._run_recompute_unification(False)
        async with db_factory() as db:
            assert (await reload(db, "vod_locked.mkv")).unification_id == "stale-locked"
            assert (await reload(db, "vod_free.mkv")).unification_id == "imdb://tt1111111"
        assert fixed == 1
