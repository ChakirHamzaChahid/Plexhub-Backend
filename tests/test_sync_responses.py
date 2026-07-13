"""Guard test for CR-C03: the sync router's fire-and-forget/status endpoints
must return typed, camelCase Pydantic response models (``JobIdResponse``,
``MessageResponse``, ``SyncJobListResponse``/``SyncJobResponse``), not raw
untyped dicts (which bypass validation and are missing from the OpenAPI
schema).

Same pattern as ``tests/test_categories_refresh_camelcase.py`` (CR-C02).
"""
from __future__ import annotations

from app.config import settings
from app.workers import sync_worker as sync_worker_module
from app.workers import enrichment_worker as enrichment_worker_module
from app.api import sync as sync_router_module


# The JSON API is X-API-Key gated (fail-closed) — same pattern as
# tests/test_adult_classification.py / test_auth_guard.py.
API_KEY = "test-master-key"
API_HEADERS = {"X-API-Key": API_KEY}


async def test_list_sync_jobs_returns_typed_camelcase_shape(monkeypatch, api_client):
    """GET /api/sync/jobs used to return a raw ``{"jobs": [...]}`` dict whose
    entries carried a snake_case ``job_id`` key. It must now be ``jobId``
    (camelCase), via ``SyncJobListResponse``/``SyncJobResponse``."""
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    monkeypatch.setattr(
        sync_worker_module,
        "_sync_jobs",
        {
            "sync_acct1_12345": {
                "status": "completed",
                "progress": {"total": 10, "synced": 10},
            },
        },
    )

    resp = await api_client.get("/api/sync/jobs", headers=API_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert "jobs" in body
    assert len(body["jobs"]) == 1

    job = body["jobs"][0]
    # New camelCase contract.
    assert job["jobId"] == "sync_acct1_12345"
    assert job["status"] == "completed"
    assert job["progress"] == {"total": 10, "synced": 10}

    # The old snake_case key must be gone from the wire (CR-C03).
    assert "job_id" not in job


async def test_cancel_sync_returns_typed_message(monkeypatch, api_client):
    """DELETE /api/sync/cancel/{task_name} used to return a raw
    ``{"message": ...}`` dict. It must now be a typed ``MessageResponse``
    (wire shape unchanged — single-word key, no camelCase transform)."""
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    monkeypatch.setattr(sync_router_module, "cancel_task_by_name", lambda name: True)

    resp = await api_client.delete("/api/sync/cancel/sync_all", headers=API_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"message": "Task 'sync_all' cancelled"}


async def test_trigger_enrichment_returns_typed_job_id(monkeypatch, api_client):
    """POST /api/sync/enrichment used to return a raw ``{"jobId": ...}``
    dict. It must now be a typed ``JobIdResponse`` (wire shape unchanged —
    ``jobId`` was already camelCase)."""
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)

    async def _noop_run():
        return None

    monkeypatch.setattr(enrichment_worker_module, "run", _noop_run)

    resp = await api_client.post("/api/sync/enrichment", headers=API_HEADERS)

    assert resp.status_code == 202
    body = resp.json()
    assert set(body.keys()) == {"jobId"}
    assert body["jobId"].startswith("enrichment_")
