"""SSRF guard (CR-S08) coverage for `app.plex_generator.storage`'s image
downloader — poster/fanart URLs are provider-controlled (TMDB/OMDb scrape
results), same threat class as an Xtream stream URL's redirect target.

`_download_sync` runs in the `_image_pool` worker thread, never on the
event loop, so these tests call it directly (sync) rather than through
`asyncio` — mirrors how `download_image`/`submit_image_download` actually
invoke it.
"""
from __future__ import annotations

import socket

import httpx
import pytest

from app.plex_generator import storage as storage_mod
from app.plex_generator.storage import LocalStorage
from app.utils import ssrf


@pytest.fixture(autouse=True)
def _clear_ssrf_cache():
    ssrf.clear_cache()
    yield
    ssrf.clear_cache()


def _addrinfo_for(*ips: str) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


class TestDownloadImageRejectsPrivateTargets:
    def test_download_image_returns_false_for_private_host_and_writes_nothing(
        self, tmp_path, monkeypatch, caplog,
    ):
        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("10.0.0.5")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)

        storage = LocalStorage(tmp_path)
        rel = "Films/Foo (2020)/poster.jpg"
        ok = storage.download_image(rel, "http://10.0.0.5/poster.jpg")

        assert ok is False
        assert not (tmp_path / rel).exists()

    def test_download_sync_pre_check_runs_before_any_http_call(self, tmp_path, monkeypatch):
        """`assert_public_host_sync` must reject BEFORE `_get_image_client`
        ever opens a connection. Calls the static `_download_sync` directly
        (not through `download_image`, whose broad `except Exception` would
        swallow the distinction) so the raised `SsrfBlockedError` is
        observable, and counts `httpx.Client.get` invocations to prove the
        HTTP call itself never happened."""

        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("169.254.169.254")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)

        calls = {"n": 0}

        def _track_get(self, url, *args, **kwargs):
            calls["n"] += 1
            return httpx.Response(200, content=b"x", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", _track_get)

        full = tmp_path / "poster.jpg"
        with pytest.raises(ssrf.SsrfBlockedError):
            LocalStorage._download_sync(full, "http://169.254.169.254/latest/meta-data/poster.jpg")

        assert calls["n"] == 0, "the HTTP GET must never happen for a rejected host"
        assert not full.exists()

    def test_download_image_allows_public_host(self, tmp_path, monkeypatch):
        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("93.184.216.34")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)

        def _fake_get(self, url, *args, **kwargs):
            return httpx.Response(200, content=b"fake-jpg-bytes", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", _fake_get)

        storage = LocalStorage(tmp_path)
        rel = "Films/Baz (2018)/poster.jpg"
        ok = storage.download_image(rel, "http://93.184.216.34/poster.jpg")

        assert ok is True
        assert (tmp_path / rel).read_bytes() == b"fake-jpg-bytes"


class TestImageClientHasVetRequestHook:
    def test_get_image_client_registers_vet_request_sync_hook(self):
        # Force a fresh per-thread client so this test doesn't depend on
        # whatever a previous test happened to leave cached.
        storage_mod._thread_local.http_client = None
        client = storage_mod._get_image_client()
        try:
            assert ssrf.vet_request_sync in client.event_hooks.get("request", [])
        finally:
            client.close()
            storage_mod._thread_local.http_client = None

    def test_image_client_hook_blocks_a_direct_request_to_a_private_host(self, monkeypatch):
        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("10.0.0.5")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        storage_mod._thread_local.http_client = None
        client = storage_mod._get_image_client()
        try:
            with pytest.raises(ssrf.SsrfBlockedError):
                client.get("http://10.0.0.5/poster.jpg")
        finally:
            client.close()
            storage_mod._thread_local.http_client = None
