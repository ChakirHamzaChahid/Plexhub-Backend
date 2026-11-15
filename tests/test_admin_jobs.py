"""tests/test_admin_jobs.py — Live jobs control & backup status UI (T9.2)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services import backup_status_service, job_registry
from app.utils import job_health, tasks as app_tasks


pytestmark = pytest.mark.asyncio

ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-jobs-pass"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)

EXPECTED_JOBS = ("sync", "epg_sync", "enrichment", "health_check", "plex_generate", "backup")


@pytest.fixture(autouse=True)
def _configure_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "BACKUP_ENABLED", True)


@pytest.fixture(autouse=True)
def _clean_job_state():
    job_registry._jobs.clear()
    job_health._last_success_ms.clear()
    yield
    job_registry._jobs.clear()
    job_health._last_success_ms.clear()


class TestAdminJobsAuthAndCsrf:
    async def test_jobs_page_requires_basic_auth(self, api_client):
        resp = await api_client.get("/admin/jobs")
        assert resp.status_code == 401

    async def test_jobs_panel_requires_basic_auth(self, api_client):
        resp = await api_client.get("/admin/jobs/panel")
        assert resp.status_code == 401

    async def test_jobs_run_requires_basic_auth(self, api_client):
        resp = await api_client.post(
            "/admin/jobs/sync/run",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 401

    async def test_jobs_run_rejects_cross_site_csrf(self, api_client):
        resp = await api_client.post(
            "/admin/jobs/sync/run",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cross-site request rejected"


class TestAdminJobsRendering:
    async def test_jobs_page_renders_all_6_jobs_and_htmx_polling(self, api_client):
        resp = await api_client.get("/admin/jobs", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        body = resp.text
        assert 'hx-get="/admin/jobs/panel"' in body
        assert 'hx-trigger="every 5s"' in body
        for name in EXPECTED_JOBS:
            assert f'id="job-row-{name}"' in body
            assert f'hx-post="/admin/jobs/{name}/run"' in body

    async def test_jobs_panel_renders_backup_status(self, api_client, monkeypatch):
        monkeypatch.setattr(
            backup_status_service,
            "compute_status",
            lambda now_ms: {
                "enabled": True,
                "lastSuccessAt": 1_700_000_000_000,
                "ageSeconds": 42,
                "integrityCheck": "ok",
                "lastAttemptAt": 1_700_000_000_000,
                "lastAttemptOutcome": "ok",
                "retentionDays": 7,
                "scheduleHourLocal": 4,
                "scheduleTimezone": "UTC",
                "serverNowMs": now_ms,
            },
        )
        resp = await api_client.get("/admin/jobs/panel", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        assert "Dernière sauvegarde base de données" in resp.text
        assert "(42 s)" in resp.text
        assert ">ok<" in resp.text

    async def test_jobs_panel_sanitizes_secrets_in_error_messages(self, api_client):
        job_registry.set_job(
            "sync_secret_leak",
            {
                "status": "failed",
                "phase": "sync",
                "started_at": 1_700_000_000_000,
                "finished_at": 1_700_000_005_000,
                "error": "Failed http://alice:supersecret@iptv.example.com/get.php?token=tok123",
                "progress": {},
            },
        )
        resp = await api_client.get("/admin/jobs/panel", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        assert "supersecret" not in resp.text
        assert "tok123" not in resp.text
        assert "Erreur" in resp.text

    async def test_jobs_panel_shows_job_health_last_success(self, api_client):
        job_health.mark_job_success("epg_sync")
        resp = await api_client.get("/admin/jobs/panel", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        assert "Dernier run réussi" in resp.text


class TestAdminJobsTrigger:
    @pytest.mark.parametrize("job_name", EXPECTED_JOBS)
    async def test_manual_trigger_runs_mocked_worker(
        self, api_client, monkeypatch, job_name
    ):
        from app.scripts import backup_db
        from app.services import epg_sync_service, plex_generation_service
        from app.workers import enrichment_worker, health_check_worker, sync_worker

        sync_mock = AsyncMock(return_value=None)
        epg_mock = AsyncMock(return_value=None)
        enrich_mock = AsyncMock(return_value=None)
        health_mock = AsyncMock(return_value=None)
        plex_mock = AsyncMock(return_value=None)
        backup_mock = MagicMock(return_value=None)

        monkeypatch.setattr(sync_worker, "run_all_accounts", sync_mock)
        monkeypatch.setattr(epg_sync_service, "run_epg_sync_job", epg_mock)
        monkeypatch.setattr(enrichment_worker, "run", enrich_mock)
        monkeypatch.setattr(health_check_worker, "run", health_mock)
        monkeypatch.setattr(
            plex_generation_service, "generate_plex_library_auto", plex_mock
        )
        monkeypatch.setattr(backup_db, "run_backup", backup_mock)

        resp = await api_client.post(
            f"/admin/jobs/{job_name}/run",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 200
        assert "démarré" in resp.text

        # Let the scheduled background task complete cleanly
        for task in list(app_tasks._background_tasks):
            if task.get_name() == f"admin_job_{job_name}":
                await asyncio.wait_for(task, timeout=2.0)

        mocks = {
            "sync": sync_mock,
            "epg_sync": epg_mock,
            "enrichment": enrich_mock,
            "health_check": health_mock,
            "plex_generate": plex_mock,
            "backup": backup_mock,
        }
        mocks[job_name].assert_called_once()

    async def test_unknown_job_returns_404(self, api_client):
        resp = await api_client.post(
            "/admin/jobs/does_not_exist/run",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 404

    async def test_already_running_job_returns_409_and_message(self, api_client):
        job_registry.create_job("admin_sync_running", phase="sync")
        resp = await api_client.post(
            "/admin/jobs/sync/run",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 409
        assert "déjà en cours" in resp.text


class TestAdminStatsBackupSummary:
    async def test_stats_fragment_includes_backup_summary(
        self, api_client, monkeypatch, db_factory
    ):
        from app.api import admin as admin_module
        from app.db import database as db_module

        monkeypatch.setattr(db_module, "async_session_factory", db_factory)
        monkeypatch.setattr(admin_module, "async_session_factory", db_factory)
        monkeypatch.setattr(
            backup_status_service,
            "compute_status",
            lambda now_ms: {
                "enabled": True,
                "lastSuccessAt": 1_700_000_000_000,
                "ageSeconds": 10,
                "integrityCheck": "ok",
                "lastAttemptAt": 1_700_000_000_000,
                "lastAttemptOutcome": "ok",
                "retentionDays": 7,
                "scheduleHourLocal": 4,
                "scheduleTimezone": "UTC",
                "serverNowMs": now_ms,
            },
        )
        resp = await api_client.get("/admin/stats", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        assert "Sauvegarde BDD" in resp.text
        assert "/admin/jobs" in resp.text
