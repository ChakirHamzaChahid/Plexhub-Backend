"""CR-P07 characterization: the media list endpoints now serialize their
response model ONCE (``_single_pass_json`` returns a ready ``Response`` so
FastAPI skips its own re-validation + re-dump pass), with ``response_model=``
kept only for the OpenAPI schema.

Two guard levels:
  1. INVARIANCE (unit): ``_single_pass_json(model)`` bytes == what FastAPI's own
     serialization path (``jsonable_encoder(model, by_alias=True)``) would have
     produced — i.e. the optimization is output-preserving, not just "close".
  2. WIRING (HTTP, in-process ASGI): every list endpoint still returns 200,
     camelCase aliases, the adult ``[XXX] `` title prefix, correct pagination
     (`hasMore`) and unified `versions[]` — proving the ``Response`` return is
     wired correctly through the real FastAPI stack.
"""
from __future__ import annotations

import json

import pytest_asyncio
from fastapi.encoders import jsonable_encoder

from app.config import settings
from app.db import database as db_module
from app.models.database import Media, XtreamAccount
from app.models.schemas import (
    MediaListResponse,
    MediaResponse,
    UnifiedEpisodeListResponse,
    UnifiedEpisodeResponse,
    UnifiedMediaListResponse,
    UnifiedMediaResponse,
    MediaVersionResponse,
)
from app.utils.server_id import build_server_id

# pytest-asyncio auto mode (pyproject.toml) — async tests need no explicit mark.

API_KEY = "test-key-singlepass"
API_HEADERS = {"X-API-Key": API_KEY}


# ─── level 1: helper invariance vs FastAPI's own serialization ──────────────


def _sample_media(**over) -> Media:
    # A DB-materialized row has its server-default columns filled; an in-Python
    # `Media(...)` leaves them None, which would fail `MediaResponse`'s
    # non-Optional fields — so set them explicitly to mirror a hydrated row.
    base = dict(
        rating_key="vod_1.mp4", server_id=build_server_id("a"),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title="The Matrix", title_sortable="matrix", type="movie", year=1999,
        unification_id="tmdb://603", history_group_key="tmdb://603",
        added_at=100, updated_at=0, page_offset=0, is_in_allowed_categories=True,
        is_broken=False, imdb_id="tt0133093", tmdb_id="603",
        display_rating=8.7, summary="A hacker learns the truth.",
        genres="Action, Sci-Fi", is_adult=False,
        view_offset=0, view_count=0, last_viewed_at=0, media_parts="[]",
    )
    base.update(over)
    return Media(**base)


def _assert_singlepass_matches_fastapi(model) -> None:
    """The helper's serialized bytes must equal FastAPI's default serialization
    (``jsonable_encoder(..., by_alias=True)``) for the SAME model — that is the
    exact path ``response_model=`` would have taken before CR-P07."""
    from app.api.media import _single_pass_json

    produced = json.loads(_single_pass_json(model).body)
    expected = jsonable_encoder(model, by_alias=True)
    assert produced == expected


def test_singlepass_matches_fastapi_for_media_list():
    model = MediaListResponse(
        items=[MediaResponse.model_validate(_sample_media())],
        total=1, has_more=False,
    )
    _assert_singlepass_matches_fastapi(model)


def test_singlepass_matches_fastapi_for_unified_list():
    model = UnifiedMediaListResponse(
        items=[UnifiedMediaResponse(
            unification_id="tmdb://603", type="movie", title="The Matrix",
            year=1999, rating=8.7, is_adult=False, version_count=1,
            versions=[MediaVersionResponse(
                server_id=build_server_id("a"), rating_key="vod_1.mp4",
                title="The Matrix", label="Compte 1", is_broken=False,
            )],
        )],
        total=1, has_more=False,
    )
    _assert_singlepass_matches_fastapi(model)


def test_singlepass_matches_fastapi_for_unified_episodes():
    model = UnifiedEpisodeListResponse(
        unification_id="tmdb://1396", series_title="Breaking Bad",
        items=[UnifiedEpisodeResponse(
            season=1, episode=1, title="Pilot", version_count=1, versions=[],
        )],
        total=1,
    )
    _assert_singlepass_matches_fastapi(model)


