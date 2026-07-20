"""Tests for the enrichment apply-phase guards + dual-provider (TMDB+OMDb)
write behaviour.

Two families live here, both exercising `_apply_enrichment_results` with
PRE-FETCHED `FetchResult.omdb` (the OMDb network fetch itself moved into the
concurrent `_resolve` phase — see `tests/test_enrichment_scraping.py` for the
fetch-side tests):

1. Guards: the cheap intra-record shape tripwire, and the OMDb tie-break that
   downgrades a low-confidence TMDB match to "ambiguous" when OMDb clearly
   contradicts it (year gap > 1 AND low title similarity).
2. Dual-provider writes (dual-provider enrichment design C3): systematic
   imdb_rating/imdb_votes COALESCE fill-missing, the blended display_rating,
   the OMDb-by-title identity policy (strong -> identity, weak -> metadata
   only), and the OMDb scrape-cache put dedup.
"""
import pytest
from sqlalchemy import func as sa_func
from sqlalchemy import select

from app.models.database import EnrichmentQueue, Media, OmdbScrapeCache
from app.services.omdb_service import OMDbData
from app.services.tmdb_service import TMDBEnrichmentData
from app.utils.rating_blend import blend_rating
from app.workers.enrichment_worker import FetchResult, _apply_enrichment_results


def _data(tmdb_id=218, imdb="tt0088247", year=1984, title="Terminator",
          vote_average=8.0, tmdb_rating=8.0, tmdb_votes=12000):
    """A matched TMDBEnrichmentData. `tmdb_rating` mirrors `vote_average` the
    way `tmdb_service._parse_details` does, so the display blend has a TMDB
    side to work with."""
    return TMDBEnrichmentData(
        tmdb_id=tmdb_id, imdb_id=imdb, overview="A cyborg assassin.",
        poster_url="http://img/p.jpg", backdrop_url="http://img/b.jpg",
        vote_average=vote_average, genres="Action, Sci-Fi", year=year, cast="Arnold",
        tmdb_rating=tmdb_rating, tmdb_votes=tmdb_votes,
    )


def _item(rating_key="vod_1.mp4", server_id="xtream_a", title="Terminator", year=1984,
          existing_imdb_id=None, existing_tmdb_id=None):
    return EnrichmentQueue(
        rating_key=rating_key, server_id=server_id, media_type="movie",
        title=title, year=year, status="pending", attempts=0, created_at=0,
        existing_imdb_id=existing_imdb_id, existing_tmdb_id=existing_tmdb_id,
    )


def _media(item, title="Terminator", imdb_rating=None, imdb_votes=None,
           tmdb_rating=None, display_rating=0.0, page_offset=0):
    return Media(
        rating_key=item.rating_key, server_id=item.server_id,
        library_section_id="1", title=title, type="movie",
        imdb_rating=imdb_rating, imdb_votes=imdb_votes, tmdb_rating=tmdb_rating,
        display_rating=display_rating, page_offset=page_offset,
    )


def _omdb_data(title="Terminator", year="1984", imdb_id="tt0088247",
               imdb_rating=8.1, imdb_votes=900000, type="movie",
               plot="A cyborg is sent back in time.", genre="Action",
               actors="Arnold Schwarzenegger"):
    return OMDbData(
        title=title, year=year, runtime_minutes=107, genre=genre,
        director="J. Cameron", actors=actors, plot=plot,
        imdb_rating=imdb_rating, imdb_votes=imdb_votes, type=type, imdb_id=imdb_id,
    )


async def _cols(db_session, item):
    return (await db_session.execute(
        select(
            Media.tmdb_id, Media.imdb_id, Media.unification_id, Media.history_group_key,
            Media.imdb_rating, Media.imdb_votes, Media.tmdb_rating, Media.scraped_rating,
            Media.display_rating, Media.summary, Media.genres, Media.cast, Media.year,
        ).where(
            Media.rating_key == item.rating_key, Media.server_id == item.server_id,
        )
    )).one()


async def _tmdb_id_of(db_session, item) -> str | None:
    row = (await db_session.execute(
        select(Media.tmdb_id).where(
            Media.rating_key == item.rating_key, Media.server_id == item.server_id,
        )
    )).scalar_one()
    return row


# ─── Shape tripwire ──────────────────────────────────────────────────────


