"""`_apply_enrichment_results`' fill-missing write of `Media.youtube_trailer`
(Lot A trailers) — mirrors the "rich metadata" tuple's COALESCE fill-missing
tests in `tests/test_enrichment_guard.py` (`original_title`/`studio`/...).

`youtube_trailer` must NEVER clobber a value already captured by the Xtream
sync (`sync_worker.map_vod_to_media`/`fetch_series_episodes`) — only fills a
NULL column.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.database import EnrichmentQueue, Media
from app.services.tmdb_service import TMDBEnrichmentData
from app.workers.enrichment_worker import FetchResult, _apply_enrichment_results


def _item(rating_key="vod_1.mp4", server_id="xtream_a", title="Terminator", year=1984):
    return EnrichmentQueue(
        rating_key=rating_key, server_id=server_id, media_type="movie",
        title=title, year=year, status="pending", attempts=0, created_at=0,
    )


def _media(item, youtube_trailer=None, page_offset=0):
    return Media(
        rating_key=item.rating_key, server_id=item.server_id,
        library_section_id="1", title="Terminator", type="movie",
        youtube_trailer=youtube_trailer, page_offset=page_offset,
    )


def _data(youtube_trailer=None, tmdb_id=218, imdb="tt0088247"):
    return TMDBEnrichmentData(
        tmdb_id=tmdb_id, imdb_id=imdb, overview="A cyborg assassin.",
        poster_url="http://img/p.jpg", backdrop_url="http://img/b.jpg",
        vote_average=8.0, genres="Action, Sci-Fi", year=1984, cast="Arnold",
        youtube_trailer=youtube_trailer,
    )


async def _trailer_of(db_session, item) -> str | None:
    return (await db_session.execute(
        select(Media.youtube_trailer).where(
            Media.rating_key == item.rating_key, Media.server_id == item.server_id,
        )
    )).scalar_one()


class TestYoutubeTrailerFillMissing:
    @pytest.mark.asyncio
    async def test_tmdb_resolved_trailer_fills_empty_column(self, db_session):
        item = _item()
        db_session.add(_media(item, youtube_trailer=None))
        await db_session.flush()

        fr = FetchResult(
            item=item, data=_data(youtube_trailer="dQw4w9WgXcQ"),
            confidence=0.97, result="matched", api_used=1, cache_key=None,
        )
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert await _trailer_of(db_session, item) == "dQw4w9WgXcQ"

    @pytest.mark.asyncio
    async def test_existing_xtream_trailer_is_never_clobbered(self, db_session):
        """A trailer key already captured at sync (Xtream `youtube_trailer`)
        must survive even when TMDB resolves a DIFFERENT one — fill-missing
        only, via `func.coalesce(Media.youtube_trailer, value)`."""
        item = _item()
        db_session.add(_media(item, youtube_trailer="already_set12"))
        await db_session.flush()

        fr = FetchResult(
            item=item, data=_data(youtube_trailer="different_id"),
            confidence=0.97, result="matched", api_used=1, cache_key=None,
        )
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert await _trailer_of(db_session, item) == "already_set12"

    @pytest.mark.asyncio
    async def test_no_tmdb_trailer_leaves_column_untouched(self, db_session):
        item = _item()
        db_session.add(_media(item, youtube_trailer=None))
        await db_session.flush()

        fr = FetchResult(
            item=item, data=_data(youtube_trailer=None),
            confidence=0.97, result="matched", api_used=1, cache_key=None,
        )
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert await _trailer_of(db_session, item) is None

    @pytest.mark.asyncio
    async def test_no_match_never_writes_a_trailer(self, db_session):
        """`nomatch`/no `enrichment_data` -> the `rich` tuple loop (which the
        trailer fill-missing lives in) is only reached inside the
        `if enrichment_data:` branch — must not raise, must not write."""
        item = _item()
        db_session.add(_media(item, youtube_trailer=None))
        await db_session.flush()

        fr = FetchResult(
            item=item, data=None, confidence=0.4, result="nomatch",
            api_used=1, cache_key=None,
        )
        await _apply_enrichment_results(db_session, [fr])
        await db_session.commit()

        assert await _trailer_of(db_session, item) is None