def test_singlepass_preserves_none_and_camel_aliases():
    """None fields serialize as null (exclude_none stays False) and keys are
    camelCase — the two properties most likely to silently regress."""
    from app.api.media import _single_pass_json

    model = MediaListResponse(
        items=[MediaResponse.model_validate(_sample_media(summary=None))],
        total=1, has_more=True,
    )
    payload = json.loads(_single_pass_json(model).body)
    assert payload["hasMore"] is True
    item = payload["items"][0]
    assert item["ratingKey"] == "vod_1.mp4"      # camelCase alias
    assert item["serverId"] == build_server_id("a")
    assert "summary" in item and item["summary"] is None  # None -> null, not dropped


# ─── level 2: HTTP wiring through the real FastAPI stack ─────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _wire_test_db(monkeypatch, db_factory):
    """Point get_db at the isolated in-memory engine + set the master key
    (same pattern as tests/test_router_http_coverage.py)."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    return db_factory


@pytest_asyncio.fixture
async def seeded(db_factory):
    # page_offset must be unique within each (server_id, library_section_id,
    # filter, sort_order) partition — the uix_media_pagination unique index.
    def _movie(rk, title, unif, added, page_offset, adult=False):
        return Media(
            rating_key=rk, server_id=build_server_id("a"),
            filter="all", sort_order="default", library_section_id="xtream_vod",
            title=title, type="movie", year=1999, unification_id=unif,
            added_at=added, page_offset=page_offset, is_in_allowed_categories=True,
            is_broken=False, is_adult=adult,
        )

    def _show(rk, title, unif, page_offset):
        return Media(
            rating_key=rk, server_id=build_server_id("a"),
            filter="all", sort_order="default", library_section_id="xtream_series",
            title=title, type="show", year=2008, unification_id=unif,
            page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
        )

    def _episode(rk, gpk, s_, e, page_offset):
        return Media(
            rating_key=rk, server_id=build_server_id("a"),
            filter="all", sort_order="default", library_section_id="xtream_series",
            title=f"S{s_:02d}E{e:02d}", type="episode",
            grandparent_rating_key=gpk, parent_index=s_, index=e,
            page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
        )

    async with db_factory() as s:
        s.add_all([
            XtreamAccount(
                id="a", label="Compte 1", base_url="http://a.example", port=80,
                username="u", password="p", is_active=True, created_at=0,
            ),
            _movie("vod_1.mp4", "Movie One", "tmdb://1", 100, page_offset=0),
            _movie("vod_2.mp4", "Movie Two", "tmdb://2", 300, page_offset=1),
            _movie("vod_x.mp4", "Naughty Film", "tmdb://9", 50, page_offset=2, adult=True),
            _show("series_1", "Breaking Bad", "tmdb://1396", page_offset=0),
            _episode("ep_1", "series_1", 1, 1, page_offset=1),
            _episode("ep_2", "series_1", 1, 2, page_offset=2),
        ])
        await s.commit()
    return db_factory


async def test_movies_endpoint_camelcase_and_pagination(api_client, seeded):
    r = await api_client.get(
        "/api/media/movies", params={"limit": 2, "offset": 0}, headers=API_HEADERS,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert set(body.keys()) == {"items", "total", "hasMore"}
    assert body["total"] == 3
    assert body["hasMore"] is True  # (offset 0 + limit 2) < 3
    assert len(body["items"]) == 2
    # camelCase alias survives the single-pass path
    assert "ratingKey" in body["items"][0]


async def test_adult_prefix_still_applied_through_singlepass(api_client, seeded):
    """The [XXX] prefix (MediaResponse.model_validator) must still be present in
    the single-pass output — it runs at model construction, before dump."""
    r = await api_client.get(
        "/api/media/movies", params={"limit": 50}, headers=API_HEADERS,
    )
    assert r.status_code == 200
    titles = {i["ratingKey"]: i["title"] for i in r.json()["items"]}
    assert titles["vod_x.mp4"].startswith("[XXX] ")
    assert titles["vod_1.mp4"] == "Movie One"  # non-adult untouched


async def test_movies_unified_versions_camelcase(api_client, seeded):
    r = await api_client.get("/api/media/movies/unified", headers=API_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"items", "total", "hasMore"}
    assert body["total"] == 3
    item = body["items"][0]
    assert "unificationId" in item
    assert "versionCount" in item
    assert isinstance(item["versions"], list)
    assert "ratingKey" in item["versions"][0]


async def test_episodes_unified_camelcase(api_client, seeded):
    r = await api_client.get(
        "/api/media/episodes/unified",
        params={"unification_id": "tmdb://1396"}, headers=API_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["unificationId"] == "tmdb://1396"
    assert body["seriesTitle"] == "Breaking Bad"
    assert body["total"] == 2
    assert {(i["season"], i["episode"]) for i in body["items"]} == {(1, 1), (1, 2)}
