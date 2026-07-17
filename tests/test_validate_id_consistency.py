"""Tests for app/scripts/validate_id_consistency.py.

Style: seed real Media rows in ``db_session`` + inject fake service doubles
(the ``_Fake`` pattern from tests/test_enrichment_scraping.py — no
unittest.mock). The script's core ``run(db, *, tmdb=, omdb=, ...)`` is directly
exercised; argparse/main stay untested (thin).
"""
import pytest
from sqlalchemy import select

from app.models.database import Media
from app.scripts import validate_id_consistency as vic
from app.services.omdb_service import OMDbData
from app.services.tmdb_service import TMDBEnrichmentData


# ─── Fakes ─────────────────────────────────────────────────────────────────


def _enrich(tmdb_id: int, imdb_id: str | None, **extra) -> TMDBEnrichmentData:
    base = dict(
        tmdb_id=tmdb_id, imdb_id=imdb_id, overview=None, poster_url=None,
        backdrop_url=None, vote_average=None, genres=None, year=None, cast=None,
    )
    base.update(extra)
    return TMDBEnrichmentData(**base)


def _omdb(title, runtime_minutes, imdb_rating=None, imdb_votes=None) -> OMDbData:
    return OMDbData(
        title=title, year="", runtime_minutes=runtime_minutes, genre=None,
        director=None, actors=None, plot=None, imdb_rating=imdb_rating,
        imdb_votes=imdb_votes, type="movie",
    )


class _FakeTMDB:
    """imdb_by_tmdb maps tmdb_id -> real imdb_id; a missing id raises (dead)."""
    is_configured = True

    def __init__(self, imdb_by_tmdb=None, details_by_tmdb=None):
        self.imdb_by_tmdb = imdb_by_tmdb or {}
        self.details_by_tmdb = details_by_tmdb or {}
        self.calls: list[int] = []

    async def _details(self, tmdb_id):
        tid = int(tmdb_id)
        self.calls.append(tid)
        if tid in self.details_by_tmdb:
            return self.details_by_tmdb[tid]
        if tid in self.imdb_by_tmdb:
            return _enrich(tid, self.imdb_by_tmdb[tid])
        raise RuntimeError("dead tmdb_id")

    async def get_movie_details(self, tmdb_id):
        return await self._details(tmdb_id)

    async def get_tv_details(self, tmdb_id):
        return await self._details(tmdb_id)


class _BoomTMDB:
    is_configured = True

    async def get_movie_details(self, *a, **k):
        raise AssertionError("TMDB must NOT be called when there are no suspects")

    async def get_tv_details(self, *a, **k):
        raise AssertionError("TMDB must NOT be called when there are no suspects")


class _FakeOMDb:
    def __init__(self, data_by_imdb=None, configured=True, count=0):
        self.data_by_imdb = data_by_imdb or {}
        self._configured = configured
        self._count = count
        self.calls: list[str] = []

    @property
    def is_configured(self):
        return self._configured

    def get_request_count(self):
        return self._count

    async def get_by_imdb_id(self, imdb_id):
        self.calls.append(imdb_id)
        self._count += 1
        return self.data_by_imdb.get(imdb_id)


# ─── Seed helper ───────────────────────────────────────────────────────────


_PAGE = [0]


def _seed(db, *, rk, tmdb, imdb, uid, title, sid="a", dur_min=None, year=2000,
          mtype="movie", imdb_rating=None):
    _PAGE[0] += 1  # keep uix_media_pagination unique across seeded rows
    m = Media(
        rating_key=rk, server_id=sid, library_section_id="1", title=title,
        type=mtype, page_offset=_PAGE[0],
        tmdb_id=str(tmdb) if tmdb is not None else None, imdb_id=imdb,
        unification_id=uid, history_group_key=uid, year=year,
        duration=(dur_min * 60000 if dur_min is not None else None),
        imdb_rating=imdb_rating, is_in_allowed_categories=True,
    )
    db.add(m)
    return m


def _verdict(report, rk):
    return next(v for v in report.verdicts if v.rating_key == rk)


async def _col(db, rk, column, sid="a"):
    return (await db.execute(
        select(column).where(Media.rating_key == rk, Media.server_id == sid)
    )).scalar_one()


