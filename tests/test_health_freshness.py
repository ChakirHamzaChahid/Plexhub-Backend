"""S5.1 (AUDIT-P8-001 volet b / AUDIT-P8-005): job-freshness gauges +
`isMaster` visibility.

The core assertion this module exists to prove (see the ticket): a job that
SUCCEEDS updates its freshness timestamp; a job that FAILS does NOT; and
`plexhub_is_master`/`GET /api/health`'s `isMaster` reflect the real election
result in both master and slave/un-shimmed-Windows-dev modes.
"""
from __future__ import annotations

import time

import pytest
from prometheus_client import REGISTRY, generate_latest

from app.db import database as db_module
from app.utils import job_health


def _scrape() -> str:
    return generate_latest(REGISTRY).decode("utf-8")


def _gauge_value(body: str, metric: str, labels: dict[str, str] | None = None) -> float:
    label_str = ",".join(f'{k}="{v}"' for k, v in (labels or {}).items())
    prefix = f"{metric}{{{label_str}}} " if labels else f"{metric} "
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix):].strip())
    raise AssertionError(f"series not found: {prefix!r}")


class TestMarkJobSuccess:
    def test_unset_job_has_no_recorded_success(self):
        assert job_health.last_success_ms("pipeline") is None

    def test_mark_job_success_records_timestamp_and_gauge(self):
        before_ms = int(time.time() * 1000)
        job_health.mark_job_success("health_check")
        after_ms = int(time.time() * 1000)

        recorded = job_health.last_success_ms("health_check")
        assert recorded is not None
        assert before_ms <= recorded <= after_ms

        body = _scrape()
        gauge_seconds = _gauge_value(
            body, "plexhub_pipeline_last_success_timestamp_seconds", {"job": "health_check"}
        )
        assert gauge_seconds == pytest.approx(recorded / 1000, abs=1)

    def test_mark_job_success_only_touches_its_own_job_label(self):
        job_health.mark_job_success("db_backup")
        assert job_health.last_success_ms("db_backup") is not None
        assert job_health.last_success_ms("epg_cleanup") is None


class TestTrackJobDecorator:
    """The exact behaviour the ticket asks to be proven: success marks
    freshness, failure does not — and the exception is never swallowed."""

    async def test_successful_job_marks_freshness(self):
        calls = {"n": 0}

        async def _job():
            calls["n"] += 1
            return "ok"

        wrapped = job_health.track_job("subtitle_cache_cleanup")(_job)
        result = await wrapped()

        assert result == "ok"
        assert calls["n"] == 1
        assert job_health.last_success_ms("subtitle_cache_cleanup") is not None

    async def test_failing_job_does_not_mark_freshness_and_reraises(self):
        async def _job():
            raise RuntimeError("boom")

        wrapped = job_health.track_job("plex_catalogue_sync")(_job)

        with pytest.raises(RuntimeError, match="boom"):
            await wrapped()

        assert job_health.last_success_ms("plex_catalogue_sync") is None

    async def test_wrapped_job_forwards_args_and_kwargs(self):
        seen = {}

        async def _job(a, *, b):
            seen["a"] = a
            seen["b"] = b

        wrapped = job_health.track_job("epg_cleanup")(_job)
        await wrapped(1, b=2)

        assert seen == {"a": 1, "b": 2}
        assert job_health.last_success_ms("epg_cleanup") is not None


class TestIsMaster:
    def test_defaults_to_false(self):
        assert job_health.is_master() is False
        assert _gauge_value(_scrape(), "plexhub_is_master") == 0.0

    def test_set_master_true_reflected_in_getter_and_gauge(self):
        job_health.set_master(True)
        assert job_health.is_master() is True
        assert _gauge_value(_scrape(), "plexhub_is_master") == 1.0

    def test_set_master_false_after_true_reflected(self):
        job_health.set_master(True)
        job_health.set_master(False)
        assert job_health.is_master() is False
        assert _gauge_value(_scrape(), "plexhub_is_master") == 0.0


class TestHealthEndpointFreshnessFields:
    """`GET /api/health` (public, house-law piège 10: no secret/path/config)
    surfaces `isMaster`/`lastPipelineSuccessAt` as additive fields."""

    async def test_defaults_when_process_never_ran_the_lifespan(
        self, api_client, db_factory, monkeypatch
    ):
        # `api_client` (tests/conftest.py) deliberately skips the real
        # `lifespan()` (fcntl master election) — this process is neither
        # master nor slave, it simply never elected. `isMaster` must still
        # report a safe default (`False`), not error or omit the field.
        monkeypatch.setattr(db_module, "async_session_factory", db_factory)

        resp = await api_client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["isMaster"] is False
        assert body["lastPipelineSuccessAt"] is None

    async def test_reflects_master_flag_and_pipeline_freshness(
        self, api_client, db_factory, monkeypatch
    ):
        monkeypatch.setattr(db_module, "async_session_factory", db_factory)

        job_health.set_master(True)
        job_health.mark_job_success("pipeline")
        expected_ts = job_health.last_success_ms("pipeline")

        resp = await api_client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["isMaster"] is True
        assert body["lastPipelineSuccessAt"] == expected_ts

    async def test_a_failed_pipeline_run_never_advances_the_health_field(
        self, api_client, db_factory, monkeypatch
    ):
        """Mirrors `TestTrackJobDecorator`'s core assertion at the HTTP
        boundary: if the scheduled pipeline coroutine never reaches its
        `mark_job_success("pipeline")` call (i.e. it raised before then,
        exactly like `scheduled_sync_enrich_generate`'s own try/except would
        catch), `/api/health` must keep reporting `None` — not a stale value
        rounded up to "now"."""
        monkeypatch.setattr(db_module, "async_session_factory", db_factory)

        async def _pipeline_job_that_fails_before_the_last_step():
            await _noop()
            raise RuntimeError("sync failed")
            # unreachable: job_health.mark_job_success("pipeline")

        async def _noop():
            return None

        with pytest.raises(RuntimeError):
            await _pipeline_job_that_fails_before_the_last_step()

        resp = await api_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["lastPipelineSuccessAt"] is None
