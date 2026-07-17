"""Tests for the enrichment anti-recurrence guard (Wave 3, S5 — id-consistency
validator design doc §5): a cheap intra-record shape tripwire plus an OMDb
tie-break that downgrades low-confidence TMDB matches to "ambiguous" when
OMDb clearly contradicts them (year gap > 1 AND low title similarity)."""
import pytest

from app.models.database import EnrichmentQueue, Media
from app.services.omdb_service import OMDbData
from app.workers.enrichment_worker import FetchResult, _apply_enrichment_results
from app.workers import enrichment_worker as ew
from app.config import settings
from sqlalchemy import select


def _data(tmdb_id=218, imdb="tt0088247", year=1984, title="Terminator"):
    """Mirrors `tests/test_enrichment_scraping.py::_data` — a matched
    TMDBEnrichmentData with a real imdb_id shape by default."""
    from app.services.tmdb_service import TMDBEnrichmentData
    return TMDBEnrichmentData(
        tmdb_id=tmdb_id, imdb_id=imdb, overview="A cyborg assassin.",
        poster_url="http://img/p.jpg", backdrop_url="http://img/b.jpg",
        vote_average=8.0, genres="Action, Sci-Fi", year=year, cast="Arnold",
    )


def _item(rating_key="vod_1.mp4", server_id="xtream_a", title="Terminator", year=1984):
    return EnrichmentQueue(
        rating_key=rating_key, server_id=server_id, media_type="movie",
        title=title, year=year, status="pending", attempts=0, created_at=0,
    )


def _media(item, title="Terminator"):
    return Media(
        rating_key=item.rating_key, server_id=item.server_id,
        library_section_id="1", title=title, type="movie",
    )


async def _tmdb_id_of(db_session, item) -> str | None:
    row = (await db_session.execute(
        select(Media.tmdb_id).where(
            Media.rating_key == item.rating_key, Media.server_id == item.server_id,
        )
    )).scalar_one()
    return row


class _FakeOmdb:
    """Double for `app.services.omdb_service.omdb_service`."""

    def __init__(self, data: OMDbData | None = None, configured: bool = True, request_count: int = 0, raises: bool = False):
        self._data = data
        self._configured = configured
        self._request_count = request_count
        self._raises = raises
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return self._configured

    def get_request_count(self) -> int:
        return self._request_count

    async def get_by_imdb_id(self, imdb_id: str) -> OMDbData | None:
        self.calls += 1
        self._request_count += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._data


def _omdb_data(title="Terminator", year="1984"):
    return OMDbData(
        title=title, year=year, runtime_minutes=107, genre="Action", director="J. Cameron",
        actors="Arnold Schwarzenegger", plot="A cyborg is sent back in time.",
        imdb_rating=8.1, imdb_votes=900000, type="movie",
    )


