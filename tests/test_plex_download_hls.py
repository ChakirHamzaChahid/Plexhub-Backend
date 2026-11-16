"""Tests for Lot 11 T11.2 — Plex HLS transcode fallback + atomic ffmpeg remux."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.services import download_service, plex_download_service
from app.services.download_service import DownloadCanceled, DownloadPermanentError
from app.utils.server_id import build_plex_server_id


def test_build_hls_transcode_url_valid_and_invalid(monkeypatch):
    monkeypatch.setattr(settings, "PLEX_CLIENT_IDENTIFIER", "client-uuid-1")
    url = plex_download_service.build_hls_transcode_url(
        "https://1-2-3-4.plex.direct:32400/",
        "secret-plex-token",
        "12345",
    )
    assert url is not None
    assert url.startswith("https://1-2-3-4.plex.direct:32400/video/:/transcode/universal/start.m3u8?")
    assert "protocol=hls" in url
    assert "path=%2Flibrary%2Fmetadata%2F12345" in url
    assert "X-Plex-Token=secret-plex-token" in url

    assert plex_download_service.build_hls_transcode_url("file:///etc/passwd", "tok", "123") is None
    assert plex_download_service.build_hls_transcode_url("https://plex.example", "", "123") is None
    assert plex_download_service.build_hls_transcode_url("https://plex.example", "tok", "123?evil=1") is None


async def test_resolve_hls_fallback_url_respects_feature_flag(monkeypatch):
    job = SimpleNamespace(server_id=build_plex_server_id("srv1"), rating_key="42")

    monkeypatch.setattr(settings, "PLEX_HLS_FALLBACK_ENABLED", False)
    assert await plex_download_service.resolve_hls_fallback_url(None, job) is None

    monkeypatch.setattr(settings, "PLEX_HLS_FALLBACK_ENABLED", True)
    fake_server = SimpleNamespace(
        base_uri="https://1-2-3-4.plex.direct:32400",
        access_token="tok-xyz",
    )

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, model, pk):
            return fake_server

    url = await plex_download_service.resolve_hls_fallback_url(lambda: _FakeSession(), job)
    assert url is not None
    assert "start.m3u8" in url
    assert "X-Plex-Token=tok-xyz" in url


async def test_download_to_disk_403_triggers_hls_fallback_when_enabled(tmp_path: Path, respx_mock):
    dest = tmp_path / "Movie.mp4"
    respx_mock.get("https://1-2-3-4.plex.direct:32400/library/parts/1/file.mp4?download=1&X-Plex-Token=tok").mock(
        return_value=httpx.Response(403)
    )
    respx_mock.get("https://1-2-3-4.plex.direct:32400/library/parts/1/file.mp4?X-Plex-Token=tok").mock(
        return_value=httpx.Response(403)
    )

    hls_url = "https://1-2-3-4.plex.direct:32400/video/:/transcode/universal/start.m3u8?X-Plex-Token=tok"

    async def _fake_exec(*cmd, **kwargs):
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"remuxed-mp4-bytes")
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    with (
        patch.object(download_service, "_assert_public_redirect_host", new=AsyncMock(return_value=None)),
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec) as mock_exec,
    ):
        result = await download_service.download_to_disk(
            "https://1-2-3-4.plex.direct:32400/library/parts/1/file.mp4?download=1&X-Plex-Token=tok",
            dest,
            fallback_urls=[
                "https://1-2-3-4.plex.direct:32400/library/parts/1/file.mp4?X-Plex-Token=tok",
            ],
            hls_fallback_url=hls_url,
        )

    assert result.bytes_downloaded == len(b"remuxed-mp4-bytes")
    assert dest.is_file()
    assert dest.read_bytes() == b"remuxed-mp4-bytes"
    assert not (tmp_path / "Movie.mp4.hls.part").exists()

    called_cmd = list(mock_exec.call_args.args)
    assert "-protocol_whitelist" in called_cmd
    wl_idx = called_cmd.index("-protocol_whitelist")
    assert called_cmd[wl_idx + 1] == "http,https,tcp,tls,crypto"
    assert "-f" in called_cmd
    assert "mp4" in called_cmd


async def test_download_to_disk_403_without_hls_fallback_still_fails(tmp_path: Path, respx_mock):
    dest = tmp_path / "Movie.mkv"
    respx_mock.get("https://1-2-3-4.plex.direct:32400/library/parts/1/file.mkv?download=1&X-Plex-Token=tok").mock(
        return_value=httpx.Response(403)
    )
    respx_mock.get("https://1-2-3-4.plex.direct:32400/library/parts/1/file.mkv?X-Plex-Token=tok").mock(
        return_value=httpx.Response(403)
    )

    with patch.object(download_service, "_assert_public_redirect_host", new=AsyncMock(return_value=None)):
        with pytest.raises(DownloadPermanentError, match="upstream 403"):
            await download_service.download_to_disk(
                "https://1-2-3-4.plex.direct:32400/library/parts/1/file.mkv?download=1&X-Plex-Token=tok",
                dest,
                fallback_urls=[
                    "https://1-2-3-4.plex.direct:32400/library/parts/1/file.mkv?X-Plex-Token=tok",
                ],
                hls_fallback_url=None,
            )


async def test_download_hls_to_disk_blocks_private_ip_ssrf(tmp_path: Path):
    dest = tmp_path / "Blocked.mkv"
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        with pytest.raises(DownloadPermanentError):
            await download_service.download_hls_to_disk(
                "http://127.0.0.1:32400/video/:/transcode/universal/start.m3u8?X-Plex-Token=secret",
                dest,
            )
        mock_exec.assert_not_called()


async def test_download_hls_to_disk_ffmpeg_failure_cleans_tmp_and_hides_token(tmp_path: Path):
    dest = tmp_path / "Fail.mkv"
    hls_url = "https://1-2-3-4.plex.direct:32400/video/:/transcode/universal/start.m3u8?X-Plex-Token=TOPSECRET"

    async def _failing_exec(*cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial")
        proc = MagicMock()
        proc.returncode = 1
        proc.wait = AsyncMock(return_value=1)
        return proc

    with (
        patch.object(download_service, "_assert_public_redirect_host", new=AsyncMock(return_value=None)),
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("asyncio.create_subprocess_exec", side_effect=_failing_exec),
    ):
        with pytest.raises(DownloadPermanentError) as exc_info:
            await download_service.download_hls_to_disk(hls_url, dest)

    assert "TOPSECRET" not in str(exc_info.value)
    assert not dest.exists()
    assert not (tmp_path / "Fail.mkv.hls.part").exists()


async def test_download_hls_to_disk_cancel_terminates_ffmpeg_and_cleans_tmp(tmp_path: Path):
    dest = tmp_path / "Canceled.mkv"
    hls_url = "https://1-2-3-4.plex.direct:32400/video/:/transcode/universal/start.m3u8?X-Plex-Token=tok"

    proc = MagicMock()
    proc.returncode = None
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=-15)

    async def _fake_exec(*cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial")
        return proc

    with (
        patch.object(download_service, "_assert_public_redirect_host", new=AsyncMock(return_value=None)),
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        with pytest.raises(DownloadCanceled):
            await download_service.download_hls_to_disk(
                hls_url, dest, cancel_check=AsyncMock(return_value=True),
            )

    proc.terminate.assert_called_once()
    assert not dest.exists()
    assert not (tmp_path / "Canceled.mkv.hls.part").exists()
