"""API routes for trailer resolution (Lot A trailers):
`GET /api/media/trailer/resolve` and `GET /api/media/trailer/file/{cache_key}`
(`app/api/media.py`).

Both routes are mounted on `media.router` (`dependencies=_guard` at the
`include_router` mount site, `main.py`) so they inherit the same
`X-API-Key` guard as every other `/api/media/*` route — auth itself is
already covered by `app/api/route_audit.py`'s boot-time assertion + the
existing `tests/test_route_auth_assertion.py`; these tests focus on the
routes' own contract instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from app.config import settings
from app.db import database as db_module
from app.models.database import Media
from app.services import trailer_service
from app.services.tmdb_service import tmdb_service
from app.utils.server_id import build_server_id

API_KEY = "test-key-trailer"
API_HEADERS = {"X-API-Key": API_KEY}


@pytest_asyncio.fixture(autouse=True)
async def _wire_test_db(monkeypatch, db_factory):
    """Points `GET /api/media/*`'s `Depends(get_db)` at an isolated
    in-memory DB (mirrors `tests/test_media_keyset_pagination.py`)."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    return db_factory


@pytest_asyncio.fixture(autouse=True)
async def _clear_trailer_state():
    trailer_service._in_flight.clear()
    trailer_service._tmdb_repli_cache.clear()
    yield
    trailer_service._in_flight.clear()
    trailer_service._tmdb_repli_cache.clear()


def _stub_background_download(monkeypatch):
    """Same seam as `tests/test_trailer_service.py` — never actually runs a
    download, just proves one was scheduled."""
    calls = {"n": 0}

    def _fake(coro, *, name=None):
        calls["n"] += 1
        coro.close()

    monkeypatch.setattr(trailer_service, "create_background_task", _fake)
    return calls


# ─── GET /trailer/resolve ──────────────────────────────────────────────────


class TestResolveEndpoint:
    async def test_requires_api_key(self, api_client, trailer_dir):
        r = await api_client.get(
            "/api/media/trailer/resolve", params={"youtube_id": "dQw4w9WgXcQ"},
        )
        assert r.status_code == 401

    async def test_neither_mode_supplied_is_400(self, api_client, trailer_dir):
        r = await api_client.get("/api/media/trailer/resolve", headers=API_HEADERS)
        assert r.status_code == 400

    async def test_youtube_id_mode_none_when_no_cache(
        self, api_client, trailer_dir, monkeypatch,
    ):
        kicked_off = _stub_background_download(monkeypatch)
        r = await api_client.get(
            "/api/media/trailer/resolve",
            params={"youtube_id": "dQw4w9WgXcQ"}, headers=API_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "pending"
        assert body["url"] is None
        assert kicked_off["n"] == 1

    async def test_youtube_id_mode_ready_when_cached(self, api_client, trailer_dir):
        (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").write_bytes(b"bytes")
        r = await api_client.get(
            "/api/media/trailer/resolve",
            params={"youtube_id": "dQw4w9WgXcQ"}, headers=API_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["url"] == "/api/media/trailer/file/dQw4w9WgXcQ"

    async def test_youtube_id_mode_invalid_id_is_none(self, api_client, trailer_dir):
        r = await api_client.get(
            "/api/media/trailer/resolve",
            params={"youtube_id": "not-an-id"}, headers=API_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "none"

    async def test_rating_key_mode_reads_stored_column(
        self, api_client, trailer_dir, db_factory,
    ):
        async with db_factory() as s:
            s.add(Media(
                rating_key="vod_1.mp4", server_id=build_server_id("acc1"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title="T", type="movie", page_offset=0,
                youtube_trailer="dQw4w9WgXcQ",
            ))
            await s.commit()
        (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").write_bytes(b"bytes")

        r = await api_client.get(
            "/api/media/trailer/resolve",
            params={"rating_key": "vod_1.mp4", "server_id": build_server_id("acc1")},
            headers=API_HEADERS,
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ready", "url": "/api/media/trailer/file/dQw4w9WgXcQ"}

    async def test_rating_key_mode_unknown_media_is_none(
        self, api_client, trailer_dir,
    ):
        r = await api_client.get(
            "/api/media/trailer/resolve",
            params={"rating_key": "nope", "server_id": "xtream_a"},
            headers=API_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "none"

    async def test_rating_key_mode_falls_back_to_tmdb_repli(
        self, api_client, trailer_dir, db_factory, monkeypatch,
    ):
        async with db_factory() as s:
            s.add(Media(
                rating_key="vod_2.mp4", server_id=build_server_id("acc1"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title="T", type="movie", page_offset=0, tmdb_id="42",
            ))
            await s.commit()
        monkeypatch.setattr(settings, "TMDB_API_KEY", "test_key")

        async def _fake(tmdb_id, media_kind):
            return "abc12345678"
        monkeypatch.setattr(tmdb_service, "get_videos_trailer", _fake)
        _stub_background_download(monkeypatch)

        r = await api_client.get(
            "/api/media/trailer/resolve",
            params={"rating_key": "vod_2.mp4", "server_id": build_server_id("acc1")},
            headers=API_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "pending"


# ─── GET /trailer/file/{cache_key} ─────────────────────────────────────────


class TestFileEndpoint:
    async def test_requires_api_key(self, api_client, trailer_dir):
        (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").write_bytes(b"bytes")
        r = await api_client.get("/api/media/trailer/file/dQw4w9WgXcQ")
        assert r.status_code == 401

    async def test_serves_cached_file(self, api_client, trailer_dir):
        (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").write_bytes(b"fake-mp4-bytes")
        r = await api_client.get(
            "/api/media/trailer/file/dQw4w9WgXcQ", headers=API_HEADERS,
        )
        assert r.status_code == 200
        assert r.content == b"fake-mp4-bytes"
        assert r.headers["content-type"] == "video/mp4"
        assert r.headers["content-encoding"] == "identity"

    async def test_range_request_returns_206_partial_content(self, api_client, trailer_dir):
        body = b"0123456789" * 100  # 1000 bytes
        (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").write_bytes(body)

        r = await api_client.get(
            "/api/media/trailer/file/dQw4w9WgXcQ",
            headers={**API_HEADERS, "Range": "bytes=10-19"},
        )
        assert r.status_code == 206
        assert r.content == body[10:20]
        assert r.headers["content-range"] == f"bytes 10-19/{len(body)}"
        assert r.headers["content-encoding"] == "identity"

    async def test_missing_file_is_404(self, api_client, trailer_dir):
        r = await api_client.get(
            "/api/media/trailer/file/dQw4w9WgXcQ", headers=API_HEADERS,
        )
        assert r.status_code == 404

    async def test_malformed_cache_key_is_404_before_touching_filesystem(
        self, api_client, trailer_dir,
    ):
        r = await api_client.get(
            "/api/media/trailer/file/..%2F..%2Fetc%2Fpasswd", headers=API_HEADERS,
        )
        assert r.status_code == 404

    async def test_disabled_feature_is_404(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "TRAILER_CACHE_DIR", "")
        r = await api_client.get(
            "/api/media/trailer/file/dQw4w9WgXcQ", headers=API_HEADERS,
        )
        assert r.status_code == 404
