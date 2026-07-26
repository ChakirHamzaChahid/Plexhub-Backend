"""Sync-side capture of `Media.youtube_trailer` (Lot A trailers).

VOD: `map_vod_to_media` reads `info.youtube_trailer` directly (pure unit
tests — the field lives in the same `vod_info["info"]` dict every other
detail field is already extracted from).

Series: `map_series_to_media` never sees `series_info` (only the listing
`dto`) — the trailer key is captured by `fetch_series_episodes` (a closure
inside `sync_account`) and applied as a SEPARATE `UPDATE`. That closure can
only be exercised through a real `sync_account()` run (integration tests,
mirroring `tests/test_sync_worker.py::TestEpisodeSyncFreshnessIntegration`).
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.database import Media, XtreamAccount
from app.services.xtream_service import xtream_service
from app.workers import sync_worker as sync_worker_module
from app.workers.sync_worker import map_vod_to_media


# ─── VOD capture (pure) ──────────────────────────────────────────────────


def _vod_dto(stream_id: int = 1) -> dict:
    return {
        "stream_id": stream_id, "name": "Test Movie", "category_id": "1",
        "container_extension": "mp4", "rating": "7.0", "added": "0",
    }


class TestVodTrailerCapture:
    def test_youtube_trailer_extracted_from_vod_info(self):
        vod_info = {"info": {"youtube_trailer": "dQw4w9WgXcQ"}, "movie_data": {}}
        row = map_vod_to_media(_vod_dto(), "acc1", 0, vod_info)
        assert row["youtube_trailer"] == "dQw4w9WgXcQ"

    def test_no_vod_info_leaves_trailer_none(self):
        row = map_vod_to_media(_vod_dto(), "acc1", 0, None)
        assert row["youtube_trailer"] is None

    def test_missing_youtube_trailer_key_leaves_none(self):
        row = map_vod_to_media(_vod_dto(), "acc1", 0, {"info": {}, "movie_data": {}})
        assert row["youtube_trailer"] is None

    def test_blank_youtube_trailer_coerced_to_none(self):
        """An empty-string provider value must never be persisted as a
        truthy-looking trailer key (`info.get(...) or None`)."""
        vod_info = {"info": {"youtube_trailer": ""}, "movie_data": {}}
        row = map_vod_to_media(_vod_dto(), "acc1", 0, vod_info)
        assert row["youtube_trailer"] is None

    def test_youtube_trailer_survives_alongside_other_info_fields(self):
        """Non-regression: adding the trailer extraction must not disturb
        the other `info`-derived fields already covered elsewhere."""
        vod_info = {
            "info": {
                "youtube_trailer": "dQw4w9WgXcQ", "plot": "A plot.",
                "genre": "Action", "duration_secs": "120",
            },
            "movie_data": {},
        }
        row = map_vod_to_media(_vod_dto(), "acc1", 0, vod_info)
        assert row["youtube_trailer"] == "dQw4w9WgXcQ"
        assert row["summary"] == "A plot."
        assert row["genres"] == "Action"
        assert row["duration"] == 120_000


# ─── Series capture (integration, via sync_account) ─────────────────────


def _account(account_id: str) -> XtreamAccount:
    return XtreamAccount(
        id=account_id, label="Test", base_url="http://x.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


_SERIES_DTO = {
    "series_id": 200, "name": "Show A", "category_id": "1",
    "cover": None, "plot": "plot", "genre": "Drama", "rating": "8.0",
    "backdrop_path": None, "episode_run_time": "30", "last_modified": "1",
}


def _ep_dto(ep_id: int, ep_num: int) -> dict:
    return {
        "id": str(ep_id), "episode_num": ep_num, "title": f"Episode {ep_num}",
        "container_extension": "mp4", "info": {},
    }


async def _series_trailer(factory) -> str | None:
    async with factory() as s:
        row = (await s.execute(
            select(Media.youtube_trailer).where(Media.rating_key == "series_200")
        )).scalar_one_or_none()
    return row


class TestSeriesTrailerCapture:
    """Reuses the `wired_sync_db`-equivalent wiring inline (own fixture would
    collide with `tests/test_sync_worker.py`'s account-id-per-test
    convention — `_account_locks` is process-global, so each test here also
    uses its own account_id, same rationale)."""

    async def test_youtube_trailer_captured_from_series_info(self, db_engine, monkeypatch):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(sync_worker_module, "async_session_factory", factory)

        async def _empty(*a, **kw):
            return []

        monkeypatch.setattr(xtream_service, "get_vod_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_series_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_live_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_vod_streams", _empty)
        monkeypatch.setattr(xtream_service, "get_live_streams", _empty)

        account_id = "trailer_series_acc"
        async with factory() as s:
            s.add(_account(account_id))
            await s.commit()

        async def _get_series(*a, **kw):
            return [dict(_SERIES_DTO)]

        async def _get_series_info(*a, **kw):
            return {
                "info": {"youtube_trailer": "dQw4w9WgXcQ"},
                "episodes": {"1": [_ep_dto(1, 1)]},
            }

        monkeypatch.setattr(xtream_service, "get_series", _get_series)
        monkeypatch.setattr(xtream_service, "get_series_info", _get_series_info)

        await sync_worker_module.sync_account(account_id)

        assert await _series_trailer(factory) == "dQw4w9WgXcQ"

    async def test_missing_series_trailer_never_clobbers_a_previous_value(
        self, db_engine, monkeypatch,
    ):
        """A run where the provider stops reporting a trailer (or a transient
        fetch failure) must NEVER erase a previously-captured one — only a
        non-empty value is ever applied (see `series_trailer_updates` in
        `sync_worker.sync_account`)."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(sync_worker_module, "async_session_factory", factory)

        async def _empty(*a, **kw):
            return []

        monkeypatch.setattr(xtream_service, "get_vod_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_series_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_live_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_vod_streams", _empty)
        monkeypatch.setattr(xtream_service, "get_live_streams", _empty)

        account_id = "trailer_series_keep_acc"
        async with factory() as s:
            s.add(_account(account_id))
            await s.commit()

        async def _get_series(*a, **kw):
            return [dict(_SERIES_DTO)]

        calls = {"n": 0}

        async def _get_series_info(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "info": {"youtube_trailer": "dQw4w9WgXcQ"},
                    "episodes": {"1": [_ep_dto(1, 1)]},
                }
            # Second run: provider genuinely stops sending a trailer key.
            return {"info": {}, "episodes": {"1": [_ep_dto(1, 1)]}}

        monkeypatch.setattr(xtream_service, "get_series", _get_series)
        monkeypatch.setattr(xtream_service, "get_series_info", _get_series_info)

        await sync_worker_module.sync_account(account_id)
        assert await _series_trailer(factory) == "dQw4w9WgXcQ"

        await sync_worker_module.sync_account(account_id)
        assert await _series_trailer(factory) == "dQw4w9WgXcQ", (
            "a run without a trailer key must not clobber the previously captured one"
        )

    async def test_series_info_fetch_failure_does_not_crash_and_no_trailer_set(
        self, db_engine, monkeypatch,
    ):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(sync_worker_module, "async_session_factory", factory)

        async def _empty(*a, **kw):
            return []

        monkeypatch.setattr(xtream_service, "get_vod_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_series_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_live_categories", _empty)
        monkeypatch.setattr(xtream_service, "get_vod_streams", _empty)
        monkeypatch.setattr(xtream_service, "get_live_streams", _empty)

        account_id = "trailer_series_fail_acc"
        async with factory() as s:
            s.add(_account(account_id))
            await s.commit()

        async def _get_series(*a, **kw):
            return [dict(_SERIES_DTO)]

        async def _get_series_info(*a, **kw):
            raise RuntimeError("provider unreachable")

        monkeypatch.setattr(xtream_service, "get_series", _get_series)
        monkeypatch.setattr(xtream_service, "get_series_info", _get_series_info)

        await sync_worker_module.sync_account(account_id)  # must not raise

        assert await _series_trailer(factory) is None
