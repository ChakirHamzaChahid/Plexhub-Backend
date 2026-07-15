"""Security cross-cutting tests (PH-DL-07 priority 2):
  - the upstream Xtream URL (embeds user/password) is NEVER persisted, logged,
    or rendered — DL-110..113 (docs/40-testplan-media-download.md);
  - auth rejection: `/admin/downloads*` (Basic Auth) and `/api/admin/downloads*`
    (`verify_master_key`, master secret ONLY — not a per-user key) — DL-002,
    DL-091, DL-092.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db import database as db_module
from app.models.database import DownloadJob, XtreamAccount
from app.services import api_key_service
from app.utils.server_id import build_server_id
from app.workers import download_worker
from app.workers.download_worker import _run_job

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

FAKE_USER = "s3cr3t_user"
FAKE_PASS = "s3cr3t_pass"
MASTER_KEY = "master-secret-download-security"
ADMIN_USER = "admin"
ADMIN_PASS = "admin-pass-download-security"


def _account(account_id: str = "acc1") -> XtreamAccount:
    return XtreamAccount(
        id=account_id, label="Compte", base_url="http://provider.example", port=80,
        username=FAKE_USER, password=FAKE_PASS, is_active=True, created_at=0,
    )


def _job(job_id: str, *, server_id: str, rating_key: str, dest_path: str, state: str = "queued") -> DownloadJob:
    from app.utils.time import now_ms

    return DownloadJob(
        id=job_id, batch_id=None, server_id=server_id, rating_key=rating_key,
        media_type="movie", unification_id=None, title="Secret Film",
        season=None, episode=None, dest_path=dest_path, state=state,
        bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )


# ─── DL-110: _safe_error never contains a URL/credentials substring ────────


class TestSafeErrorNeverLeaksUrl:
    @pytest.mark.parametrize(
        "raw_message",
        [
            f"Connection to http://{FAKE_USER}:{FAKE_PASS}@host/movie/{FAKE_USER}/{FAKE_PASS}/1.mkv failed",
            f"httpx.ConnectError: [Errno 111] host=provider.example user={FAKE_USER} pass={FAKE_PASS}",
        ],
    )
    def test_safe_error_strips_nothing_it_didnt_construct_but_is_bounded(self, raw_message):
        """`_safe_error` doesn't scrub arbitrary text — the actual guarantee
        (spec §6.3) is that `download_to_disk` NEVER raises an exception whose
        `str()` embeds the url in the first place (see the class below); this
        test only pins the bounding/cap behaviour of `_safe_error` itself."""
        exc = Exception(raw_message)
        result = download_worker._safe_error(exc)
        assert len(result) <= 200

    def test_typed_exceptions_from_download_to_disk_never_carry_a_url(self):
        """The typed exceptions download_to_disk actually raises carry ONLY
        short, hardcoded messages (spec §5.3 "AUCUN message ne contient url")
        — this pins that contract so a future edit can't silently start
        interpolating the url into one of these messages."""
        from app.services.download_service import DownloadPermanentError, DownloadTransientError

        for exc in (
            DownloadPermanentError("upstream 404"),
            DownloadPermanentError("invalid content-type text/html"),
            DownloadTransientError("upstream 503"),
            DownloadTransientError("network timeout"),
            DownloadTransientError("network error"),
        ):
            assert FAKE_USER not in str(exc)
            assert FAKE_PASS not in str(exc)
            assert download_worker._safe_error(exc) == str(exc)[:200]


# ─── DL-111/DL-113: end-to-end worker run never logs/persists the URL ──────


class TestWorkerNeverLeaksCredentials:
    async def _wire(self, db_engine, monkeypatch, download_dir):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add(_account())
            s.add(_job(
                "job-secret-1", server_id=build_server_id("acc1"),
                rating_key="vod_1.mkv", dest_path="Movies/Secret Film (2020)/Secret Film (2020).mkv",
            ))
            await s.commit()
        return factory

    async def test_permanent_failure_error_column_excludes_credentials(
        self, db_engine, monkeypatch, download_dir, xtream_mock, caplog,
    ):
        factory = await self._wire(db_engine, monkeypatch, download_dir)

        # build_stream_url embeds user/password in the path -> the mocked URL
        # itself carries the "leaked" secret if the code were to misbehave.
        url = f"http://provider.example:80/movie/{FAKE_USER}/{FAKE_PASS}/1.mkv"
        xtream_mock.get(url).mock(return_value=httpx.Response(404))

        with caplog.at_level(logging.DEBUG, logger="plexhub.download.worker"):
            await _run_job(factory, "job-secret-1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "job-secret-1")
        assert job.state == "failed"
        assert job.error is not None
        assert FAKE_USER not in job.error
        assert FAKE_PASS not in job.error
        assert url not in job.error

        for record in caplog.records:
            message = record.getMessage()
            assert FAKE_USER not in message, f"credential leaked in log: {message!r}"
            assert FAKE_PASS not in message, f"credential leaked in log: {message!r}"
            assert url not in message, f"raw stream URL leaked in log: {message!r}"

    async def test_transient_failure_error_column_excludes_credentials(
        self, db_engine, monkeypatch, download_dir, xtream_mock, caplog,
    ):
        monkeypatch.setattr(settings, "DOWNLOAD_MAX_RETRIES", 0)  # fail on first transient hit
        factory = await self._wire(db_engine, monkeypatch, download_dir)

        url = f"http://provider.example:80/movie/{FAKE_USER}/{FAKE_PASS}/1.mkv"
        xtream_mock.get(url).mock(return_value=httpx.Response(503))

        with caplog.at_level(logging.DEBUG, logger="plexhub.download.worker"):
            await _run_job(factory, "job-secret-1", asyncio.Semaphore(1))

        async with factory() as s:
            job = await s.get(DownloadJob, "job-secret-1")
        assert job.state == "failed"
        assert FAKE_USER not in (job.error or "")
        assert FAKE_PASS not in (job.error or "")

        for record in caplog.records:
            message = record.getMessage()
            assert FAKE_USER not in message
            assert FAKE_PASS not in message


# ─── DL-113: schema never carries a full-URL column ────────────────────────


class TestNoUrlColumnOnDownloadModels:
    def test_download_job_has_no_url_column(self):
        from app.models.database import DownloadJob as DJ

        col_names = {c.name for c in DJ.__table__.columns}
        assert not any("url" in name.lower() for name in col_names), col_names
        assert "dest_path" in col_names

    def test_download_batch_has_no_url_column(self):
        from app.models.database import DownloadBatch as DB

        col_names = {c.name for c in DB.__table__.columns}
        assert not any("url" in name.lower() for name in col_names), col_names


# ─── DL-112: HTML fragment / JSON responses never carry the URL ────────────


class TestResponsesNeverExposeUrl:
    @pytest.fixture(autouse=True)
    def _configure(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_API_KEY", MASTER_KEY)
        monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)

    async def test_queue_htmx_fragment_never_contains_credentials(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        monkeypatch.setattr(db_module, "async_session_factory", db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_job(
                "job-visible-1", server_id=build_server_id("acc1"),
                rating_key="vod_1.mkv", dest_path="Movies/Secret Film (2020)/Secret Film (2020).mkv",
                state="running",
            ))
            await s.commit()

        resp = await api_client.get("/admin/downloads/queue", auth=(ADMIN_USER, ADMIN_PASS))
        assert resp.status_code == 200
        assert FAKE_USER not in resp.text
        assert FAKE_PASS not in resp.text
        assert "provider.example" not in resp.text

    async def test_json_list_never_contains_credentials(
        self, api_client, monkeypatch, db_factory, download_dir,
    ):
        monkeypatch.setattr(db_module, "async_session_factory", db_factory)
        async with db_factory() as s:
            s.add(_account())
            s.add(_job(
                "job-visible-2", server_id=build_server_id("acc1"),
                rating_key="vod_1.mkv", dest_path="Movies/Secret Film (2020)/Secret Film (2020).mkv",
            ))
            await s.commit()

        resp = await api_client.get(
            "/api/admin/downloads", headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        body_text = resp.text
        assert FAKE_USER not in body_text
        assert FAKE_PASS not in body_text
        assert "provider.example" not in body_text


# ─── DL-002 / DL-091 / DL-092: auth rejection ──────────────────────────────


class TestAdminDownloadsRequiresBasicAuth:
    """DL-002: `/admin/downloads` is Basic-Auth gated like the rest of `/admin`
    (`app.main` mounts `admin_downloads.router` with
    `dependencies=[Depends(verify_admin_basic_auth)]`)."""

    @pytest.fixture(autouse=True)
    def _admin_creds(self, monkeypatch):
        monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)

    async def test_index_401_without_credentials(self, api_client):
        resp = await api_client.get("/admin/downloads")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate", "").lower().startswith("basic")
        assert "downloads-queue" not in resp.text  # no fragment rendered

    async def test_index_401_with_wrong_credentials(self, api_client):
        resp = await api_client.get("/admin/downloads", auth=(ADMIN_USER, "wrong"))
        assert resp.status_code == 401

    async def test_queue_fragment_401_without_credentials(self, api_client):
        resp = await api_client.get("/admin/downloads/queue")
        assert resp.status_code == 401

    async def test_enqueue_401_without_credentials(self, api_client):
        resp = await api_client.post(
            "/admin/downloads",
            data={"type": "movie", "unification_id": "x", "scope": "movie",
                  "source": "xtream_a|vod_1.mkv"},
        )
        assert resp.status_code == 401

    async def test_index_200_with_correct_credentials(self, api_client, monkeypatch, db_factory):
        monkeypatch.setattr(db_module, "async_session_factory", db_factory)
        resp = await api_client.get("/admin/downloads", auth=(ADMIN_USER, ADMIN_PASS))
        assert resp.status_code == 200


class TestApiAdminDownloadsRequiresMasterKey:
    """DL-091/DL-092: `/api/admin/downloads` is guarded module-level by
    `verify_master_key` (Pattern C, same as `api_keys.py`) — it accepts ONLY
    `settings.AI_API_KEY`, never a per-user `api_keys` row (unlike
    `verify_backend_secret`, which accepts either)."""

    @pytest.fixture(autouse=True)
    def _master_key(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_API_KEY", MASTER_KEY)

    async def test_list_401_without_key(self, api_client):
        resp = await api_client.get("/api/admin/downloads")
        assert resp.status_code == 401

    async def test_list_401_with_wrong_key(self, api_client):
        resp = await api_client.get(
            "/api/admin/downloads", headers={"X-API-Key": "definitely-wrong"},
        )
        assert resp.status_code == 401

    async def test_list_not_401_with_master_key(self, api_client, monkeypatch, db_factory):
        monkeypatch.setattr(db_module, "async_session_factory", db_factory)
        resp = await api_client.get(
            "/api/admin/downloads", headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code != 401

    async def test_list_401_with_a_valid_but_non_master_per_user_key(
        self, api_client, monkeypatch, db_factory,
    ):
        """A genuinely active per-user key (the kind `verify_backend_secret`
        happily accepts on `/api/media`, `/api/accounts`, etc.) must still be
        REJECTED here — key-scoped download visibility/mutation is
        master-only by design (spec §7.2)."""
        monkeypatch.setattr(db_module, "async_session_factory", db_factory)
        # `api_key_service.resolve()` (used by `verify_backend_secret`) opens
        # its OWN session via a module-level `async_session_factory` import
        # (`app.services.api_key_service`, not `app.db.database`) — must be
        # patched on that module too, same gotcha as `test_api_key_service.py`.
        monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
        async with db_factory() as s:
            _row, plaintext = await api_key_service.create_key(s, label="Per-user")

        # Sanity: this per-user key DOES authenticate on a verify_backend_secret
        # router (proves it's genuinely active, not just malformed).
        resp_ok = await api_client.get(
            "/api/accounts", headers={"X-API-Key": plaintext},
        )
        assert resp_ok.status_code != 401

        resp = await api_client.get(
            "/api/admin/downloads", headers={"X-API-Key": plaintext},
        )
        assert resp.status_code == 401

    async def test_get_by_id_401_without_key(self, api_client):
        resp = await api_client.get("/api/admin/downloads/some-job-id")
        assert resp.status_code == 401


class TestDownloadRedirectSSRF:
    """DL-01: downloads follow a provider→CDN 302, but only to PUBLIC targets.

    Regression guard for "all downloads fail (upstream 302)": real Xtream
    providers 302 stream URLs to their CDN, so `follow_redirects=False` +
    "any 3xx is permanent failure" broke every download. We now follow, but a
    redirect to an internal address is still rejected, never fetched.
    """

    async def test_assert_public_redirect_host_allows_public_ip(self):
        from app.services.download_service import _assert_public_redirect_host
        # IP literals resolve to themselves — public ones must not raise.
        await _assert_public_redirect_host("103.176.90.57")
        await _assert_public_redirect_host("1.1.1.1")

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "10.0.0.1", "192.168.1.10", "172.16.0.1",
         "169.254.169.254", "0.0.0.0", ""],
    )
    async def test_assert_public_redirect_host_rejects_internal(self, host):
        from app.services.download_service import (
            _assert_public_redirect_host, DownloadPermanentError,
        )
        with pytest.raises(DownloadPermanentError):
            await _assert_public_redirect_host(host)

    # End-to-end redirect-follow behaviour (public followed, private rejected,
    # strict when disabled) is covered against the real download_to_disk streaming
    # path in tests/test_download_transfer.py::TestSafeRedirectFollow.
