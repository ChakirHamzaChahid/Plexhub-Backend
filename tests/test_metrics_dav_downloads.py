"""Tests for Lot 12 T12.2 — Prometheus metrics for DAV & Downloads."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.dav import relay
from app.dav.vfs import DavEntry, DavTree, dav_tree_cache
from app.db import database as db_module
from app.main import app
from app.models.database import DownloadJob, XtreamAccount
from app.services import download_service
from app.utils import metrics
from app.utils.server_id import build_server_id
from app.utils.time import now_ms
from app.workers import download_worker


def test_lot12_metrics_zero_initialized_in_prometheus_registry():
    body = generate_latest(REGISTRY).decode("utf-8")
    assert 'plexhub_dav_requests_total{method="GET",status="2xx"}' in body
    assert 'plexhub_dav_requests_total{method="HEAD",status="4xx"}' in body
    assert "plexhub_dav_bytes_served_total " in body
    assert 'plexhub_download_job_outcomes_total{source="xtream",status="completed"}' in body
    assert 'plexhub_download_job_outcomes_total{source="plex",status="failed"}' in body
    assert "plexhub_download_queue_size " in body


async def test_dav_requests_total_and_bytes_served_increment(db_engine, tmp_path: Path, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(settings, "DAV_ENABLED", True)
    monkeypatch.setattr(settings, "DAV_USERNAME", "davuser")
    monkeypatch.setattr(settings, "DAV_PASSWORD", "davpass")
    monkeypatch.setattr(settings, "DAV_PROBE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DAV_PROBE_CACHE_MAX_MB", 50)

    key = relay.probe_cache_key("xtream_1", "vod_99")
    relay.write_probe_slice(key, "head", b"Z" * 100, 100)

    tree = DavTree(
        entries={
            "": DavEntry(name="", is_dir=True),
            "Film.mkv": DavEntry(
                name="Film.mkv", is_dir=False, size=100,
                server_id="xtream_1", rating_key="vod_99",
            ),
        },
        children={"": ["Film.mkv"]},
        built_at=9999999999.0,
    )
    monkeypatch.setattr(dav_tree_cache, "_tree", tree)

    before_head = metrics.dav_requests_total.labels(method="HEAD", status="2xx")._value.get()
    before_get = metrics.dav_requests_total.labels(method="GET", status="2xx")._value.get()
    before_404 = metrics.dav_requests_total.labels(method="GET", status="4xx")._value.get()
    before_bytes = metrics.dav_bytes_served_total._value.get()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        auth=("davuser", "davpass"),
    ) as client:
        r_head = await client.head("/dav/Film.mkv")
        assert r_head.status_code == 200

        r_get = await client.get(
            "/dav/Film.mkv",
            headers={"Range": "bytes=0-19"},
        )
        assert r_get.status_code == 206
        assert r_get.content == b"Z" * 20

        r_miss = await client.get("/dav/Unknown.mkv")
        assert r_miss.status_code == 404

    assert metrics.dav_requests_total.labels(method="HEAD", status="2xx")._value.get() == before_head + 1
    assert metrics.dav_requests_total.labels(method="GET", status="2xx")._value.get() == before_get + 1
    assert metrics.dav_requests_total.labels(method="GET", status="4xx")._value.get() == before_404 + 1
    assert metrics.dav_bytes_served_total._value.get() == before_bytes + 20


async def test_download_worker_updates_queue_size_and_jobs_total(
    db_engine, tmp_path: Path, monkeypatch, respx_mock,
):
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    account = XtreamAccount(
        id="acc1", label="Acc", base_url="http://provider.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )
    job = DownloadJob(
        id="job_m1", batch_id=None, server_id=build_server_id("acc1"),
        rating_key="vod_55", media_type="movie", unification_id=None,
        title="Film Metric", season=None, episode=None,
        dest_path="Movies/Film Metric/Film Metric.mkv",
        state="queued", bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )
    async with factory() as s:
        s.add_all([account, job])
        await s.commit()

    await download_worker._refresh_queue_depth_gauge(factory)
    assert metrics.download_queue_size._value.get() == 1

    before_completed = metrics.download_jobs_total.labels(source="xtream", status="completed")._value.get()
    respx_mock.get("http://provider.example/movie/u/p/55.ts").mock(
        return_value=httpx.Response(200, content=b"video")
    )

    with (
        patch.object(download_service, "assert_public_redirect_host", new=AsyncMock(return_value=None)),
        patch.object(download_service, "_assert_public_redirect_host", new=AsyncMock(return_value=None)),
    ):
        await download_worker._run_job(factory, "job_m1", asyncio.Semaphore(1))

    assert metrics.download_jobs_total.labels(source="xtream", status="completed")._value.get() == before_completed + 1
    await download_worker._refresh_queue_depth_gauge(factory)
    assert metrics.download_queue_size._value.get() == 0
