"""ADR 0004 Décision 2 / AUDIT-P5-001, AUDIT-P5-008, AUDIT-P6-005.

This is the exact test the audit found missing: a trigger's returned
``jobId`` must be a LIVE handle, not a dead one. Before this fix:

  - `POST /api/sync/xtream/all` -> `jobId` -> `GET /status/{jobId}` -> 200
    `{"status": "unknown"}` (indistinguishable from a real pending job);
  - `/enrichment`, `/validate-streams`, `/full-pipeline` never registered
    anything at all;
  - only `/xtream` (single-account) had a tracker, and even then the
    router's `jobId` (`f"sync_{id}_{id(task)}"`) never matched the worker's
    own registration key (`f"sync_{id}_{now_ms()}"`).

Workers are monkeypatched to no-ops (per house law: hermetic, no real
network/DB from these tests) -- what's under test is the registry
plumbing/contract, not sync/enrichment/validation/generation themselves
(those have their own dedicated test suites).
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import job_registry
from app.workers import sync_worker as sync_worker_module
from app.workers import enrichment_worker as enrichment_worker_module
from app.workers import health_check_worker as health_check_worker_module
from app.services import plex_generation_service as plex_generation_service_module

API_KEY = "test-master-key"
API_HEADERS = {"X-API-Key": API_KEY}

# `status` keeps its pre-existing vocabulary (ADR 0004 Décision 2 point 5) --
# what changed is that an id now either resolves to ONE of these, or 404s.
# It must never again be the opaque `"unknown"` string.
KNOWN_STATUSES = {"processing", "completed", "failed"}


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)


@pytest.fixture(autouse=True)
def _isolated_job_registry():
    """The registry is a module-level global shared process-wide (by
    design, see job_registry.py docstring) -- isolate it per test so FIFO
    eviction/listing assertions aren't polluted by other test modules."""
    job_registry._jobs.clear()
    yield
    job_registry._jobs.clear()


async def _noop() -> None:
    return None


async def _noop_sync_account(account_id: str, job_id: str | None = None) -> str:
    if job_id:
        job_registry.update_job(job_id, status="completed", progress={"total": 0})
    return job_id or f"sync_{account_id}_noop"


# ─── Trigger -> status, the 5 endpoints ──────────────────────────────────


class TestTriggerToStatusHandshake:
    async def test_single_account_sync_trigger_to_status(self, monkeypatch, api_client):
        monkeypatch.setattr(sync_worker_module, "sync_account", _noop_sync_account)

        resp = await api_client.post(
            "/api/sync/xtream", json={"accountId": "acct1"}, headers=API_HEADERS,
        )
        assert resp.status_code == 202
        job_id = resp.json()["jobId"]

        status_resp = await api_client.get(
            f"/api/sync/status/{job_id}", headers=API_HEADERS,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] in KNOWN_STATUSES

    async def test_sync_all_trigger_to_status(self, monkeypatch, api_client):
        monkeypatch.setattr(sync_worker_module, "run_all_accounts", _noop)

        resp = await api_client.post("/api/sync/xtream/all", headers=API_HEADERS)
        assert resp.status_code == 202
        job_id = resp.json()["jobId"]

        status_resp = await api_client.get(
            f"/api/sync/status/{job_id}", headers=API_HEADERS,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] in KNOWN_STATUSES

    async def test_enrichment_trigger_to_status(self, monkeypatch, api_client):
        monkeypatch.setattr(enrichment_worker_module, "run", _noop)

        resp = await api_client.post("/api/sync/enrichment", headers=API_HEADERS)
        assert resp.status_code == 202
        job_id = resp.json()["jobId"]

        status_resp = await api_client.get(
            f"/api/sync/status/{job_id}", headers=API_HEADERS,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] in KNOWN_STATUSES

    async def test_validation_trigger_to_status(self, monkeypatch, api_client):
        monkeypatch.setattr(
            health_check_worker_module, "run_pipeline_validation", _noop,
        )

        resp = await api_client.post("/api/sync/validate-streams", headers=API_HEADERS)
        assert resp.status_code == 202
        job_id = resp.json()["jobId"]

        status_resp = await api_client.get(
            f"/api/sync/status/{job_id}", headers=API_HEADERS,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] in KNOWN_STATUSES

    async def test_full_pipeline_trigger_to_status(self, monkeypatch, api_client):
        # All 4 stages mocked -- this test is about the registry contract,
        # not about sync/enrichment/validation/generation correctness.
        monkeypatch.setattr(sync_worker_module, "run_all_accounts", _noop)
        monkeypatch.setattr(enrichment_worker_module, "run", _noop)
        monkeypatch.setattr(
            health_check_worker_module, "run_pipeline_validation", _noop,
        )
        monkeypatch.setattr(
            plex_generation_service_module, "generate_plex_library_auto", _noop,
        )

        resp = await api_client.post("/api/sync/full-pipeline", headers=API_HEADERS)
        assert resp.status_code == 202
        job_id = resp.json()["jobId"]

        status_resp = await api_client.get(
            f"/api/sync/status/{job_id}", headers=API_HEADERS,
        )
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["status"] in KNOWN_STATUSES
        # AUDIT-P6-005: the pipeline's current phase is exposed without a
        # dedicated endpoint -- whichever phase the (mocked, fast) run has
        # reached by the time we poll, it must be one of the known stages.
        assert body["phase"] in {
            "sync", "enrichment", "validation", "generation", "snapshot",
        }


