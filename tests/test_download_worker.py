"""`app.workers.download_worker` — review remediation regression tests
(PH-DL review, `docs/20-impl-media-download.md` §6):

  1. BLOQUANT — the drain loop (`run_drain_loop`) must survive a non-lock
     exception raised mid-tick, not die permanently.
  3. Sécu Moyen DL-02 — the disk-space préflight is actually wired into
     `_run_job`, before the transfer starts.
  4. Majeur — `cancel_check` is throttled (~1 SELECT/s), not called once
     per chunk.
  5. Majeur — a transient failure releases the concurrency semaphore
     BEFORE the exponential back-off sleep (no head-of-line blocking), and
     the job shows `queued` (not `running`) for the whole back-off window.

All HTTP is mocked via `respx` (`xtream_mock` fixture). DB is the in-memory
`db_engine` fixture from `tests/conftest.py`. No real filesystem writes
outside `download_dir` (tmp_path-backed).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import DownloadJob, XtreamAccount
from app.services import download_service
from app.services.download_service import DownloadResult
from app.utils.server_id import build_server_id
from app.utils.time import now_ms
from app.workers import download_worker
from app.workers.download_worker import _run_job

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

ACCOUNT_ID = "acc1"
SERVER_ID = build_server_id(ACCOUNT_ID)


def _account() -> XtreamAccount:
    return XtreamAccount(
        id=ACCOUNT_ID, label="Compte", base_url="http://provider.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


def _job(job_id: str, *, rating_key: str, state: str = "queued") -> DownloadJob:
    return DownloadJob(
        id=job_id, batch_id=None, server_id=SERVER_ID, rating_key=rating_key,
        media_type="movie", unification_id=None, title=f"Film {job_id}",
        season=None, episode=None,
        dest_path=f"Movies/{job_id}/{job_id}.mkv",
        state=state, bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )


def _url(n: int) -> str:
    return f"http://provider.example:80/movie/u/p/{n}.mkv"


async def _seeded_factory(db_engine, *, jobs: list[DownloadJob]):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(_account())
        s.add_all(jobs)
        await s.commit()
    return factory


# ─── Fix #1 (BLOQUANT): drain loop survives a non-lock exception ───────────


class TestDrainLoopResilience:
    async def test_survives_non_lock_exception_and_keeps_polling(
        self, db_engine, monkeypatch, download_dir,
    ):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

        call_count = {"n": 0}
        reached_third_tick = asyncio.Event()

        async def _flaky_fetch(session_factory, *, limit, exclude_ids):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # A non-lock OperationalError analogue (WAL checkpoint / nightly
                # backup / "disk image malformed"...) — must NOT kill the loop.
                raise RuntimeError("disk image malformed (simulated)")
            if call_count["n"] >= 3:
                reached_third_tick.set()
            return []

        monkeypatch.setattr(download_worker, "_fetch_queued", _flaky_fetch)

        # Fast-forward the poll interval so the test doesn't wait real
        # seconds — `asyncio.sleep(0)` is a genuine yield-point (unlike a
        # no-op stub), so the loop still cooperates with the event loop.
        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(download_worker.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(settings, "DOWNLOAD_CONCURRENCY", 1)

        task = asyncio.create_task(download_worker.run_drain_loop(factory))
        try:
            await asyncio.wait_for(reached_third_tick.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert call_count["n"] >= 3, (
            "the drain loop must keep polling (and calling _fetch_queued again) "
            "after a non-'database is locked' exception, not die permanently"
        )

    async def test_cancelled_error_still_propagates_and_stops_the_loop(
        self, db_engine, monkeypatch, download_dir,
    ):
        """Sanity guard: the new per-tick try/except must NOT swallow
        shutdown — `asyncio.CancelledError` still stops the loop cleanly."""
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(download_worker.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(settings, "DOWNLOAD_CONCURRENCY", 1)

        task = asyncio.create_task(download_worker.run_drain_loop(factory))
        await real_sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()


# ─── Fix #3 (Sécu Moyen DL-02): disk-space préflight wired into _run_job ───


class TestDiskPreflightWiredIntoWorker:
    async def test_insufficient_disk_space_fails_job_without_writing_any_bytes(
        self, db_engine, monkeypatch, download_dir, xtream_mock,
    ):
        from types import SimpleNamespace

        factory = await _seeded_factory(db_engine, jobs=[_job("j1", rating_key="vod_1.mkv")])

        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 2048)
        monkeypatch.setattr(
            download_service.shutil, "disk_usage",
            lambda path: SimpleNamespace(total=0, used=0, free=10 * 1024 * 1024),  # 10 MiB
        )
        # If the préflight regresses and the transfer starts anyway, this
        # would be fetched — assert it never is.
        route = xtream_mock.get(_url(1)).mock(
            return_value=httpx.Response(200, content=b"should never be fetched")
        )

        await _run_job(factory, "j1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "j1")
        assert job.state == "failed"
        assert job.error is not None and "insufficient free disk" in job.error
        assert route.call_count == 0, "the transfer must never start when the préflight fails"
        assert list(download_dir.rglob("*")) == [], "no bytes/files must be written on disk"

    async def test_sufficient_disk_space_lets_the_transfer_proceed(
        self, db_engine, monkeypatch, download_dir, xtream_mock,
    ):
        from types import SimpleNamespace

        factory = await _seeded_factory(db_engine, jobs=[_job("j2", rating_key="vod_2.mkv")])

        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 100)
        monkeypatch.setattr(
            download_service.shutil, "disk_usage",
            lambda path: SimpleNamespace(total=0, used=0, free=5000 * 1024 * 1024),
        )
        xtream_mock.get(_url(2)).mock(
            return_value=httpx.Response(200, content=b"ok", headers={"Content-Length": "2"})
        )

        await _run_job(factory, "j2", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "j2")
        assert job.state == "completed"


# ─── Fix #4 (Majeur): cancel_check throttled, not called once per chunk ───


class TestCancelCheckThrottled:
    async def test_cancel_check_calls_bounded_across_many_simulated_chunks(
        self, db_engine, monkeypatch, download_dir,
    ):
        factory = await _seeded_factory(db_engine, jobs=[_job("j3", rating_key="vod_3.mkv")])
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)  # préflight disabled, unrelated to this test

        is_canceled_calls = {"n": 0}

        async def _spy_is_canceled(session_factory, job_id):
            is_canceled_calls["n"] += 1
            return False

        monkeypatch.setattr(download_worker, "_is_canceled", _spy_is_canceled)

        n_simulated_chunks = 50

        async def _fake_download_to_disk(url, dest, *, on_progress=None, cancel_check=None, **_kw):
            # Simulate `n_simulated_chunks` chunk writes in a tight loop (no
            # real I/O delay) — this is exactly the regime that used to open
            # a fresh DB session + run a SELECT on every single call.
            for i in range(n_simulated_chunks):
                if on_progress is not None:
                    await on_progress(i + 1, n_simulated_chunks)
                if cancel_check is not None:
                    assert not await cancel_check()
            return DownloadResult(
                bytes_downloaded=n_simulated_chunks, bytes_total=n_simulated_chunks,
                already_present=False, resumed=False,
            )

        monkeypatch.setattr(download_service, "download_to_disk", _fake_download_to_disk)

        await _run_job(factory, "j3", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "j3")
        assert job.state == "completed"
        assert 1 <= is_canceled_calls["n"] < n_simulated_chunks, (
            f"cancel-check must be throttled (~1/s), not called once per chunk "
            f"(got {is_canceled_calls['n']} calls for {n_simulated_chunks} chunks)"
        )


# ─── Fix #5 (Majeur): HOL blocking — semaphore released before back-off ───


class TestTransientBackoffDoesNotHoldTheSemaphore:
    async def test_handle_transient_is_invoked_after_the_semaphore_is_released(
        self, db_engine, monkeypatch, download_dir, xtream_mock,
    ):
        factory = await _seeded_factory(db_engine, jobs=[_job("j4", rating_key="vod_4.mkv")])
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        sem = asyncio.Semaphore(1)
        handle_transient_calls: list[bool] = []

        async def _fake_handle_transient(session_factory, job_id, message):
            # The whole point of the fix: by the time this runs, `_run_job`
            # must already have exited `async with sem:`.
            handle_transient_calls.append(not sem.locked())

        monkeypatch.setattr(download_worker, "_handle_transient", _fake_handle_transient)
        xtream_mock.get(_url(4)).mock(return_value=httpx.Response(503))

        await _run_job(factory, "j4", sem)

        assert handle_transient_calls == [True], (
            "the semaphore must be released BEFORE _handle_transient (back-off) runs"
        )
        assert not sem.locked()

    async def test_second_job_completes_while_first_is_backing_off(
        self, db_engine, monkeypatch, download_dir, xtream_mock,
    ):
        """End-to-end proof at DOWNLOAD_CONCURRENCY=1: job A fails transient
        and enters back-off; job B must be able to fully complete via the
        SAME semaphore while job A is still "in flight" (sleeping) — this
        would deadlock/timeout under the pre-fix behaviour (semaphore held
        for the whole `min(2**attempts, 30)`s back-off)."""
        factory = await _seeded_factory(
            db_engine,
            jobs=[_job("jobA", rating_key="vod_5.mkv"), _job("jobB", rating_key="vod_6.mkv")],
        )
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        sem = asyncio.Semaphore(1)
        entered_backoff = asyncio.Event()
        resume_backoff = asyncio.Event()

        async def _fake_handle_transient(session_factory, job_id, message):
            entered_backoff.set()
            await resume_backoff.wait()  # simulate the back-off sleep

        monkeypatch.setattr(download_worker, "_handle_transient", _fake_handle_transient)

        xtream_mock.get(_url(5)).mock(return_value=httpx.Response(503))  # transient -> back-off
        xtream_mock.get(_url(6)).mock(
            return_value=httpx.Response(200, content=b"ok", headers={"Content-Length": "2"})
        )

        task_a = asyncio.create_task(_run_job(factory, "jobA", sem))
        await asyncio.wait_for(entered_backoff.wait(), timeout=2)

        # Job B must be able to run to completion RIGHT NOW, concurrently,
        # even though job A's (stubbed) back-off is still pending.
        await asyncio.wait_for(_run_job(factory, "jobB", sem), timeout=2)

        async with factory() as s:
            job_b = await s.get(DownloadJob, "jobB")
        assert job_b.state == "completed", "job B must not be head-of-line-blocked by job A's back-off"

        resume_backoff.set()
        await asyncio.wait_for(task_a, timeout=2)


class TestHandleTransientRequeuesBeforeSleeping:
    async def test_state_is_queued_not_running_during_the_backoff_wait(
        self, db_engine, monkeypatch,
    ):
        """Majeur fix requirement: the job must show `queued` (not
        `running`) for the ENTIRE back-off window, not just after it."""
        factory = await _seeded_factory(db_engine, jobs=[_job("j5", rating_key="vod_7.mkv", state="running")])

        real_sleep = asyncio.sleep
        observed_state_during_sleep = {}

        async def _spy_sleep(_delay):
            async with factory() as s:
                job = await s.get(DownloadJob, "j5")
            observed_state_during_sleep["state"] = job.state
            observed_state_during_sleep["attempts"] = job.attempts
            await real_sleep(0)  # yield without a real multi-second wait

        monkeypatch.setattr(download_worker.asyncio, "sleep", _spy_sleep)

        await download_worker._handle_transient(factory, "j5", "upstream 503")

        assert observed_state_during_sleep["state"] == "queued"
        assert observed_state_during_sleep["attempts"] == 1

        async with factory() as s:
            job = await s.get(DownloadJob, "j5")
        assert job.state == "queued"
        assert job.error == "upstream 503"