class TestShapeTripwire:
    @pytest.mark.asyncio
    async def test_malformed_imdb_id_numeric_only(self, db_session):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()

        fr = FetchResult(item=item, data=_data(imdb="12345"), confidence=1.0,
                         result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "ambiguous"
        assert item.status == "skipped"
        assert await _tmdb_id_of(db_session, item) is None

    @pytest.mark.asyncio
    async def test_malformed_imdb_id_non_tt_prefix(self, db_session):
        item = _item(rating_key="vod_2.mp4")
        db_session.add(_media(item))
        await db_session.flush()

        fr = FetchResult(item=item, data=_data(imdb="ttabc"), confidence=1.0,
                         result="matched", api_used=1, cache_key=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "ambiguous"
        assert item.status == "skipped"
        assert await _tmdb_id_of(db_session, item) is None


# ─── OMDb tie-break (uses the PRE-FETCHED fr.omdb, no network) ─────────────


class TestOmdbContradiction:
    @pytest.mark.asyncio
    async def test_year_and_title_contradiction_downgrades(self, db_session):
        item = _item(title="Terminator", year=1984)
        db_session.add(_media(item))
        await db_session.flush()
        # Year off by 3 AND a wildly different title -> genuine contradiction.
        fr = FetchResult(item=item, data=_data(year=1984), confidence=0.8,
                         result="matched", api_used=1, cache_key=None,
                         omdb=_omdb_data(title="Zzz Totally Unrelated Picture Qqq", year="1987"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "ambiguous"
        assert item.status == "skipped"
        assert await _tmdb_id_of(db_session, item) is None

    @pytest.mark.asyncio
    async def test_same_year_different_language_title_keeps_match(self, db_session):
        """Language-safety: OMDb often returns the English/original title for
        localized content — a low title similarity alone must never trigger a
        downgrade when the year still matches."""
        item = _item(title="Le Fugitif", year=1993)
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(tmdb_id=458, imdb="tt0106977", year=1993),
                         confidence=0.7, result="matched", api_used=1, cache_key=None,
                         omdb=_omdb_data(title="The Fugitive", year="1993", imdb_id="tt0106977"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "matched"
        assert item.status == "done"
        assert await _tmdb_id_of(db_session, item) == "458"

    @pytest.mark.asyncio
    async def test_year_gap_but_similar_title_keeps_match(self, db_session):
        """AND-logic: a year gap > 1 alone (e.g. a same-title remake) is NOT
        conclusive without a low title similarity too — keep the match."""
        item = _item(title="Terminator", year=1984)
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(year=1984), confidence=0.8,
                         result="matched", api_used=1, cache_key=None,
                         omdb=_omdb_data(title="Terminator", year="1990"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "matched"
        assert item.status == "done"
        assert await _tmdb_id_of(db_session, item) == "218"

    @pytest.mark.asyncio
    async def test_omdb_none_keeps_match(self, db_session):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(), confidence=0.7,
                         result="matched", api_used=1, cache_key=None, omdb=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "matched"
        assert item.status == "done"

    @pytest.mark.asyncio
    async def test_confidence_1_0_ignores_contradiction_and_fills_ratings(self, db_session):
        """A perfect-confidence match (existing tmdb_id path) is trusted: the
        contradiction check is skipped, but the SINGLE OMDb fetch still enriches
        imdb_rating."""
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(), confidence=1.0,
                         result="matched", api_used=1, cache_key=None,
                         omdb=_omdb_data(title="Completely Different", year="2020"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert fr.result == "matched"
        assert item.status == "done"
        row = await _cols(db_session, item)
        assert row.tmdb_id == "218"
        assert row.imdb_rating == 8.1  # ratings still filled at confidence 1.0

    @pytest.mark.asyncio
    async def test_downgraded_match_writes_no_ratings(self, db_session):
        """A downgraded match writes NEITHER identity NOR ratings — even though
        the OMDb payload carried an imdb_rating."""
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(year=1984), confidence=0.8,
                         result="matched", api_used=1, cache_key=None,
                         omdb=_omdb_data(title="Zzz Totally Unrelated Picture Qqq", year="1987"),
                         omdb_put=("tt0088247", "found"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert fr.result == "ambiguous"
        assert row.tmdb_id is None
        assert row.imdb_rating is None      # no rating written on a downgrade
        assert row.display_rating == 0.0    # untouched
        # ...but the OMDb resolution is still cached (fetch outcome, not verdict).
        n = (await db_session.execute(
            select(sa_func.count()).select_from(OmdbScrapeCache)
            .where(OmdbScrapeCache.imdb_id == "tt0088247")
        )).scalar_one()
        assert n == 1


# ─── imdb_rating fill-missing + blended display_rating (design C3) ─────────


class TestOmdbRatingEnrichment:
    @pytest.mark.asyncio
    async def test_matched_fills_imdb_rating_and_blends_display(self, db_session):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(vote_average=8.0, tmdb_rating=8.0),
                         confidence=0.95, result="matched", api_used=2, cache_key=None,
                         omdb=_omdb_data(imdb_rating=8.1, imdb_votes=900000),
                         omdb_put=("tt0088247", "found"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_rating == 8.1
        assert row.imdb_votes == 900000
        assert row.scraped_rating == 8.0            # raw TMDB, unchanged intent
        assert row.tmdb_rating == 8.0
        assert row.display_rating == blend_rating(8.1, 8.0)  # blend, NOT vote_average

    @pytest.mark.asyncio
    async def test_matched_does_not_clobber_nfo_imdb_rating(self, db_session):
        # A richer NFO imdb_rating already present — OMDb must not overwrite it,
        # and the display blend must use the persisted (NFO) value.
        item = _item()
        db_session.add(_media(item, imdb_rating=9.9, imdb_votes=5))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(vote_average=8.0, tmdb_rating=8.0),
                         confidence=0.95, result="matched", api_used=2, cache_key=None,
                         omdb=_omdb_data(imdb_rating=8.1, imdb_votes=900000),
                         omdb_put=("tt0088247", "found"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_rating == 9.9               # NFO value preserved
        assert row.imdb_votes == 5
        assert row.display_rating == blend_rating(9.9, 8.0)

    @pytest.mark.asyncio
    async def test_display_one_sided_tmdb_only(self, db_session):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(vote_average=8.0, tmdb_rating=8.0),
                         confidence=0.95, result="matched", api_used=2, cache_key=None,
                         omdb=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_rating is None
        assert row.display_rating == 8.0            # one-sided (tmdb) blend branch

    @pytest.mark.asyncio
    async def test_display_one_sided_imdb_only(self, db_session):
        # TMDB matched but with no vote_average -> only the OMDb imdb side has a
        # rating -> one-sided imdb blend branch.
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(vote_average=None, tmdb_rating=None),
                         confidence=0.95, result="matched", api_used=2, cache_key=None,
                         omdb=_omdb_data(imdb_rating=7.4, imdb_votes=100),
                         omdb_put=("tt0088247", "found"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_rating == 7.4
        assert row.tmdb_rating is None
        assert row.display_rating == 7.4            # one-sided (imdb) blend branch

    @pytest.mark.asyncio
    async def test_display_else_noop_when_no_usable_rating(self, db_session):
        # OMDb present but no imdb_rating (votes only) and no TMDB match -> the
        # blend CASE else_ branch keeps the current display_rating (the branch
        # recompute_display_rating_stmt never reaches).
        item = _item()
        db_session.add(_media(item, display_rating=6.5))
        await db_session.flush()
        fr = FetchResult(item=item, data=None, confidence=0.4, result="nomatch",
                         api_used=2, cache_key=None,
                         omdb=_omdb_data(title="Terminator", year="1984",
                                         imdb_rating=None, imdb_votes=500),
                         omdb_put=("tt0088247", "found"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_rating is None
        assert row.imdb_votes == 500
        assert row.display_rating == 6.5            # else_ no-op: kept

    @pytest.mark.asyncio
    async def test_partial_fill_rating_present_votes_absent(self, db_session):
        """QA adversarial regression (mirror of
        `test_display_else_noop_when_no_usable_rating` above, which covers
        votes-present/rating-absent): OMDb sometimes returns a numeric
        `imdbRating` but "N/A" `imdbVotes` (parsed to `None` by
        `omdb_service._parse_imdb_votes`) for newly-listed titles. The two
        COALESCE fill-missing writes (`imdb_rating`/`imdb_votes`) are
        independent in `_apply_enrichment_results` — proves the rating gets
        filled while the absent votes are correctly left untouched (not
        zeroed, no crash), and the blend still uses the freshly-filled
        rating."""
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(vote_average=8.0, tmdb_rating=8.0),
                         confidence=0.95, result="matched", api_used=2, cache_key=None,
                         omdb=_omdb_data(imdb_rating=8.1, imdb_votes=None),
                         omdb_put=("tt0088247", "found"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_rating == 8.1                 # rating filled
        assert row.imdb_votes is None                 # nothing to fill -> stays None
        assert row.display_rating == blend_rating(8.1, 8.0)

    @pytest.mark.asyncio
    async def test_display_blend_uses_persisted_imdb_when_no_fresh_omdb(self, db_session):
        """QA adversarial regression: a PRIOR pass already filled
        `imdb_rating` (NFO import or an earlier OMDb hit). THIS pass has a
        fresh TMDB match but NO OMDb result (`omdb=None` — budget exhausted /
        unconfigured / fail-open exception, all indistinguishable to the
        apply phase). `display_rating` must still blend using the STALE
        PERSISTED `imdb_rating` (the bare `Media.imdb_rating` column operand,
        since `new_imdb is None` this pass) together with the freshly-written
        `tmdb_rating` — not silently drop to the one-sided tmdb-only branch
        just because no OMDb data arrived in this particular pass."""
        item = _item()
        db_session.add(_media(item, imdb_rating=9.0, imdb_votes=500))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(vote_average=7.0, tmdb_rating=7.0),
                         confidence=0.95, result="matched", api_used=2, cache_key=None,
                         omdb=None)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_rating == 9.0                        # untouched (persisted)
        assert row.display_rating == blend_rating(9.0, 7.0)  # blend uses the PERSISTED imdb side


# ─── OMDb-by-title identity policy (D-IDENTITY) ────────────────────────────


class TestOmdbTitleIdentity:
    @pytest.mark.asyncio
    async def test_strong_title_writes_identity(self, db_session):
        item = _item(title="Terminator", year=1984)
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=None, confidence=0.4, result="nomatch",
                         api_used=2, cache_key="movie|terminator|1984",
                         omdb=_omdb_data(imdb_id="tt0088247", imdb_rating=8.1),
                         omdb_put=("tt0088247", "found"), omdb_identity=True)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_id == "tt0088247"
        assert row.unification_id == "imdb://tt0088247"
        assert row.history_group_key == "imdb://tt0088247"
        assert row.summary == "A cyborg is sent back in time."   # metadata fill-missing
        assert row.genres == "Action"
        assert row.imdb_rating == 8.1                            # ratings fill-missing
        assert item.status == "done"

    @pytest.mark.asyncio
    async def test_weak_title_metadata_only_no_identity(self, db_session):
        item = _item(title="Terminator", year=1984)
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=None, confidence=0.4, result="nomatch",
                         api_used=2, cache_key="movie|terminator|1984",
                         omdb=_omdb_data(imdb_id="tt0088247", imdb_rating=8.1),
                         omdb_put=("tt0088247", "found"), omdb_identity=False)
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = await _cols(db_session, item)
        assert row.imdb_id is None                  # NO identity written
        assert row.unification_id == ""             # untouched (default)
        assert row.summary == "A cyborg is sent back in time."   # metadata still filled
        assert row.imdb_rating == 8.1                            # ratings still filled
        assert item.status == "skipped"


# ─── OMDb scrape-cache put dedup (regression: the double-INSERT) ────────────


class TestOmdbCachePutDedup:
    @pytest.mark.asyncio
    async def test_same_batch_shared_imdb_id_one_put(self, db_session):
        """Two items in the SAME batch sharing one imdb_id (same film synced
        from two Xtream accounts) each carry an `omdb_put` for that id. Under
        `no_autoflush` a naive per-item put would double-INSERT and raise
        `UNIQUE constraint failed: omdb_scrape_cache.imdb_id` at commit,
        permanently stalling enrichment. The batch must dedupe puts by id."""
        item1 = _item(rating_key="vod_a.mp4", server_id="xtream_a")
        item2 = _item(rating_key="vod_b.mp4", server_id="xtream_b")
        db_session.add_all([_media(item1), _media(item2)])
        await db_session.flush()

        omdb = _omdb_data(title="Zzz Totally Unrelated Picture Qqq", year="1987")
        fr1 = FetchResult(item=item1, data=_data(year=1984), confidence=0.8,
                          result="matched", api_used=1, cache_key=None,
                          omdb=omdb, omdb_put=("tt0088247", "found"))
        fr2 = FetchResult(item=item2, data=_data(year=1984), confidence=0.8,
                          result="matched", api_used=1, cache_key=None,
                          omdb=omdb, omdb_put=("tt0088247", "found"))

        # Single batch, single commit — must not raise IntegrityError.
        await _apply_enrichment_results(db_session, [fr1, fr2])
        await db_session.commit()

        n = (await db_session.execute(
            select(sa_func.count()).select_from(OmdbScrapeCache)
            .where(OmdbScrapeCache.imdb_id == "tt0088247")
        )).scalar_one()
        assert n == 1  # one cache row, not two
        # Consistent verdict: both downgraded (year gap + title mismatch).
        assert fr1.result == "ambiguous" and fr2.result == "ambiguous"
        assert await _tmdb_id_of(db_session, item1) is None
        assert await _tmdb_id_of(db_session, item2) is None

    @pytest.mark.asyncio
    async def test_omdb_put_not_found_persisted(self, db_session):
        item = _item()
        db_session.add(_media(item))
        await db_session.flush()
        fr = FetchResult(item=item, data=_data(), confidence=0.95, result="matched",
                         api_used=1, cache_key=None, omdb=None,
                         omdb_put=("tt0088247", "not_found"))
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        row = (await db_session.execute(
            select(OmdbScrapeCache).where(OmdbScrapeCache.imdb_id == "tt0088247")
        )).scalars().one()
        assert row.result == "not_found"
        assert row.payload is None
