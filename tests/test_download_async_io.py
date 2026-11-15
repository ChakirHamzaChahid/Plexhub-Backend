"""Tests for non-blocking disk I/O in download_to_disk (CR-A03)."""
from __future__ import annotations

import asyncio
import socket

import errno

import httpx
import pytest
import respx

from app.services import download_service
from app.services.download_service import DownloadCanceled, DownloadTransientError
from app.utils import ssrf


async def test_download_to_disk_offloads_file_io_to_thread(tmp_path, monkeypatch):
    """CR-A03: open/write/flush+close must be offloaded via asyncio.to_thread."""
    ssrf.clear_cache()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )

    real_to_thread = asyncio.to_thread
    thread_calls: list[str] = []

    async def _spy_to_thread(func, /, *args, **kwargs):
        thread_calls.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(download_service.asyncio, "to_thread", _spy_to_thread)

    dest = tmp_path / "video.mp4"
    with respx.mock() as router:
        router.get("http://cdn.example/video.mp4").mock(
            return_value=httpx.Response(200, content=b"chunk1-chunk2")
        )
        res = await download_service.download_to_disk("http://cdn.example/video.mp4", dest)

    assert res.bytes_downloaded == len(b"chunk1-chunk2")
    assert dest.read_bytes() == b"chunk1-chunk2"
    assert "open" in thread_calls
    assert "write" in thread_calls
    assert "_flush_and_close" in thread_calls


async def test_download_to_disk_closes_file_on_cancel(tmp_path, monkeypatch):
    """CR-A03: file handle must be closed via _flush_and_close when canceled mid-stream."""
    ssrf.clear_cache()
    closed = {"flag": False}
    real_open = open

    class _TrackedFile:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            return self._fh.write(data)

        def flush(self):
            return self._fh.flush()

        def close(self):
            closed["flag"] = True
            return self._fh.close()

    monkeypatch.setattr(
        download_service,
        "open",
        lambda *a, **kw: _TrackedFile(real_open(*a, **kw)),
        raising=False,
    )

    async def _cancel():
        return True

    dest = tmp_path / "cancel.mp4"
    with respx.mock() as router:
        router.get("http://cdn.example/cancel.mp4").mock(
            return_value=httpx.Response(200, content=b"abc")
        )
        with pytest.raises(DownloadCanceled):
            await download_service.download_to_disk(
                "http://cdn.example/cancel.mp4", dest, cancel_check=_cancel,
            )

    assert closed["flag"] is True


async def test_download_to_disk_closes_file_when_flush_raises_oserror(tmp_path, monkeypatch):
    """CR-A03: fh.close() must still run even if fh.flush() raises OSError (e.g. ENOSPC)."""
    ssrf.clear_cache()
    closed = {"flag": False}
    real_open = open

    class _FailingFlushFile:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            return self._fh.write(data)

        def flush(self):
            raise OSError(errno.ENOSPC, "No space left on device")

        def close(self):
            closed["flag"] = True
            return self._fh.close()

    monkeypatch.setattr(
        download_service,
        "open",
        lambda *a, **kw: _FailingFlushFile(real_open(*a, **kw)),
        raising=False,
    )

    dest = tmp_path / "enospc.mp4"
    with respx.mock() as router:
        router.get("http://cdn.example/enospc.mp4").mock(
            return_value=httpx.Response(200, content=b"abc")
        )
        with pytest.raises(DownloadTransientError):
            await download_service.download_to_disk("http://cdn.example/enospc.mp4", dest)

    assert closed["flag"] is True
