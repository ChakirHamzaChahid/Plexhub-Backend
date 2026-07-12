"""CR-T03: drive the actual `sync_worker` orchestration end-to-end.

Before this file, only the pure helpers of `app/workers/sync_worker.py`
(duration parsing, dto-hash, cleanup functions, `upsert_media_batch`, ...) were
covered. `sync_account()` and `run_all_accounts()` themselves -- the real
upsert/cleanup flow, the per-account lock, and job-status recording -- were
never actually invoked by any test.

Hermetic: `xtream_service`'s network methods are monkeypatched to small fixed
catalogs (no network I/O); the DB is an in-memory SQLite engine wired onto
`sync_worker.async_session_factory`, following the `wired_sync_db` pattern
already used in `tests/test_sync_worker.py`.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.database import LiveChannel, Media, XtreamAccount
from app.services.xtream_service import xtream_service
from app.workers import sync_worker as sync_worker_module

# pytest-asyncio runs in auto mode (pyproject.toml) -- async tests need no mark.


def _account(account_id: str) -> XtreamAccount:
    return XtreamAccount(
        id=account_id, label="Test", base_url="http://x.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


_SERIES_DTO = {
    "series_id": 100, "name": "Show A", "category_id": "1",
    "cover": None, "plot": "plot", "genre": "Drama", "rating": "8.0",
    "backdrop_path": None, "episode_run_time": "30", "last_modified": "1",
}


def _ep_dto(ep_id: int, ep_num: int) -> dict:
    return {
        "id": str(ep_id), "episode_num": ep_num, "title": f"Episode {ep_num}",
        "container_extension": "mp4", "info": {},
    }


_VOD_DTO_1 = {
    "stream_id": 501, "name": "Movie One", "added": "1000",
    "category_id": "5", "container_extension": "mp4", "rating": "7.5",
    "stream_icon": "http://img/1.jpg",
}
_VOD_DTO_2 = {
    "stream_id": 502, "name": "Movie Two", "added": "2000",
    "category_id": "5", "container_extension": "mp4", "rating": "8.0",
    "stream_icon": "http://img/2.jpg",
}

_LIVE_DTO_1 = {
    "stream_id": 9001, "name": "Channel One", "category_id": "1",
    "container_extension": "ts", "added": "100",
}


@pytest_asyncio.fixture
async def wired_sync_db(db_engine, monkeypatch):
    """Wire `sync_account`/`run_all_accounts` onto an in-memory test DB.

    All `xtream_service` network methods default to empty catalogs (hermetic,
    no network); individual tests override whichever ones they need to drive
    real content through the orchestration. `category_filter_mode` defaults
    to "all" so no `XtreamCategory` row is required.
    """
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sync_worker_module, "async_session_factory", factory)

    async def _empty_list(*args, **kwargs):
        return []

    async def _empty_dict(*args, **kwargs):
        return {}

    monkeypatch.setattr(xtream_service, "get_vod_categories", _empty_list)
    monkeypatch.setattr(xtream_service, "get_series_categories", _empty_list)
    monkeypatch.setattr(xtream_service, "get_live_categories", _empty_list)
    monkeypatch.setattr(xtream_service, "get_vod_streams", _empty_list)
    monkeypatch.setattr(xtream_service, "get_vod_info", _empty_dict)
    monkeypatch.setattr(xtream_service, "get_series", _empty_list)
    monkeypatch.setattr(xtream_service, "get_series_info", _empty_dict)
    monkeypatch.setattr(xtream_service, "get_live_streams", _empty_list)

    return factory


# ─── sync_account: full VOD + Series + Episodes + Live upsert ───────────


class TestSyncAccountEndToEnd:
    """Drives `sync_account()` through its real upsert/cleanup flow (movies,
    shows, episodes, live channels) against small fixed catalogs."""

    async def test_vod_series_episodes_live_are_upserted(self, wired_sync_db, monkeypatch):
        account_id = "cr_t03_e2e"
        async with wired_sync_db() as s:
            s.add(_account(account_id))
            await s.commit()

        async def _get_vod_streams(*a, **kw):
            return [dict(_VOD_DTO_1), dict(_VOD_DTO_2)]

        async def _get_vod_info(*a, **kw):
            return {"info": {"plot": "A plot"}}

        async def _get_series(*a, **kw):
            return [dict(_SERIES_DTO)]

        async def _get_series_info(*a, **kw):
            return {"episodes": {"1": [_ep_dto(1, 1), _ep_dto(2, 2)]}}

        async def _get_live_streams(*a, **kw):
            return [dict(_LIVE_DTO_1)]

        monkeypatch.setattr(xtream_service, "get_vod_streams", _get_vod_streams)
        monkeypatch.setattr(xtream_service, "get_vod_info", _get_vod_info)
        monkeypatch.setattr(xtream_service, "get_series", _get_series)
        monkeypatch.setattr(xtream_service, "get_series_info", _get_series_info)
        monkeypatch.setattr(xtream_service, "get_live_streams", _get_live_streams)

        job_id = await sync_worker_module.sync_account(account_id)

        async with wired_sync_db() as s:
            movies = (await s.execute(select(Media).where(Media.type == "movie"))).scalars().all()
            shows = (await s.execute(select(Media).where(Media.type == "show"))).scalars().all()
            episodes = (await s.execute(select(Media).where(Media.type == "episode"))).scalars().all()
            live = (await s.execute(select(LiveChannel))).scalars().all()

        assert {m.title for m in movies} == {"Movie One", "Movie Two"}
        assert len(shows) == 1 and shows[0].title == "Show A"
        assert {e.rating_key for e in episodes} == {"ep_1.mp4", "ep_2.mp4"}
        assert len(live) == 1 and live[0].name == "Channel One"

        job = sync_worker_module.get_sync_job(job_id)
        assert job is not None
        assert job["status"] == "completed"
        assert job["progress"]["total"] > 0

    async def test_second_run_on_unchanged_catalog_is_a_noop(self, wired_sync_db, monkeypatch):
        """A second sync with a byte-identical catalog must skip the expensive
        per-item `get_vod_info` fetch (dto_hash short-circuit) and must not
        rewrite the already-upserted row."""
        account_id = "cr_t03_noop"
        async with wired_sync_db() as s:
            s.add(_account(account_id))
            await s.commit()

        vod_info_calls = {"n": 0}

        async def _get_vod_streams(*a, **kw):
            return [dict(_VOD_DTO_1)]

        async def _get_vod_info(*a, **kw):
            vod_info_calls["n"] += 1
            return {"info": {"plot": "A plot"}}

        monkeypatch.setattr(xtream_service, "get_vod_streams", _get_vod_streams)
        monkeypatch.setattr(xtream_service, "get_vod_info", _get_vod_info)

        await sync_worker_module.sync_account(account_id)
        assert vod_info_calls["n"] == 1

        async with wired_sync_db() as s:
            before = (await s.execute(
                select(Media.rating_key, Media.updated_at, Media.content_hash)
                .where(Media.type == "movie")
            )).all()

        await sync_worker_module.sync_account(account_id)

        assert vod_info_calls["n"] == 1, (
            "an unchanged catalog must skip the per-item get_vod_info call "
            "(dto_hash short-circuit) -- the API must not be re-hit"
        )

        async with wired_sync_db() as s:
            after = (await s.execute(
                select(Media.rating_key, Media.updated_at, Media.content_hash)
                .where(Media.type == "movie")
            )).all()

        assert before == after, "a genuine no-op sync must not rewrite an unchanged row"

    async def test_delisted_vod_is_removed_on_resync(self, wired_sync_db, monkeypatch):
        """A movie dropped from the provider's VOD listing must be cleaned up
        (differential_cleanup), while a still-listed one survives."""
        account_id = "cr_t03_delist"
        async with wired_sync_db() as s:
            s.add(_account(account_id))
            await s.commit()

        calls = {"n": 0}

        async def _get_vod_streams(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return [dict(_VOD_DTO_1), dict(_VOD_DTO_2)]
            return [dict(_VOD_DTO_1)]  # "Movie Two" delisted this sync

        async def _get_vod_info(*a, **kw):
            return {"info": {}}

        monkeypatch.setattr(xtream_service, "get_vod_streams", _get_vod_streams)
        monkeypatch.setattr(xtream_service, "get_vod_info", _get_vod_info)

        await sync_worker_module.sync_account(account_id)
        async with wired_sync_db() as s:
            titles = set((await s.execute(
                select(Media.title).where(Media.type == "movie")
            )).scalars().all())
        assert titles == {"Movie One", "Movie Two"}

        await sync_worker_module.sync_account(account_id)
        async with wired_sync_db() as s:
            titles = set((await s.execute(
                select(Media.title).where(Media.type == "movie")
            )).scalars().all())
        assert titles == {"Movie One"}, "the delisted movie must be removed, the listed one kept"

    async def test_hard_failure_records_job_status_failed(self, wired_sync_db, monkeypatch):
        """A genuine unhandled exception in the sync flow must be recorded as
        a `failed` job, not silently swallowed."""
        account_id = "cr_t03_fail"
        async with wired_sync_db() as s:
            s.add(_account(account_id))
            await s.commit()

        async def _boom(*a, **kw):
            raise RuntimeError("commit exploded")

        # `commit_with_retry` is called (unguarded by any try/except) right
        # after `_refresh_categories` fetches categories -- the first genuine
        # commit point in `sync_account`'s flow.
        monkeypatch.setattr(sync_worker_module, "commit_with_retry", _boom)

        job_id = await sync_worker_module.sync_account(account_id)

        job = sync_worker_module.get_sync_job(job_id)
        assert job is not None
        assert job["status"] == "failed"
        assert "commit exploded" in job["progress"]["error"]


# ─── Per-account lock ────────────────────────────────────────────────────


class TestPerAccountLockGuardsSyncAccount:
    async def test_sync_account_skips_when_lock_already_held(self, wired_sync_db):
        """A concurrent sync attempt on the SAME account must be rejected
        immediately (never touching the DB or recording a job), rather than
        queueing or racing with the in-progress run."""
        account_id = "cr_t03_lock"
        async with wired_sync_db() as s:
            s.add(_account(account_id))
            await s.commit()

        jobs_before = [
            j for j in sync_worker_module.get_all_sync_jobs() if account_id in j["job_id"]
        ]
        assert jobs_before == []

        lock = sync_worker_module._get_account_lock(account_id)
        async with lock:
            result = await sync_worker_module.sync_account(account_id)

        assert result == f"sync_{account_id}_skipped"
        # The skip path returns before ever calling `_record_sync_job` --
        # no job should have been recorded under this account.
        jobs_after = [
            j for j in sync_worker_module.get_all_sync_jobs() if account_id in j["job_id"]
        ]
        assert jobs_after == []

    async def test_lock_is_released_after_a_completed_sync(self, wired_sync_db):
        """Once a sync finishes, the lock must be free again -- a following
        sync on the same account must run for real, not be skipped."""
        account_id = "cr_t03_lock_release"
        async with wired_sync_db() as s:
            s.add(_account(account_id))
            await s.commit()

        first = await sync_worker_module.sync_account(account_id)
        assert not first.endswith("_skipped")

        second = await sync_worker_module.sync_account(account_id)
        assert not second.endswith("_skipped")
        assert sync_worker_module.get_sync_job(second)["status"] == "completed"


# ─── run_all_accounts ────────────────────────────────────────────────────


class TestRunAllAccounts:
    async def test_syncs_only_active_accounts(self, wired_sync_db, monkeypatch):
        async with wired_sync_db() as s:
            inactive = _account("cr_t03_run_inactive")
            inactive.is_active = False
            s.add_all([
                _account("cr_t03_run_active_1"),
                _account("cr_t03_run_active_2"),
                inactive,
            ])
            await s.commit()

        synced_ids: list[str] = []

        async def _spy_sync_account(account_id):
            synced_ids.append(account_id)
            return f"sync_{account_id}_ok"

        monkeypatch.setattr(sync_worker_module, "sync_account", _spy_sync_account)

        await sync_worker_module.run_all_accounts()

        assert set(synced_ids) == {"cr_t03_run_active_1", "cr_t03_run_active_2"}

    async def test_drives_real_sync_account_end_to_end(self, wired_sync_db, monkeypatch):
        """Unlike the spy-based test above, this exercises the REAL
        `sync_account` through `run_all_accounts` for one active account, to
        prove the two are actually wired together (not just individually
        correct)."""
        account_id = "cr_t03_run_real"
        async with wired_sync_db() as s:
            s.add(_account(account_id))
            await s.commit()

        async def _get_vod_streams(*a, **kw):
            return [dict(_VOD_DTO_1)]

        async def _get_vod_info(*a, **kw):
            return {"info": {}}

        monkeypatch.setattr(xtream_service, "get_vod_streams", _get_vod_streams)
        monkeypatch.setattr(xtream_service, "get_vod_info", _get_vod_info)

        await sync_worker_module.run_all_accounts()

        async with wired_sync_db() as s:
            titles = set((await s.execute(
                select(Media.title).where(Media.type == "movie")
            )).scalars().all())
        assert titles == {"Movie One"}