# ─── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_suspect_groups(db_session):
    # Two unrelated movies, each its own unification_id -> no divergence.
    _seed(db_session, rk="1", tmdb=100, imdb="tt1000", uid="imdb://tt1000", title="A")
    _seed(db_session, rk="2", tmdb=200, imdb="tt2000", uid="imdb://tt2000", title="B")
    await db_session.commit()

    report = await vic.run(
        db_session, media_type="movie", tmdb=_BoomTMDB(), omdb=_FakeOMDb(),
    )

    assert report.suspect_group_count == 0
    assert report.members_examined == 0
    assert report.tmdb_fetches == 0
    assert report.omdb_fetches == 0
    assert all(report.counts[c] == 0 for c in vic._ALL_CLASSES)


@pytest.mark.asyncio
async def test_same_content_mislabeled(db_session):
    # imdb://tt1000 group: member 1 is genuine, member 2 has a wrong tmdb but
    # is the SAME content (OMDb confirms title + runtime).
    _seed(db_session, rk="1", tmdb=100, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    _seed(db_session, rk="2", tmdb=200, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    await db_session.commit()

    tmdb = _FakeTMDB(imdb_by_tmdb={100: "tt1000", 200: "tt9999"})
    omdb = _FakeOMDb(data_by_imdb={"tt1000": _omdb("Right Movie", 90)})

    report = await vic.run(db_session, media_type="movie", tmdb=tmdb, omdb=omdb)

    assert report.suspect_group_count == 1
    assert _verdict(report, "1").classification == vic.CONSISTENT
    v2 = _verdict(report, "2")
    assert v2.classification == vic.SAME_CONTENT_MISLABELED
    # Reassignment target = the CONSISTENT member's ids.
    assert v2.new_tmdb_id == "100"
    assert v2.new_imdb_id == "tt1000"
    assert v2.new_unification_id == "imdb://tt1000"


@pytest.mark.asyncio
async def test_different_content_decoupled(db_session):
    # imdb://tt2000 group: member 2 is genuinely a different title merged by
    # mistake — its own tmdb resolves elsewhere AND OMDb title+duration diverge.
    _seed(db_session, rk="1", tmdb=300, imdb="tt2000", uid="imdb://tt2000",
          title="Alpha", dur_min=100)
    _seed(db_session, rk="2", tmdb=400, imdb="tt2000", uid="imdb://tt2000",
          title="Beta Different", dur_min=45)
    await db_session.commit()

    tmdb = _FakeTMDB(imdb_by_tmdb={300: "tt2000", 400: "tt4444"})
    omdb = _FakeOMDb(data_by_imdb={"tt2000": _omdb("Alpha", 100)})

    report = await vic.run(db_session, media_type="movie", tmdb=tmdb, omdb=omdb)

    v2 = _verdict(report, "2")
    assert v2.classification == vic.DIFFERENT_CONTENT
    # Decoupled to its OWN identity (own tmdb + that tmdb's real imdb).
    assert v2.new_tmdb_id == "400"
    assert v2.new_imdb_id == "tt4444"
    assert v2.new_unification_id == "imdb://tt4444"


@pytest.mark.asyncio
async def test_uncertain_weak_signal(db_session):
    # Title differs but duration is unknown (OMDb runtime None) -> undecided.
    _seed(db_session, rk="1", tmdb=500, imdb="tt3000", uid="imdb://tt3000",
          title="Gamma", dur_min=120)
    _seed(db_session, rk="2", tmdb=600, imdb="tt3000", uid="imdb://tt3000",
          title="Totally Other", dur_min=None)
    await db_session.commit()

    tmdb = _FakeTMDB(imdb_by_tmdb={500: "tt3000", 600: "tt6666"})
    omdb = _FakeOMDb(data_by_imdb={"tt3000": _omdb("Something", None)})

    report = await vic.run(db_session, media_type="movie", tmdb=tmdb, omdb=omdb)

    v2 = _verdict(report, "2")
    assert v2.classification == vic.UNCERTAIN
    assert v2.new_unification_id is None
    # Untouched.
    assert await _col(db_session, "2", Media.tmdb_id) == "600"
    assert await _col(db_session, "2", Media.imdb_id) == "tt3000"


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db_session):
    _seed(db_session, rk="1", tmdb=100, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    _seed(db_session, rk="2", tmdb=200, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    await db_session.commit()

    tmdb = _FakeTMDB(imdb_by_tmdb={100: "tt1000", 200: "tt9999"})
    omdb = _FakeOMDb(data_by_imdb={"tt1000": _omdb("Right Movie", 90)})

    report = await vic.run(
        db_session, media_type="movie", tmdb=tmdb, omdb=omdb, apply=False,
    )

    assert report.applied is False
    assert report.rebuilt_types == []
    # Row 2 must be untouched despite being classified MISLABELED.
    assert await _col(db_session, "2", Media.tmdb_id) == "200"
    assert await _col(db_session, "2", Media.unification_id) == "imdb://tt1000"


@pytest.mark.asyncio
async def test_apply_writes_and_rebuilds_and_coalesces_rating(db_session):
    # Row 2 carries an existing (NFO) imdb_rating that COALESCE must NOT clobber.
    _seed(db_session, rk="1", tmdb=100, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    _seed(db_session, rk="2", tmdb=200, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90, imdb_rating=7.7)
    await db_session.commit()

    tmdb = _FakeTMDB(imdb_by_tmdb={100: "tt1000", 200: "tt9999"})
    omdb = _FakeOMDb(data_by_imdb={"tt1000": _omdb("Right Movie", 90, imdb_rating=5.0)})

    report = await vic.run(
        db_session, media_type="movie", tmdb=tmdb, omdb=omdb, apply=True,
    )

    assert report.applied is True
    assert report.rebuilt_types == ["movie"]
    # Reassigned.
    assert await _col(db_session, "2", Media.tmdb_id) == "100"
    assert await _col(db_session, "2", Media.imdb_id) == "tt1000"
    assert await _col(db_session, "2", Media.unification_id) == "imdb://tt1000"
    # COALESCE: existing rating survives (not overwritten by OMDb's 5.0).
    assert await _col(db_session, "2", Media.imdb_rating) == 7.7


@pytest.mark.asyncio
async def test_apply_decouple_changes_unification_id(db_session):
    _seed(db_session, rk="1", tmdb=300, imdb="tt2000", uid="imdb://tt2000",
          title="Alpha", dur_min=100)
    _seed(db_session, rk="2", tmdb=400, imdb="tt2000", uid="imdb://tt2000",
          title="Beta Different", dur_min=45)
    await db_session.commit()

    tmdb = _FakeTMDB(imdb_by_tmdb={300: "tt2000", 400: "tt4444"})
    omdb = _FakeOMDb(data_by_imdb={"tt2000": _omdb("Alpha", 100)})

    report = await vic.run(
        db_session, media_type="movie", tmdb=tmdb, omdb=omdb, apply=True,
    )

    assert await _col(db_session, "2", Media.unification_id) == "imdb://tt4444"
    assert await _col(db_session, "2", Media.imdb_id) == "tt4444"
    # The changed-uid list flags the folder that must be cleared before regen.
    changed = {c["new"] for c in report.changed_unification_ids}
    assert "imdb://tt4444" in changed


@pytest.mark.asyncio
async def test_budget_guard_skips_omdb(db_session):
    # OMDb reports it has already spent its daily budget -> no OMDb calls, and
    # the unconfirmed member falls through to UNCERTAIN (no fallback signal).
    _seed(db_session, rk="1", tmdb=100, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    _seed(db_session, rk="2", tmdb=200, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    await db_session.commit()

    tmdb = _FakeTMDB(imdb_by_tmdb={100: "tt1000", 200: "tt9999"})
    omdb = _FakeOMDb(data_by_imdb={"tt1000": _omdb("Right Movie", 90)}, count=999)

    report = await vic.run(
        db_session, media_type="movie", tmdb=tmdb, omdb=omdb, omdb_daily_limit=20,
    )

    assert omdb.calls == []
    assert report.omdb_fetches == 0
    assert _verdict(report, "2").classification == vic.UNCERTAIN


@pytest.mark.asyncio
async def test_omdb_unconfigured_lands_uncertain(db_session):
    _seed(db_session, rk="1", tmdb=100, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    _seed(db_session, rk="2", tmdb=200, imdb="tt1000", uid="imdb://tt1000",
          title="Right Movie", dur_min=90)
    await db_session.commit()

    tmdb = _FakeTMDB(imdb_by_tmdb={100: "tt1000", 200: "tt9999"})
    omdb = _FakeOMDb(configured=False)

    report = await vic.run(db_session, media_type="movie", tmdb=tmdb, omdb=omdb)

    assert report.omdb_configured is False
    assert omdb.calls == []
    assert _verdict(report, "2").classification == vic.UNCERTAIN
