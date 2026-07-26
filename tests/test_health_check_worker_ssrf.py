"""SSRF guard (CR-S08) coverage for `health_check_worker`'s pooled client.

`_check_one`'s existing classification tests (`tests/test_health_check_worker.py`)
build their OWN bare `httpx.AsyncClient()` against a non-resolvable host
(`http://acct.test`) — they never go through `hc._get_client()`, so they are
untouched by the new `vet_request` hook. These tests exercise the REAL
singleton (per the design brief) so the hook itself is proven wired in,
with `socket.getaddrinfo` patched instead of hitting real DNS/network.
"""
from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

from app.utils import ssrf
from app.workers import health_check_worker as hc


def _addrinfo_for(*ips: str) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


def _fake_account() -> SimpleNamespace:
    return SimpleNamespace(base_url="http://ssrf-acct.test", port=80, username="u", password="p")


def _movie_item(stream_id: str = "1") -> SimpleNamespace:
    return SimpleNamespace(rating_key=f"vod_{stream_id}.mp4")


@pytest.fixture(autouse=True)
async def _fresh_client_singleton():
    """`hc._client` is a module-level singleton — reset it around each test
    so the client actually picking up (or not picking up) the SSRF hook in
    this file never bleeds into any other test module."""
    hc._client = None
    ssrf.clear_cache()
    yield
    await hc.close()
    ssrf.clear_cache()


class TestGetClientHasVetRequestHook:
    async def test_get_client_registers_the_ssrf_hook(self):
        client = await hc._get_client()
        assert hc.vet_request in client.event_hooks.get("request", [])


class TestCheckOneRejectsUnsafeHost:
    async def test_private_host_is_classified_unsafe_and_definitive(self, monkeypatch, xtream_mock):
        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("10.0.0.9")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        # No respx route needed — the request never actually reaches the
        # (mocked) transport; the hook raises first. If it somehow didn't,
        # respx would raise its own "no route" error, which is NOT what this
        # test expects, so the assertion below double-checks classification.

        client = await hc._get_client()
        _, is_broken, reason, size = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

        assert is_broken is True
        assert reason == "unsafe_host"
        assert size is None
        assert hc._is_definitive_failure(reason) is True

    async def test_metadata_ip_target_is_rejected(self, monkeypatch):
        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("169.254.169.254")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)

        client = await hc._get_client()
        _, is_broken, reason, _size = await hc._check_one(
            client, _movie_item("2"), _fake_account(), asyncio.Semaphore(1)
        )
        assert is_broken is True
        assert reason == "unsafe_host"

    async def test_public_host_still_classified_normally(self, monkeypatch, xtream_mock):
        """Regression guard: wiring the hook in must not break a normal,
        legitimately-public probe — `_check_one`'s HEAD/GET classification
        keeps working end to end through the real singleton."""

        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("93.184.216.34")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)

        url = "http://ssrf-acct.test:80/movie/u/p/1.mp4"
        xtream_mock.head(url).respond(
            200, headers={"content-type": "video/mp4", "content-length": "999"}
        )

        client = await hc._get_client()
        _, is_broken, reason, size = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

        assert is_broken is False
        assert reason == "head_ct_video"
        assert size == 999