# ─── GET /status/{unknown} -> 404 ────────────────────────────────────────


class TestUnknownJobId404:
    async def test_unknown_job_id_returns_404_not_200_unknown(self, api_client):
        resp = await api_client.get(
            "/api/sync/status/does-not-exist-at-all", headers=API_HEADERS,
        )
        assert resp.status_code == 404
        # The old contract returned 200 {"status": "unknown"} -- must be gone.
        assert resp.json() != {"status": "unknown"}


# ─── GET /jobs sees non-account job kinds too ────────────────────────────


class TestJobsListing:
    async def test_jobs_list_includes_a_non_account_trigger(self, monkeypatch, api_client):
        """Before this fix, `GET /api/sync/jobs` only ever listed
        per-account sync jobs -- enrichment/validation/pipeline triggers
        were invisible to it (nothing registered them)."""
        monkeypatch.setattr(enrichment_worker_module, "run", _noop)

        resp = await api_client.post("/api/sync/enrichment", headers=API_HEADERS)
        job_id = resp.json()["jobId"]

        jobs_resp = await api_client.get("/api/sync/jobs", headers=API_HEADERS)
        assert jobs_resp.status_code == 200
        ids = [j["jobId"] for j in jobs_resp.json()["jobs"]]
        assert job_id in ids


# ─── FIFO eviction ────────────────────────────────────────────────────────


class TestFifoEviction:
    async def test_oldest_entry_is_evicted_beyond_the_cap_and_then_404s(self, api_client):
        total = job_registry._MAX_JOBS + 5
        for i in range(total):
            job_registry.create_job(f"evict_test_{i}")

        assert len(job_registry._jobs) == job_registry._MAX_JOBS

        oldest_resp = await api_client.get(
            "/api/sync/status/evict_test_0", headers=API_HEADERS,
        )
        assert oldest_resp.status_code == 404

        newest_id = f"evict_test_{total - 1}"
        newest_resp = await api_client.get(
            f"/api/sync/status/{newest_id}", headers=API_HEADERS,
        )
        assert newest_resp.status_code == 200


# ─── job_registry unit-level contract (no HTTP) ──────────────────────────


class TestJobRegistryUnit:
    def test_create_job_then_get_job_is_processing(self):
        job_registry.create_job("unit_1", phase="sync")
        job = job_registry.get_job("unit_1")
        assert job is not None
        assert job["status"] == "processing"
        assert job["phase"] == "sync"
        assert job["finished_at"] is None

    def test_update_job_merges_and_stamps_finished_at_on_terminal_status(self):
        job_registry.create_job("unit_2")
        job_registry.update_job("unit_2", status="completed", progress={"total": 3})
        job = job_registry.get_job("unit_2")
        assert job["status"] == "completed"
        assert job["progress"] == {"total": 3}
        assert job["finished_at"] is not None

    def test_update_job_without_prior_create_job_creates_a_fresh_entry(self):
        assert job_registry.get_job("unit_3_never_created") is None
        job_registry.update_job("unit_3_never_created", status="failed", error="boom")
        job = job_registry.get_job("unit_3_never_created")
        assert job is not None
        assert job["status"] == "failed"
        assert job["error"] == "boom"

    def test_get_job_unknown_returns_none_not_raise(self):
        assert job_registry.get_job("totally-unknown") is None
