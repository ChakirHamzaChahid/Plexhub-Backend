"""Tests for adult / X-rated movie tagging.

Movies in an adult Xtream category (name matches a keyword like "ADULT"/"+18",
or category_id explicitly listed) are flagged ``is_adult``, get ``content_rating``
forced to ``settings.ADULT_CONTENT_RATING`` (NFO ``<mpaa>`` + API), and are
prefixed ``[XXX]`` in the API title only — the generated Plex/Jellyfin library
folder stays clean (it signals +18 via ``<mpaa>``).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.migrations import _migration_013_add_media_is_adult
from app.models.database import Media, XtreamAccount, XtreamCategory
from app.models.schemas import (
    ADULT_TITLE_PREFIX, MediaResponse, UnifiedMediaResponse, apply_adult_prefix,
)
from app.services.category_service import (
    _is_adult_category_name, update_media_adult_flags,
)
from app.utils.server_id import build_server_id


# pytest-asyncio runs in auto mode (pyproject.toml) — async tests need no mark.

# The JSON API is X-API-Key gated (fail-closed); the API tests set this as the
# master secret and send it as the header.
API_KEY = "test-master-key"
API_HEADERS = {"X-API-Key": API_KEY}


# ─── Seed helpers ────────────────────────────────────────────────────────


def _account(id_: str = "a") -> XtreamAccount:
    return XtreamAccount(
        id=id_, label="Compte", base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


def _category(account_id: str, category_id: str, name: str,
              type_: str = "vod") -> XtreamCategory:
    return XtreamCategory(
        account_id=account_id, category_id=category_id, category_type=type_,
        category_name=name, is_allowed=True, last_fetched_at=0,
    )


def _movie(account_id: str, rating_key: str, title: str, category_id: str,
           content_rating: str | None = "PG-13",
           unif: str = "") -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter=category_id, sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=2020, content_rating=content_rating,
        unification_id=unif or f"tmdb://{rating_key}",
        is_in_allowed_categories=True, is_broken=False,
    )


# ─── Category name matching ──────────────────────────────────────────────


class TestAdultCategoryName:
    @pytest.mark.parametrize("name", [
        "VOD - ADULT +18", "XXX", "18+ Films", "Porno", "X-Rated Movies",
        "vod adult",
    ])
    def test_matches(self, name):
        assert _is_adult_category_name(name) is True

    @pytest.mark.parametrize("name", ["Action", "Kids", "Comédie", "", None])
    def test_rejects(self, name):
        assert _is_adult_category_name(name) is False


# ─── Reconciliation (update_media_adult_flags) ───────────────────────────


class TestAdultReconciliation:
    @pytest_asyncio.fixture
    async def factory(self, db_engine):
        return async_sessionmaker(db_engine, class_=AsyncSession,
                                  expire_on_commit=False)

    async def _seed(self, factory):
        async with factory() as s:
            s.add_all([
                _account("a"),
                _category("a", "1555", "VOD - ADULT +18"),
                _category("a", "10", "Action"),
                _movie("a", "vod_adult.mp4", "Naughty Film (2020)", "1555"),
                _movie("a", "vod_normal.mp4", "Action Hero (2020)", "10"),
            ])
            await s.commit()

    async def _flag(self, rating_key, factory):
        async with factory() as s:
            row = (await s.execute(
                select(Media).where(Media.rating_key == rating_key)
            )).scalar_one()
            return row

    async def test_adult_flagged_and_rating_forced(self, factory):
        await self._seed(factory)
        async with factory() as s:
            await update_media_adult_flags(s, "a")

        adult = await self._flag("vod_adult.mp4", factory)
        assert adult.is_adult is True
        assert adult.content_rating == settings.ADULT_CONTENT_RATING

        normal = await self._flag("vod_normal.mp4", factory)
        assert normal.is_adult is False
        assert normal.content_rating == "PG-13"  # untouched

    async def test_idempotent(self, factory):
        await self._seed(factory)
        async with factory() as s:
            await update_media_adult_flags(s, "a")
        async with factory() as s:
            await update_media_adult_flags(s, "a")  # second pass must not change

        adult = await self._flag("vod_adult.mp4", factory)
        assert adult.is_adult is True
        assert adult.content_rating == settings.ADULT_CONTENT_RATING
        normal = await self._flag("vod_normal.mp4", factory)
        assert normal.is_adult is False

    async def test_reclassification_clears_stale_flag(self, factory):
        """A movie previously adult whose category is no longer adult gets reset."""
        await self._seed(factory)
        # Manually pre-mark the normal movie as adult (stale state).
        async with factory() as s:
            await s.execute(text(
                "UPDATE media SET is_adult=1 WHERE rating_key='vod_normal.mp4'"
            ))
            await s.commit()

        async with factory() as s:
            await update_media_adult_flags(s, "a")

        normal = await self._flag("vod_normal.mp4", factory)
        assert normal.is_adult is False

    async def test_explicit_category_id(self, factory, monkeypatch):
        """A neutrally-named category tagged adult via ADULT_CATEGORY_IDS."""
        monkeypatch.setattr(settings, "ADULT_CATEGORY_IDS", ["10"])
        await self._seed(factory)
        async with factory() as s:
            await update_media_adult_flags(s, "a")

        # Category "10" ("Action") is now adult because its id is listed.
        normal = await self._flag("vod_normal.mp4", factory)
        assert normal.is_adult is True
        assert normal.content_rating == settings.ADULT_CONTENT_RATING


# ─── Title prefix serialization ──────────────────────────────────────────


class TestAdultPrefix:
    def test_apply_prefix(self):
        assert apply_adult_prefix("Movie", True) == "[XXX] Movie"
        assert apply_adult_prefix("Movie", False) == "Movie"

    def test_apply_prefix_idempotent(self):
        once = apply_adult_prefix("Movie", True)
        assert apply_adult_prefix(once, True) == once  # no double prefix

    def test_media_response_prefixes_adult_title(self):
        resp = MediaResponse(
            rating_key="k", server_id="xtream_a", library_section_id="xtream_vod",
            title="Naughty Film", type="movie", is_adult=True,
        )
        assert resp.title == "[XXX] Naughty Film"

    def test_media_response_keeps_normal_title(self):
        resp = MediaResponse(
            rating_key="k", server_id="xtream_a", library_section_id="xtream_vod",
            title="Action Hero", type="movie", is_adult=False,
        )
        assert resp.title == "Action Hero"

    def test_unified_response_carries_is_adult(self):
        resp = UnifiedMediaResponse(
            unification_id="tmdb://1", type="movie", title="[XXX] X", is_adult=True,
        )
        dumped = resp.model_dump(by_alias=True)
        assert dumped["isAdult"] is True


# ─── End-to-end API ──────────────────────────────────────────────────────


class TestAdultApi:
    @pytest_asyncio.fixture
    async def seeded(self, db_engine, monkeypatch):
        from app.db import database as db_module
        factory = async_sessionmaker(db_engine, class_=AsyncSession,
                                     expire_on_commit=False)
        monkeypatch.setattr(db_module, "async_session_factory", factory)
        monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
        async with factory() as s:
            adult = _movie("a", "vod_adult.mp4", "Naughty Film (2020)", "1555",
                           content_rating=settings.ADULT_CONTENT_RATING)
            adult.is_adult = True
            normal = _movie("a", "vod_normal.mp4", "Action Hero (2020)", "10")
            s.add_all([_account("a"), adult, normal])
            await s.commit()
        return factory

    async def test_unified_movies_prefixed_and_flagged(self, api_client, seeded):
        resp = await api_client.get("/api/media/movies/unified", headers=API_HEADERS)
        assert resp.status_code == 200
        items = {i["title"]: i for i in resp.json()["items"]}
        assert "[XXX] Naughty Film" in items
        assert items["[XXX] Naughty Film"]["isAdult"] is True
        assert items["[XXX] Naughty Film"]["contentRating"] == settings.ADULT_CONTENT_RATING
        assert "Action Hero" in items
        assert items["Action Hero"]["isAdult"] is False

    async def test_non_unified_movies_prefixed(self, api_client, seeded):
        resp = await api_client.get("/api/media/movies", headers=API_HEADERS)
        assert resp.status_code == 200
        titles = {i["title"]: i for i in resp.json()["items"]}
        assert any(t.startswith("[XXX] ") and i["isAdult"] for t, i in titles.items())
        # Normal movie is not prefixed.
        assert any(t == "Action Hero (2020)" and not i["isAdult"]
                   for t, i in titles.items())


# ─── Plex/Jellyfin library stays clean (no [XXX] in folder, <mpaa> set) ───


class TestAdultLibrary:
    def test_nfo_emits_mpaa_rating(self):
        from app.plex_generator.models import PlexMovie, PlexMovieVersion
        from app.plex_generator.nfo_builder import build_movie_nfo

        movie = PlexMovie(
            source_id="tmdb://1", title="Naughty Film", year=2020,
            content_rating=settings.ADULT_CONTENT_RATING,
            versions=[PlexMovieVersion(source_id="vod_1.mp4",
                                       stream_url="http://x/1.mp4")],
        )
        xml = build_movie_nfo(movie)
        assert f"<mpaa>{settings.ADULT_CONTENT_RATING}</mpaa>" in xml

    def test_library_path_has_no_prefix(self):
        from app.plex_generator.naming import movie_version_path

        # The generator is fed the clean title (canonical_title_year), so no
        # "[XXX]" leaks into on-disk folders/files.
        path = movie_version_path("Naughty Film", 2020, "VF · Compte 1")
        assert ADULT_TITLE_PREFIX not in path
        assert "[XXX]" not in path


# ─── Migration 013 ───────────────────────────────────────────────────────


class TestMigration013:
    async def test_idempotent_and_column_present(self, db_engine):
        # create_all already added is_adult; the guarded ALTER must not raise.
        await _migration_013_add_media_is_adult(db_engine)
        await _migration_013_add_media_is_adult(db_engine)

        async with db_engine.connect() as conn:
            cols = (await conn.execute(text("PRAGMA table_info(media)"))).fetchall()
        names = {c[1] for c in cols}
        assert "is_adult" in names
