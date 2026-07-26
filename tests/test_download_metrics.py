"""S5.2 (AUDIT-P8-002 / F-103) — the physical-download subsystem gains
observability: queue depth, bytes transferred, failures by reason.

See `docs/plans/2026-07-26-refacto-audit-v1-plan.md` §VAGUE 5 S5.2. The 3
metrics under test are DECLARED and zero-initialised by S1.4
(`app/utils/metrics.py`, guarded by `tests/test_metrics_registry.py`) — this
file only exercises the wiring added in `app/workers/download_worker.py` and
`app/services/download_service.py`:

  - `plexhub_download_jobs{state}` — a Gauge refreshed from a live
    ``COUNT(*) GROUP BY state`` snapshot (`_refresh_queue_depth_gauge`), not a
    hand-incremented/decremented counter (see that function's docstring for
    why: cross-process writers in `download_service.py` — cancel/retry/
    clear_finished — can run on a uvicorn worker process OTHER than the one
    holding the master election and draining, so an inc/dec approach would
    update the wrong process's in-memory `prometheus_client` registry).
  - `plexhub_download_bytes_total` — incremented in `download_to_disk`'s
    chunk loop, i.e. real bytes actually written to `.part`, not the final
    file size (so resumes/retries/skip-if-exists are counted correctly,
    tested explicitly below).
  - `plexhub_download_failures_total{reason}` — incremented at the 3 points a
    job's outcome becomes known (`_mark_failed`, the confinement except-
    branch, `_handle_transient`), using `download_service.classify_failure_
    reason`. A 404 (a real failure with NO matching reason among the 4
    CLOSED values `http_403|disk_full|timeout|confinement`) must NOT be
    mislabelled onto any of them — tested explicitly (the documented gap).

All HTTP is mocked via `respx` (`xtream_mock` fixture, no base_url). No real
filesystem writes outside `download_dir` (tmp_path-backed, F-007). Every
counter/gauge assertion is a DELTA against a `before` snapshot (the
`prometheus_client` registry is process-global and shared across the whole
test session — see `tests/test_media_episodes_missing_server_id_metric.py`
for the same established pattern).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import DownloadJob, XtreamAccount
from app.services import download_service
from app.utils import metrics
from app.utils.server_id import build_server_id
from app.utils.time import now_ms
from app.workers import download_worker
from app.workers.download_worker import _run_job

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

ACCOUNT_ID = "acc1"
SERVER_ID = build_server_id(ACCOUNT_ID)


def _account(*, username: str = "u", password: str = "p") -> XtreamAccount:
    return XtreamAccount(
        id=ACCOUNT_ID, label="Compte", base_url="http://provider.example", port=80,
        username=username, password=password, is_active=True, created_at=0,
    )


def _job(job_id: str, *, rating_key: str, state: str = "queued", dest_path: str | None = None) -> DownloadJob:
    return DownloadJob(
        id=job_id, batch_id=None, server_id=SERVER_ID, rating_key=rating_key,
        media_type="movie", unification_id=None, title=f"Film {job_id}",
        season=None, episode=None,
        dest_path=dest_path or f"Movies/{job_id}/{job_id}.mkv",
        state=state, bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )


def _url(n: int, *, username: str = "u", password: str = "p") -> str:
    return f"http://provider.example:80/movie/{username}/{password}/{n}.mkv"


async def _seeded_factory(db_engine, *, jobs: list[DownloadJob], account: XtreamAccount | None = None):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(account or _account())
        s.add_all(jobs)
        await s.commit()
    return factory


def _failure_count(reason: str) -> float:
    return metrics.download_failures_total.labels(reason=reason)._value.get()


def _queue_gauge(state: str) -> float:
    return metrics.download_jobs.labels(state=state)._value.get()


def _bytes_total() -> float:
    return metrics.download_bytes_total._value.get()


# ─── plexhub_download_bytes_total: real transferred bytes, not file size ──


class TestDownloadBytesTotal:
    async def test_full_transfer_increments_by_body_length(self, tmp_path, xtream_mock):
        url = "http://provider.example/movie/u/p/bytes1.mkv"
        dest = tmp_path / "Movies" / "X" / "X.mkv"
        body = b"0123456789" * 10  # 100 bytes
        xtream_mock.get(url).mock(
            return_value=httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})
        )
        before = _bytes_total()

        result = await download_service.download_to_disk(url, dest, chunk_bytes=10)

        assert result.bytes_downloaded == len(body)
        assert _bytes_total() == before + len(body)

    async def test_skip_if_exists_does_not_increment(self, tmp_path):
        dest = tmp_path / "Movies" / "X" / "X.mkv"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"already here")
        before = _bytes_total()

        result = await download_service.download_to_disk("http://unused.example/never-fetched", dest)

        assert result.already_present is True
        assert _bytes_total() == before, (
            "a skip-if-exists transfers ZERO bytes over the wire this call — "
            "must not be counted as if the whole file had just been written"
        )

    async def test_resumed_transfer_counts_only_the_new_bytes(self, tmp_path, xtream_mock):
        url = "http://provider.example/movie/u/p/bytes2.mkv"
        dest = tmp_path / "Movies" / "X" / "X.mkv"
        part = dest.with_name(dest.name + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(b"0123456789")  # 10 bytes already on disk from a prior attempt
        xtream_mock.get(url).mock(
            return_value=httpx.Response(
                206, content=b"ABCDE",
                headers={"Content-Range": "bytes 10-14/15"},
            )
        )
        before = _bytes_total()

        result = await download_service.download_to_disk(url, dest)

        assert result.bytes_downloaded == 15  # 10 resumed + 5 new
        assert _bytes_total() == before + 5, (
            "only the 5 NEWLY transferred bytes must be counted — the 10 "
            "bytes already on disk from a previous attempt were already "
            "counted (or not) when THAT attempt wrote them"
        )


# ─── plexhub_download_jobs{state}: periodic COUNT(*) snapshot, not inc/dec ─


class TestQueueDepthGauge:
    async def test_refresh_reflects_live_counts_per_state(self, db_engine, download_dir):
        factory = await _seeded_factory(
            db_engine,
            jobs=[
                _job("q1", rating_key="vod_1.mkv", state="queued"),
                _job("q2", rating_key="vod_2.mkv", state="queued"),
                _job("r1", rating_key="vod_3.mkv", state="running"),
                _job("f1", rating_key="vod_4.mkv", state="failed"),
            ],
        )

        await download_worker._refresh_queue_depth_gauge(factory)

        assert _queue_gauge("queued") == 2
        assert _queue_gauge("running") == 1
        assert _queue_gauge("failed") == 1
        assert _queue_gauge("completed") == 0
        assert _queue_gauge("canceled") == 0

    async def test_refresh_resets_a_state_back_to_zero_once_its_jobs_leave_it(
        self, db_engine, download_dir,
    ):
        """Drift-proof by construction: a state that had jobs on a previous
        refresh but none anymore must read back 0, not a stale stuck value —
        the whole point of re-deriving from a COUNT(*) instead of hand
        incrementing/decrementing at each transition."""
        factory = await _seeded_factory(
            db_engine, jobs=[_job("j1", rating_key="vod_1.mkv", state="queued")],
        )
        await download_worker._refresh_queue_depth_gauge(factory)
        assert _queue_gauge("queued") == 1

        async with factory() as s:
            job = await s.get(DownloadJob, "j1")
            job.state = "completed"
            await s.commit()

        await download_worker._refresh_queue_depth_gauge(factory)
        assert _queue_gauge("queued") == 0
        assert _queue_gauge("completed") == 1

    async def test_refresh_is_best_effort_and_never_raises(self, db_engine, download_dir, monkeypatch):
        def _factory_that_blows_up():
            # `session_factory()` (e.g. `async_sessionmaker(...)`) is called
            # SYNCHRONOUSLY to obtain the async context manager — simulate a
            # broken factory raising right there, before `async with` even
            # starts, the same shape `run_with_retry`'s own tests use.
            raise RuntimeError("simulated DB outage")

        # Must not propagate — the drain loop's tick must survive a failed
        # gauge refresh exactly like it survives any other transient error.
        await download_worker._refresh_queue_depth_gauge(_factory_that_blows_up)


class TestQueueDepthGaugeWiredIntoDrainLoop:
    async def test_run_drain_loop_calls_refresh_at_boot_and_every_tick(
        self, db_engine, monkeypatch, download_dir,
    ):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

        calls = {"n": 0}
        reached_third_call = asyncio.Event()

        async def _spy_refresh(_factory):
            calls["n"] += 1
            if calls["n"] >= 3:
                reached_third_call.set()

        async def _empty_fetch(session_factory, *, limit, exclude_ids):
            return []

        monkeypatch.setattr(download_worker, "_refresh_queue_depth_gauge", _spy_refresh)
        monkeypatch.setattr(download_worker, "_fetch_queued", _empty_fetch)

        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(download_worker.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(settings, "DOWNLOAD_CONCURRENCY", 1)

        task = asyncio.create_task(download_worker.run_drain_loop(factory))
        try:
            await asyncio.wait_for(reached_third_call.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls["n"] >= 3, "the gauge must be refreshed at boot AND on every subsequent tick"


# ─── plexhub_download_failures_total{reason}: one label per real cause ────


class TestFailureReasonHttp403:
    async def test_permanent_403_without_fallback_labels_http_403(
        self, db_engine, download_dir, xtream_mock,
    ):
        factory = await _seeded_factory(db_engine, jobs=[_job("j1", rating_key="vod_1.mkv")])
        xtream_mock.get(_url(1)).mock(return_value=httpx.Response(403))
        before = _failure_count("http_403")

        await _run_job(factory, "j1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "j1")
        assert job.state == "failed"
        assert job.error == "upstream 403"
        assert _failure_count("http_403") == before + 1


class TestFailureReasonDiskFull:
    async def test_preflight_insufficient_space_labels_disk_full(
        self, db_engine, monkeypatch, download_dir, xtream_mock,
    ):
        factory = await _seeded_factory(db_engine, jobs=[_job("j1", rating_key="vod_1.mkv")])
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 2048)
        monkeypatch.setattr(
            download_service.shutil, "disk_usage",
            lambda path: SimpleNamespace(total=0, used=0, free=10 * 1024 * 1024),
        )
        route = xtream_mock.get(_url(1)).mock(
            return_value=httpx.Response(200, content=b"never fetched")
        )
        before = _failure_count("disk_full")

        await _run_job(factory, "j1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "j1")
        assert job.state == "failed"
        assert "insufficient free disk" in job.error
        assert route.call_count == 0
        assert _failure_count("disk_full") == before + 1

    async def test_mid_transfer_disk_error_labels_disk_full_via_handle_transient(
        self, db_engine, monkeypatch,
    ):
        """`download_to_disk` maps an `OSError` mid-transfer to
        `DownloadTransientError("disk error: ...")` — routed through
        `_handle_transient`, not `_mark_failed` directly."""
        factory = await _seeded_factory(
            db_engine, jobs=[_job("j1", rating_key="vod_1.mkv", state="running")],
        )
        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(download_worker.asyncio, "sleep", _fast_sleep)
        before = _failure_count("disk_full")

        await download_worker._handle_transient(
            factory, "j1", "disk error: No space left on device",
        )

        assert _failure_count("disk_full") == before + 1


class TestFailureReasonTimeout:
    async def test_handle_transient_network_timeout_labels_timeout(
        self, db_engine, monkeypatch,
    ):
        factory = await _seeded_factory(
            db_engine, jobs=[_job("j1", rating_key="vod_1.mkv", state="running")],
        )
        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(download_worker.asyncio, "sleep", _fast_sleep)
        before = _failure_count("timeout")

        await download_worker._handle_transient(factory, "j1", "network timeout")

        assert _failure_count("timeout") == before + 1

    async def test_each_retry_attempt_counts_even_if_the_job_eventually_succeeds(
        self, db_engine, monkeypatch,
    ):
        """One increment per transient ATTEMPT, not only on final give-up —
        retry churn that self-heals must still be visible."""
        factory = await _seeded_factory(
            db_engine, jobs=[_job("j1", rating_key="vod_1.mkv", state="running")],
        )
        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(download_worker.asyncio, "sleep", _fast_sleep)
        before = _failure_count("timeout")

        await download_worker._handle_transient(factory, "j1", "network timeout")
        await download_worker._handle_transient(factory, "j1", "network timeout")

        assert _failure_count("timeout") == before + 2


class TestFailureReasonConfinement:
    async def test_escaping_dest_path_labels_confinement(self, db_engine, download_dir):
        factory = await _seeded_factory(
            db_engine,
            jobs=[_job("j1", rating_key="vod_1.mkv", dest_path="../outside.mkv")],
        )
        before = _failure_count("confinement")

        await _run_job(factory, "j1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "j1")
        assert job.state == "failed"
        assert job.error == "chemin de destination invalide"
        assert _failure_count("confinement") == before + 1


class TestUnmappedFailureReasonIsNotMislabelled:
    async def test_404_is_a_real_failure_but_increments_none_of_the_4_known_reasons(
        self, db_engine, download_dir, xtream_mock,
    ):
        """Documented gap (`download_service.classify_failure_reason`):
        `reason` is a CLOSED 4-value enum. A 404 doesn't fit any of them —
        it must stay unlabelled here rather than being mislabelled onto
        e.g. `http_403`."""
        factory = await _seeded_factory(db_engine, jobs=[_job("j1", rating_key="vod_1.mkv")])
        xtream_mock.get(_url(1)).mock(return_value=httpx.Response(404))
        before = {
            reason: _failure_count(reason)
            for reason in ("http_403", "disk_full", "timeout", "confinement")
        }

        await _run_job(factory, "j1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "j1")
        assert job.state == "failed"
        assert job.error == "upstream 404"
        after = {
            reason: _failure_count(reason)
            for reason in ("http_403", "disk_full", "timeout", "confinement")
        }
        assert after == before, "a 404 must not be mislabelled onto any of the 4 closed reasons"


# ─── Secrets never leak into the metric surface ────────────────────────────


class TestNoSecretLeaksInMetricLabels:
    async def test_credentialed_url_never_appears_in_the_metrics_scrape_after_a_failure(
        self, db_engine, download_dir, xtream_mock,
    ):
        """The Xtream stream URL embeds `user`/`password` in its path
        (`build_stream_url`). Force a real, classified failure (403) and
        prove neither credential — nor the URL/host at all — ever reaches
        the Prometheus text exposition, on ANY metric, not just the download
        ones (piège §9-17c)."""
        username, password = "s3cr3t-user-9f2b", "s3cr3t-pass-7a1c"
        factory = await _seeded_factory(
            db_engine,
            jobs=[_job("j1", rating_key="vod_1.mkv")],
            account=_account(username=username, password=password),
        )
        xtream_mock.get(_url(1, username=username, password=password)).mock(
            return_value=httpx.Response(403)
        )
        before = _failure_count("http_403")

        await _run_job(factory, "j1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "j1")
        assert job.state == "failed"
        # Sanity: the failure really was captured (not silently dropped,
        # which would make the "no leak" assertion below vacuous).
        assert _failure_count("http_403") == before + 1

        body = generate_latest(REGISTRY).decode("utf-8")
        assert username not in body
        assert password not in body
        assert "provider.example" not in body
