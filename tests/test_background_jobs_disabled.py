"""B2-10 — `BACKGROUND_JOBS_ENABLED=false` : instance de test sans tâche de fond (ADR 0012 § D2.4).

Le réglage coupe l'élection master (donc l'import de `fcntl`, absent sous Windows), le planificateur,
le pipeline initial, le worker de téléchargement et la reprise Plex ; `is_master` reste faux. Défaut :
`true`, comportement de production inchangé (couvert par `tests/test_startup_wiring.py`).
`POST /api/accounts` lance toujours la synchronisation du compte créé (indépendante du lifespan).
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.config import settings
from tests.test_startup_wiring import TestLifespanMasterElection


@pytest.fixture
def lifespan_env(monkeypatch, tmp_path):
    helper = TestLifespanMasterElection()
    main_module, instances, calls, captured_bg = helper._wire_common(monkeypatch, tmp_path)
    return main_module, instances, calls, captured_bg


def test_the_setting_defaults_to_true():
    assert settings.BACKGROUND_JOBS_ENABLED is True


async def test_disabled_boots_without_fcntl_scheduler_or_workers(monkeypatch, lifespan_env):
    from app.utils import job_health

    main_module, instances, calls, captured_bg = lifespan_env
    monkeypatch.setattr(settings, "BACKGROUND_JOBS_ENABLED", False)
    monkeypatch.setitem(sys.modules, "fcntl", None)          # `import fcntl` => ImportError (Windows)

    async with main_module.lifespan(main_module.app):
        assert instances == [], "no scheduler on a test instance"
        assert captured_bg == {}, "no boot-time pipeline"
        assert job_health.is_master() is False

    assert calls == []


async def test_disabled_never_starts_the_download_worker_nor_the_plex_reap(monkeypatch, lifespan_env, tmp_path):
    from app.workers import download_worker

    main_module, instances, calls, captured_bg = lifespan_env
    monkeypatch.setattr(settings, "BACKGROUND_JOBS_ENABLED", False)
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", tmp_path / "dl")      # would normally start the drain loop
    monkeypatch.setattr(settings, "PLEX_ACCOUNT_TOKEN", "factice-token")  # would normally schedule the reap
    monkeypatch.setitem(sys.modules, "fcntl", None)
    started: list[str] = []

    async def spy_drain(*_a, **_k):
        started.append("drain")

    monkeypatch.setattr(download_worker, "run_drain_loop", spy_drain)
    async with main_module.lifespan(main_module.app):
        await asyncio.sleep(0)
    assert started == [] and instances == [] and captured_bg == {}


async def test_enabled_still_elects_a_master_and_starts_the_scheduler(monkeypatch, lifespan_env):
    """Non-regression: the default behaviour is untouched."""
    from tests.test_startup_wiring import _fake_fcntl_module

    main_module, instances, calls, captured_bg = lifespan_env
    monkeypatch.setattr(settings, "BACKGROUND_JOBS_ENABLED", True)
    monkeypatch.setitem(sys.modules, "fcntl", _fake_fcntl_module(flock_succeeds=True))
    async with main_module.lifespan(main_module.app):
        assert len(instances) == 1 and instances[0].started is True
        assert "sync_enrich_generate" in instances[0].jobs
    captured_bg["coro"].close()


async def test_the_health_endpoint_reports_not_master(api_client, monkeypatch):
    monkeypatch.setattr(settings, "BACKGROUND_JOBS_ENABLED", False)
    body = (await api_client.get("/api/health")).json()
    assert body["isMaster"] is False


async def test_account_creation_still_starts_its_sync_when_jobs_are_disabled(
    api_client, db_factory, monkeypatch, xtream_mock
):
    from app.db import database as db_module
    from app.workers import sync_worker

    monkeypatch.setattr(settings, "BACKGROUND_JOBS_ENABLED", False)
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", "factice-master-bg-jobs")
    started: list[str] = []

    async def spy_sync(account_id: str):
        started.append(account_id)

    monkeypatch.setattr(sync_worker, "sync_account", spy_sync)
    xtream_mock.get("http://127.0.0.1:9/player_api.php").respond(200, json={
        "user_info": {"status": "Active", "exp_date": "2000000000", "max_connections": "2",
                      "allowed_output_formats": ["m3u8", "ts"]},
        "server_info": {"url": "127.0.0.1", "https_port": "443"},
    })
    resp = await api_client.post(
        "/api/accounts",
        json={"label": "fake", "baseUrl": "http://127.0.0.1", "port": 9, "username": "u", "password": "p"},
        headers={"X-API-Key": "factice-master-bg-jobs"},
    )
    assert resp.status_code in (200, 201), resp.text
    for _ in range(20):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started and started[0] == resp.json()["id"]
