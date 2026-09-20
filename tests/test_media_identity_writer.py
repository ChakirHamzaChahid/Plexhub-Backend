"""ADR 0005 D4 (W0) — `media_identity_writer.build_identity_values` /
`build_rating_values`, both modes.

Functional style: build the dict, apply it via a real
`update(Media).values(**d)` against an in-memory SQLite row, then assert
the PERSISTED column values — comparing the raw dicts with `==` is unsafe
(SQLAlchemy `ColumnElement.__eq__` returns another expression, not a bool,
so `dict1 == dict2` on dicts containing `func.coalesce(...)` values raises).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.models.database import Media
from app.services.media_identity_writer import build_identity_values, build_rating_values
from app.services.omdb_service import OMDbData
from app.services.tmdb_service import TMDBEnrichmentData


def _tmdb_data(**overrides) -> TMDBEnrichmentData:
    base = dict(
        tmdb_id=218, imdb_id="tt0088247", overview="A cyborg assassin.",
        poster_url="http://img/new_poster.jpg", backdrop_url="http://img/new_backdrop.jpg",
        vote_average=8.0, genres="Action, Sci-Fi", year=1984, cast="Arnold, Linda",
        original_title="The Terminator", tagline="It's nothing personal.",
        premiered="1984-10-26", status="Released", studio="Orion Pictures",
        country="United States", content_rating="R", tvdb_id="218", wikidata_id="Q133654",
        tmdb_rating=8.0, tmdb_votes=9000, cast_json='[{"name": "Arnold"}]',
        youtube_trailer="k64P4l2Wmeg",
    )
    base.update(overrides)
    return TMDBEnrichmentData(**base)


def _omdb_data(**overrides) -> OMDbData:
    base = dict(
        title="The Terminator", year="1984", runtime_minutes=107, genre="Action, Sci-Fi",
        director="James Cameron", actors="Arnold Schwarzenegger", plot="A soldier is sent...",
        imdb_rating=8.1, imdb_votes=900000, type="movie", imdb_id="tt0088247",
    )
    base.update(overrides)
    return OMDbData(**base)


def _row(**overrides) -> Media:
    base = dict(
        rating_key="rk1", server_id="acct1", filter="all", sort_order="default",
        library_section_id="xtream_vod", page_offset=0,
        title="Terminator (VF)", type="movie",
        unification_id="title_rk1", history_group_key="title_rk1",
        thumb_url="http://xtream/original_thumb.jpg",
        art_url="http://xtream/original_art.jpg",
        is_in_allowed_categories=True, is_broken=False,
    )
    base.update(overrides)
    return Media(**base)


async def _apply(db_session, rating_key: str, values: dict) -> Media:
    await db_session.execute(
        update(Media).where(Media.rating_key == rating_key, Media.server_id == "acct1").values(**values)
    )
    await db_session.commit()
    db_session.expire_all()
    return (await db_session.execute(select(Media).where(Media.rating_key == rating_key))).scalars().one()


class TestBuildIdentityValuesFill:
    async def test_identity_fields_always_set(self, db_session):
        db_session.add(_row())
        await db_session.commit()
        values = build_identity_values(_tmdb_data(), confidence=0.97, omdb=None, mode="fill")
        row = await _apply(db_session, "rk1", values)
        assert row.tmdb_id == "218"
        assert row.imdb_id == "tt0088247"
        assert row.unification_id == "imdb://tt0088247"
        assert row.history_group_key == "imdb://tt0088247"
        assert row.tmdb_match_confidence == 0.97

    async def test_falls_back_to_tmdb_uri_when_no_imdb(self, db_session):
        db_session.add(_row())
        await db_session.commit()
        values = build_identity_values(_tmdb_data(imdb_id=None), confidence=0.9, omdb=None, mode="fill")
        row = await _apply(db_session, "rk1", values)
        assert row.imdb_id is None
        assert row.unification_id == "tmdb://218"

    async def test_overwrites_direct_fields_when_tmdb_provides_them(self, db_session):
        db_session.add(_row(summary="old summary", genres="Old", year=1980, cast="Old cast"))
        await db_session.commit()
        values = build_identity_values(_tmdb_data(), confidence=1.0, omdb=None, mode="fill")
        row = await _apply(db_session, "rk1", values)
        assert row.summary == "A cyborg assassin."
        assert row.genres == "Action, Sci-Fi"
        assert row.year == 1984
        assert row.cast == "Arnold, Linda"
        assert row.resolved_thumb_url == "http://img/new_poster.jpg"
        assert row.resolved_art_url == "http://img/new_backdrop.jpg"
        assert row.scraped_rating == 8.0

    async def test_direct_fields_untouched_when_tmdb_gives_nothing(self, db_session):
        db_session.add(_row(summary="keep me", genres="KeepGenre"))
        await db_session.commit()
        data = _tmdb_data(overview=None, genres=None, poster_url=None, backdrop_url=None,
                           vote_average=None, year=None, cast=None)
        values = build_identity_values(data, confidence=0.9, omdb=None, mode="fill")
        row = await _apply(db_session, "rk1", values)
        assert row.summary == "keep me"
        assert row.genres == "KeepGenre"

    async def test_rich_columns_coalesce_never_clobber_existing(self, db_session):
        db_session.add(_row(content_rating="XXX", original_title="Existing Original"))
        await db_session.commit()
        values = build_identity_values(_tmdb_data(), confidence=1.0, omdb=None, mode="fill")
        row = await _apply(db_session, "rk1", values)
        # COALESCE(existing, new) -> existing wins since it was non-NULL.
        assert row.content_rating == "XXX"
        assert row.original_title == "Existing Original"
        # But a previously-NULL rich column DOES get filled.
        assert row.tagline == "It's nothing personal."

    async def test_omdb_ignored_in_fill_mode(self, db_session):
        """ADR 0005 D4: fill mode ignores `omdb` entirely — the COALESCE
        already protects existing values (e.g. adult-tag content_rating)."""
        db_session.add(_row())
        await db_session.commit()
        omdb = _omdb_data(plot="OMDb plot should be ignored")
        values = build_identity_values(_tmdb_data(overview=None), confidence=1.0, omdb=omdb, mode="fill")
        row = await _apply(db_session, "rk1", values)
        assert row.summary is None  # not filled from OMDb in fill mode


class TestBuildRatingValuesFill:
    async def test_coalesce_fill_missing_and_blend(self, db_session):
        db_session.add(_row(display_rating=0.0))
        await db_session.commit()
        values = build_rating_values(
            omdb_imdb_rating=8.1, omdb_imdb_votes=900000, tmdb_rating=7.9, mode="fill",
        )
        row = await _apply(db_session, "rk1", values)
        assert row.imdb_rating == 8.1
        assert row.imdb_votes == 900000
        assert row.display_rating == pytest.approx((8.1 + 7.9) / 2)

    async def test_never_clobbers_existing_imdb_rating(self, db_session):
        db_session.add(_row(imdb_rating=9.5, imdb_votes=1000000))
        await db_session.commit()
        values = build_rating_values(
            omdb_imdb_rating=1.0, omdb_imdb_votes=1, tmdb_rating=None, mode="fill",
        )
        row = await _apply(db_session, "rk1", values)
        assert row.imdb_rating == 9.5
        assert row.imdb_votes == 1000000


class TestBuildIdentityValuesReplace:
    async def test_prefers_tmdb_falls_back_to_omdb_never_null(self, db_session):
        db_session.add(_row(summary="old", genres="OldGenre", cast="OldCast", year=1970))
        await db_session.commit()
        data = _tmdb_data(overview=None, genres=None, cast=None, year=None)
        omdb = _omdb_data(plot="OMDb plot", genre="OMDb Genre", actors="OMDb Actors", year="1984")
        values = build_identity_values(data, confidence=1.0, omdb=omdb, mode="replace")
        row = await _apply(db_session, "rk1", values)
        assert row.summary == "OMDb plot"
        assert row.genres == "OMDb Genre"
        assert row.cast == "OMDb Actors"
        assert row.year == 1984

    async def test_neither_source_leaves_column_untouched(self, db_session):
        db_session.add(_row(summary="keep me untouched"))
        await db_session.commit()
        data = _tmdb_data(overview=None)
        values = build_identity_values(data, confidence=1.0, omdb=None, mode="replace")
        row = await _apply(db_session, "rk1", values)
        assert row.summary == "keep me untouched"

    async def test_content_rating_cert_from_tmdb_when_not_adult(self, db_session):
        db_session.add(_row(content_rating="OLD"))
        await db_session.commit()
        values = build_identity_values(_tmdb_data(), confidence=1.0, omdb=None, mode="replace", is_adult=False)
        row = await _apply(db_session, "rk1", values)
        assert row.content_rating == "R"

    async def test_content_rating_xxx_kept_when_is_adult(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "ADULT_CONTENT_RATING", "XXX")
        db_session.add(_row(content_rating="XXX"))
        await db_session.commit()
        # Even though TMDB returns a "normal" certification, is_adult wins.
        values = build_identity_values(
            _tmdb_data(content_rating="R"), confidence=1.0, omdb=None, mode="replace", is_adult=True,
        )
        row = await _apply(db_session, "rk1", values)
        assert row.content_rating == "XXX"

    async def test_resolved_thumb_resets_to_xtream_raw_when_tmdb_has_none(self, db_session):
        db_session.add(_row(
            thumb_url="http://xtream/raw.jpg", art_url="http://xtream/raw_art.jpg",
            resolved_thumb_url="http://wrong/poster.jpg", resolved_art_url="http://wrong/art.jpg",
        ))
        await db_session.commit()
        data = _tmdb_data(poster_url=None, backdrop_url=None)
        values = build_identity_values(data, confidence=1.0, omdb=None, mode="replace")
        row = await _apply(db_session, "rk1", values)
        assert row.resolved_thumb_url == "http://xtream/raw.jpg"
        assert row.resolved_art_url == "http://xtream/raw_art.jpg"

    async def test_resolved_thumb_uses_fresh_tmdb_poster(self, db_session):
        db_session.add(_row(resolved_thumb_url="http://wrong/poster.jpg"))
        await db_session.commit()
        values = build_identity_values(_tmdb_data(), confidence=1.0, omdb=None, mode="replace")
        row = await _apply(db_session, "rk1", values)
        assert row.resolved_thumb_url == "http://img/new_poster.jpg"

    async def test_provider_only_cols_null_out_when_tmdb_gives_nothing(self, db_session):
        db_session.add(_row(tagline="Old wrong tagline", studio="Old wrong studio"))
        await db_session.commit()
        data = _tmdb_data(tagline=None, studio=None)
        values = build_identity_values(data, confidence=1.0, omdb=None, mode="replace")
        row = await _apply(db_session, "rk1", values)
        assert row.tagline is None
        assert row.studio is None

    async def test_provider_only_cols_direct_assign_new_value(self, db_session):
        db_session.add(_row(tagline="Old wrong tagline"))
        await db_session.commit()
        values = build_identity_values(_tmdb_data(), confidence=1.0, omdb=None, mode="replace")
        row = await _apply(db_session, "rk1", values)
        assert row.tagline == "It's nothing personal."
        assert row.scraped_rating == 8.0


class TestBuildRatingValuesReplace:
    async def test_direct_assign_no_coalesce(self, db_session):
        db_session.add(_row(imdb_rating=9.9, imdb_votes=1))
        await db_session.commit()
        values = build_rating_values(
            omdb_imdb_rating=8.1, omdb_imdb_votes=900000, tmdb_rating=7.9, mode="replace",
        )
        row = await _apply(db_session, "rk1", values)
        assert row.imdb_rating == 8.1
        assert row.imdb_votes == 900000
        assert row.display_rating == pytest.approx((8.1 + 7.9) / 2)

    async def test_nulls_out_when_no_omdb_rating(self, db_session):
        db_session.add(_row(imdb_rating=9.9, imdb_votes=1))
        await db_session.commit()
        values = build_rating_values(
            omdb_imdb_rating=None, omdb_imdb_votes=None, tmdb_rating=None, mode="replace",
        )
        row = await _apply(db_session, "rk1", values)
        assert row.imdb_rating is None
        assert row.imdb_votes is None

    async def test_display_rating_reset_to_zero_when_both_absent(self, db_session):
        """ADR 0005 W0-review follow-up (a): a correction with neither an
        OMDb nor a TMDB rating in hand must NOT keep the WRONG film's old
        `display_rating` — it is reset to `0.0` (the column is NOT NULL;
        `0.0` is the same "absent" sentinel `blend_rating`'s own `<= 0`
        rule uses everywhere else), never left stale."""
        db_session.add(_row(display_rating=3.3, imdb_rating=None, tmdb_rating=None))
        await db_session.commit()
        values = build_rating_values(
            omdb_imdb_rating=None, omdb_imdb_votes=None, tmdb_rating=None, mode="replace",
        )
        row = await _apply(db_session, "rk1", values)
        assert row.display_rating == 0.0
