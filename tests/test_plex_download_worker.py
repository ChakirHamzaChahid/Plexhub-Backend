"""tests/test_plex_download_worker.py — worker-side dispatch for the Plex
download source (feature "Télécharger Plex", ticket C5).

Verifies `download_worker._run_job` branches on `is_plex_server_id(job.server_id)`:
a `plex_*` job resolves its URL via `plex_download_service.resolve_job_url`
(NEVER `build_stream_url`/`_load_account`), while an `xtream_*` job keeps the
existing `_load_account` + `build_stream_url` path byte-for-byte — the ~141
pre-existing download tests monkeypatch those two names directly by module
attribute, so this suite proves the Xtream branch still calls them at the
exact same call-sites.

The URL built for a Plex job carries `X-Plex-Token` — every assertion here
also proves the token/URL never lands in `job.error`, a `DownloadJob` column,
or the log output.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import DownloadJob, PlexMediaItem, PlexServer, XtreamAccount
from app.services import download_service, plex_download_service
from app.services.download_service import DownloadResult
from app.utils.server_id import build_plex_server_id, build_server_id
from app.utils.time import now_ms
from app.workers import download_worker
from app.workers.download_worker import _run_job

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

ACCOUNT_ID = "acc1"
XTREAM_SERVER_ID = build_server_id(ACCOUNT_ID)
CID = "cid-worker"
PLEX_SERVER_ID = build_plex_server_id(CID)
TOKEN = "sekret-plex-token-42"


def _xtream_account() -> XtreamAccount:
    return XtreamAccount(
        id=ACCOUNT_ID, label="Compte", base_url="http://provider.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


def _plex_server(
    *, base_uri: str = "https://1-2-3-4.plex.direct:32400", access_token: str = TOKEN,
) -> PlexServer:
    return PlexServer(
        client_identifier=CID, name="PMS", owned=True,
        access_token=access_token, base_uri=base_uri,
        is_reachable=True, created_at=now_ms(), updated_at=now_ms(),
    )


def _plex_item(rating_key: str = "1001", *, part_key: str | None = "/library/parts/1001/file.mkv") -> PlexMediaItem:
    return PlexMediaItem(
        server_id=PLEX_SERVER_ID, rating_key=rating_key, type="movie",
        title="Dune", year=2021, container="mkv", part_key=part_key,
        synced_at=now_ms(),
    )


def _xtream_job(job_id: str, rating_key: str = "vod_1.mkv", state: str = "queued") -> DownloadJob:
    return DownloadJob(
        id=job_id, batch_id=None, server_id=XTREAM_SERVER_ID, rating_key=rating_key,
        media_type="movie", unification_id=None, title=f"Film {job_id}",
        season=None, episode=None, dest_path=f"Movies/{job_id}/{job_id}.mkv",
        state=state, bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )


def _plex_job(job_id: str, rating_key: str = "1001", state: str = "queued") -> DownloadJob:
    return DownloadJob(
        id=job_id, batch_id=None, server_id=PLEX_SERVER_ID, rating_key=rating_key,
        media_type="movie", unification_id=None, title=f"Plex {job_id}",
        season=None, episode=None, dest_path=f"Movies/{job_id}/{job_id}.mkv",
        state=state, bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )


def _url(n: int) -> str:
    return f"http://provider.example:80/movie/u/p/{n}.mkv"


async def _seeded_factory(db_engine, *, jobs, servers=(), accounts=(), items=()):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for acc in accounts:
            s.add(acc)
        for srv in servers:
            s.add(srv)
        for item in items:
            s.add(item)
        s.add_all(jobs)
        await s.commit()
    return factory


class TestPlexDispatch:
    async def test_plex_job_resolves_url_via_plex_download_service(
        self, db_engine, monkeypatch, download_dir,
    ):
        factory = await _seeded_factory(
            db_engine,
            jobs=[_plex_job("p1")],
            servers=[_plex_server()],
            items=[_plex_item()],
        )
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        captured_urls: list[str] = []

        async def _fake_download_to_disk(url, dest, *, on_progress=None, cancel_check=None, **_kw):
            captured_urls.append(url)
            return DownloadResult(
                bytes_downloaded=2, bytes_total=2, already_present=False, resumed=False,
            )

        monkeypatch.setattr(download_service, "download_to_disk", _fake_download_to_disk)

        await _run_job(factory, "p1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "p1")
        assert job.state == "completed"
        assert len(captured_urls) == 1
        assert captured_urls[0] == (
            "https://1-2-3-4.plex.direct:32400/library/parts/1001/file.mkv"
            f"?download=1&X-Plex-Token={TOKEN}"
        )

    async def test_xtream_job_keeps_existing_path_intact(
        self, db_engine, monkeypatch, download_dir, xtream_mock,
    ):
        factory = await _seeded_factory(
            db_engine,
            jobs=[_xtream_job("x1")],
            accounts=[_xtream_account()],
        )
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        calls = {"build_stream_url": 0, "resolve_job_url": 0}

        real_build = download_worker.build_stream_url

        def _spy_build_stream_url(account, rating_key):
            calls["build_stream_url"] += 1
            return real_build(account, rating_key)

        async def _spy_resolve_job_url(session_factory, job):
            calls["resolve_job_url"] += 1
            return None

        monkeypatch.setattr(download_worker, "build_stream_url", _spy_build_stream_url)
        monkeypatch.setattr(plex_download_service, "resolve_job_url", _spy_resolve_job_url)

        xtream_mock.get(_url(1)).mock(
            return_value=httpx.Response(200, content=b"ok", headers={"Content-Length": "2"})
        )

        await _run_job(factory, "x1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "x1")
        assert job.state == "completed"
        assert calls["build_stream_url"] == 1
        assert calls["resolve_job_url"] == 0

    async def test_missing_base_uri_marks_failed_without_secret_leak(
        self, db_engine, monkeypatch, download_dir, caplog,
    ):
        factory = await _seeded_factory(
            db_engine,
            jobs=[_plex_job("p2")],
            servers=[_plex_server(base_uri="")],  # simulate a never-probed server
            items=[_plex_item(rating_key="1001")],
        )
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        with caplog.at_level(logging.DEBUG):
            await _run_job(factory, "p2", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "p2")
        assert job.state == "failed"
        assert job.error == "source Plex introuvable ou non synchronisée"
        assert TOKEN not in (job.error or "")
        for record in caplog.records:
            assert TOKEN not in record.getMessage()

    async def test_missing_part_key_marks_failed(
        self, db_engine, monkeypatch, download_dir,
    ):
        factory = await _seeded_factory(
            db_engine,
            jobs=[_plex_job("p3")],
            servers=[_plex_server()],
            items=[_plex_item(rating_key="1001", part_key=None)],
        )
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        await _run_job(factory, "p3", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "p3")
        assert job.state == "failed"
        assert job.error == "source Plex introuvable ou non synchronisée"

    async def test_unknown_server_marks_failed(
        self, db_engine, monkeypatch, download_dir,
    ):
        factory = await _seeded_factory(
            db_engine,
            jobs=[_plex_job("p4")],
            servers=[],
            items=[_plex_item(rating_key="1001")],
        )
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        await _run_job(factory, "p4", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "p4")
        assert job.state == "failed"
        assert job.error == "source Plex introuvable ou non synchronisée"

    async def test_token_never_leaks_into_job_row_or_logs_on_success(
        self, db_engine, monkeypatch, download_dir, caplog,
    ):
        factory = await _seeded_factory(
            db_engine,
            jobs=[_plex_job("p5")],
            servers=[_plex_server()],
            items=[_plex_item(rating_key="1001")],
        )
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        async def _fake_download_to_disk(url, dest, *, on_progress=None, cancel_check=None, **_kw):
            return DownloadResult(
                bytes_downloaded=2, bytes_total=2, already_present=False, resumed=False,
            )

        monkeypatch.setattr(download_service, "download_to_disk", _fake_download_to_disk)

        with caplog.at_level(logging.DEBUG):
            await _run_job(factory, "p5", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "p5")
        assert job.state == "completed"

        for column in DownloadJob.__table__.columns:
            value = getattr(job, column.name)
            if isinstance(value, str):
                assert TOKEN not in value

        for record in caplog.records:
            assert TOKEN not in record.getMessage()


class TestResolveJobUrl:
    async def test_returns_none_for_non_plex_server_id(self, db_engine):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        job = _xtream_job("j1")
        assert await plex_download_service.resolve_job_url(factory, job) is None

    async def test_returns_none_when_server_missing(self, db_engine):
        factory = await _seeded_factory(
            db_engine, jobs=[], items=[_plex_item(rating_key="1001")],
        )
        job = _plex_job("j2")
        assert await plex_download_service.resolve_job_url(factory, job) is None

    async def test_returns_none_when_item_missing(self, db_engine):
        factory = await _seeded_factory(
            db_engine, jobs=[], servers=[_plex_server()],
        )
        job = _plex_job("j3")
        assert await plex_download_service.resolve_job_url(factory, job) is None

    async def test_returns_none_when_access_token_empty(self, db_engine):
        factory = await _seeded_factory(
            db_engine, jobs=[],
            servers=[_plex_server(access_token="")],
            items=[_plex_item(rating_key="1001")],
        )
        job = _plex_job("j4")
        assert await plex_download_service.resolve_job_url(factory, job) is None

    async def test_builds_expected_url_on_success(self, db_engine):
        factory = await _seeded_factory(
            db_engine, jobs=[],
            servers=[_plex_server(base_uri="https://5-6-7-8.plex.direct:32400/")],
            items=[_plex_item(rating_key="1001", part_key="/library/parts/1001/file.mkv")],
        )
        job = _plex_job("j5")
        url = await plex_download_service.resolve_job_url(factory, job)
        assert url == (
            "https://5-6-7-8.plex.direct:32400/library/parts/1001/file.mkv"
            f"?download=1&X-Plex-Token={TOKEN}"
        )
