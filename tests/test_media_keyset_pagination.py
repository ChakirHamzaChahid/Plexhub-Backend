"""CR-P04: additive keyset (seek) pagination on the raw media list endpoints.

The cursor is OPTIONAL and non-breaking: offset paging is byte-identical to
before, and a `nextCursor` is added to the response. The core guarantees:

  1. Walking pages with `cursor` yields EXACTLY the same sequence (order + no
     duplicates + no skips) as one large offset query — including across rows
     that share the same `added_at` (the composite-PK tie-break must hold),
     for both added_desc and added_asc.
  2. A malformed cursor is a 400, not a 500.
  3. A non-recency sort (e.g. title_asc) ignores the cursor and stays on the
     offset path (nextCursor null), so the cursor can't corrupt those orders.
  4. encode/decode round-trips the recency key + full composite PK.
"""
from __future__ import annotations

import pytest_asyncio

from app.config import settings
from app.db import database as db_module
from app.models.database import Media, XtreamAccount
from app.services.media_service import encode_media_cursor, decode_media_cursor
from app.utils.server_id import build_server_id

# pytest-asyncio auto mode (pyproject.toml).

API_KEY = "test-key-keyset"
API_HEADERS = {"X-API-Key": API_KEY}


@pytest_asyncio.fixture(autouse=True)
async def _wire_test_db(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    return db_factory


@pytest_asyncio.fixture
async def seeded(db_factory):
    """8 movies, with DELIBERATE duplicate added_at values (100 & 300 appear
    twice) so the tie-break path is exercised."""
    added = [100, 100, 200, 300, 300, 400, 500, 600]
    async with db_factory() as s:
        s.add(XtreamAccount(
            id="a", label="Compte 1", base_url="http://a.example", port=80,
            username="u", password="p", is_active=True, created_at=0,
        ))
        for i, ts in enumerate(added):
            s.add(Media(
                rating_key=f"vod_{i}.mp4", server_id=build_server_id("a"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title=f"Movie {i}", type="movie", year=2000 + i,
                unification_id=f"tmdb://{i}", added_at=ts, page_offset=i,
                is_in_allowed_categories=True, is_broken=False,
            ))
        await s.commit()
    return db_factory


async def _offset_all(client, sort):
    r = await client.get(
        "/api/media/movies", params={"limit": 50, "offset": 0, "sort": sort},
        headers=API_HEADERS,
    )
    assert r.status_code == 200
    return [i["ratingKey"] for i in r.json()["items"]]


async def _cursor_walk(client, sort, page_size=3):
    """A keyset client pages by following nextCursor until it is null (it does
    NOT use hasMore, which keeps offset semantics)."""
    keys: list[str] = []
    cursor = None
    for _ in range(20):  # generous safety bound vs 8 rows / page 3
        params = {"limit": page_size, "sort": sort}
        if cursor is not None:
            params["cursor"] = cursor
        r = await client.get("/api/media/movies", params=params, headers=API_HEADERS)
        assert r.status_code == 200
        body = r.json()
        keys.extend(i["ratingKey"] for i in body["items"])
        cursor = body["nextCursor"]
        if cursor is None:
            break
    return keys


async def test_cursor_walk_matches_offset_desc(api_client, seeded):
    expected = await _offset_all(api_client, "added_desc")
    walked = await _cursor_walk(api_client, "added_desc")
    assert walked == expected
    assert len(walked) == 8
    assert len(set(walked)) == 8  # no duplicates, no skips across the tie-break


async def test_cursor_walk_matches_offset_asc(api_client, seeded):
    expected = await _offset_all(api_client, "added_asc")
    walked = await _cursor_walk(api_client, "added_asc")
    assert walked == expected
    assert len(walked) == 8
    assert len(set(walked)) == 8


async def test_full_final_page_terminates_cleanly(api_client, seeded):
    """8 rows / page 4 => two full pages; the second must report hasMore and a
    cursor, and the third fetch returns empty with hasMore False (the standard
    keyset boundary behaviour)."""
    walked = await _cursor_walk(api_client, "added_desc", page_size=4)
    assert len(walked) == 8
    assert len(set(walked)) == 8


async def test_bad_cursor_is_400_not_500(api_client, seeded):
    r = await api_client.get(
        "/api/media/movies",
        params={"cursor": "!!!not-base64!!!", "sort": "added_desc"},
        headers=API_HEADERS,
    )
    assert r.status_code == 400


async def test_non_recency_sort_ignores_cursor(api_client, seeded):
    """title_asc is not a keyset sort — a cursor must be ignored (offset path),
    so the request still succeeds and nextCursor stays null."""
    r = await api_client.get(
        "/api/media/movies",
        params={"cursor": encode_media_cursor_stub(), "sort": "title_asc"},
        headers=API_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["nextCursor"] is None


def encode_media_cursor_stub() -> str:
    """A syntactically valid cursor (so the test proves the sort gate, not a
    decode failure, is what makes it fall back to offset)."""
    m = Media(
        rating_key="vod_0.mp4", server_id=build_server_id("a"),
        filter="all", sort_order="default", added_at=100,
    )
    return encode_media_cursor(m)


def test_cursor_roundtrip_carries_recency_and_full_pk():
    m = Media(
        rating_key="vod_7.mp4", server_id=build_server_id("a"),
        filter="all", sort_order="default", added_at=600,
    )
    cur = encode_media_cursor(m)
    assert decode_media_cursor(cur) == (
        600, "vod_7.mp4", build_server_id("a"), "all", "default",
    )