class TestShapeTripwire:
    @pytest.mark.asyncio
    async def test_malformed_imdb_id_numeric_only(self, db_session, monkeypatch):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        monkeypatch.setattr(ew, "omdb_service", _FakeOmdb())

        fr = FetchResult(item=item, data=_data(imdb="12345"), confidence=1.0,
                          result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "ambiguous"
        assert item.status == "skipped"
        assert await _tmdb_id_of(db_session, item) is None

    @pytest.mark.asyncio
    async def test_malformed_imdb_id_non_tt_prefix(self, db_session, monkeypatch):
        item = _item(rating_key="vod_2.mp4")
        db_session.add(_media(item))
        await db_session.flush()
        monkeypatch.setattr(ew, "omdb_service", _FakeOmdb())

        fr = FetchResult(item=item, data=_data(imdb="ttabc"), confidence=1.0,
                          result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "ambiguous"
        assert item.status == "skipped"
        assert await _tmdb_id_of(db_session, item) is None


class TestOmdbTieBreak:
    @pytest.mark.asyncio
    async def test_confidence_1_0_skips_omdb_entirely(self, db_session, monkeypatch):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fake = _FakeOmdb(data=_omdb_data(title="Completely Different", year="2020"))
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = FetchResult(item=item, data=_data(), confidence=1.0,
                          result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fake.calls == 0
        assert fr.result == "matched"
        assert item.status == "done"
        assert await _tmdb_id_of(db_session, item) == "218"

    @pytest.mark.asyncio
    async def test_year_and_title_contradiction_downgrades(self, db_session, monkeypatch):
        item = _item(title="Terminator", year=1984)
        db_session.add(_media(item))
        await db_session.flush()
        # Year off by 3 AND a wildly different title -> genuine contradiction.
        fake = _FakeOmdb(data=_omdb_data(title="Zzz Totally Unrelated Picture Qqq", year="1987"))
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = FetchResult(item=item, data=_data(year=1984), confidence=0.8,
                          result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fake.calls == 1
        assert fr.result == "ambiguous"
        assert item.status == "skipped"
        assert await _tmdb_id_of(db_session, item) is None

    @pytest.mark.asyncio
    async def test_same_year_different_language_title_keeps_match(self, db_session, monkeypatch):
        """Language-safety: OMDb often returns the English/original title for
        localized content — a low title similarity alone must never trigger
        a downgrade when the year still matches."""
        item = _item(title="Le Fugitif", year=1993)
        db_session.add(_media(item))
        await db_session.flush()
        fake = _FakeOmdb(data=_omdb_data(title="The Fugitive", year="1993"))
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = FetchResult(item=item, data=_data(tmdb_id=458, imdb="tt0106977", year=1993),
                          confidence=0.7, result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fake.calls == 1
        assert fr.result == "matched"
        assert item.status == "done"
        assert await _tmdb_id_of(db_session, item) == "458"

    @pytest.mark.asyncio
    async def test_omdb_not_found_keeps_match(self, db_session, monkeypatch):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fake = _FakeOmdb(data=None)  # not_found
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = FetchResult(item=item, data=_data(), confidence=0.7,
                          result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fake.calls == 1
        assert fr.result == "matched"
        assert item.status == "done"

    @pytest.mark.asyncio
    async def test_omdb_unconfigured_zero_calls_keeps_match(self, db_session, monkeypatch):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fake = _FakeOmdb(configured=False)
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = FetchResult(item=item, data=_data(), confidence=0.7,
                          result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fake.calls == 0
        assert fr.result == "matched"
        assert item.status == "done"

    @pytest.mark.asyncio
    async def test_budget_exhausted_zero_calls_keeps_match(self, db_session, monkeypatch):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fake = _FakeOmdb(request_count=settings.OMDB_DAILY_LIMIT)
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = FetchResult(item=item, data=_data(), confidence=0.7,
                          result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fake.calls == 0
        assert fr.result == "matched"
        assert item.status == "done"

    @pytest.mark.asyncio
    async def test_omdb_raises_keeps_match(self, db_session, monkeypatch):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fake = _FakeOmdb(raises=True)
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr = FetchResult(item=item, data=_data(), confidence=0.7,
                          result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "matched"
        assert item.status == "done"

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_recall_omdb(self, db_session, monkeypatch):
        """Second run against the same imdb_id must hit the OMDb scrape cache
        instead of re-calling the (fake, call-counting) OMDb service."""
        item1 = _item(rating_key="vod_a.mp4", server_id="xtream_a", title="Terminator", year=1984)
        item2 = _item(rating_key="vod_b.mp4", server_id="xtream_b", title="Terminator", year=1984)
        db_session.add_all([_media(item1), _media(item2, title="Terminator")])
        await db_session.flush()

        # Same imdb_id, contradiction shape so we can also verify the second
        # run still downgrades (proves it used the cached OMDb payload, not
        # a fresh no-signal path).
        fake = _FakeOmdb(data=_omdb_data(title="Zzz Totally Unrelated Picture Qqq", year="1987"))
        monkeypatch.setattr(ew, "omdb_service", fake)

        fr1 = FetchResult(item=item1, data=_data(year=1984), confidence=0.8,
                           result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr1])
        await db_session.commit()
        assert fake.calls == 1
        assert fr1.result == "ambiguous"

        fr2 = FetchResult(item=item2, data=_data(year=1984), confidence=0.8,
                           result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr2])
        await db_session.commit()

        assert fake.calls == 1  # no second HTTP call — served from the OMDb cache
        assert fr2.result == "ambiguous"
