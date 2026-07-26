"""`app/utils/ssrf.py` — the shared SSRF guard (CR-S08).

Covers the predicate itself (blocking IP ranges, allow-list, mixed-A-record
rejection, unresolvable-host fail-closed, verdict caching) and the httpx
`event_hooks["request"]` wrappers used by the four call sites
(`download_service`, `dav.relay`, `plex_generator.storage`,
`health_check_worker`).
"""
from __future__ import annotations

import socket

import httpx
import pytest

from app.config import settings
from app.utils import ssrf


@pytest.fixture(autouse=True)
def _clear_ssrf_cache():
    """Every test starts and ends with an empty verdict cache — hosts are
    reused (as literal IP strings) across tests/files, and a cached verdict
    from an unrelated test must never leak in."""
    ssrf.clear_cache()
    yield
    ssrf.clear_cache()


def _addrinfo_for(*ips: str) -> list[tuple]:
    """Build a fake `socket.getaddrinfo` return value for the given IPs
    (mirrors the real 5-tuple shape; only `info[4][0]` is ever read)."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
        for ip in ips
    ]


# ─── Blocking: private/loopback/link-local/reserved/multicast ─────────────


class TestBlocksInternalAddresses:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.10",
            "172.16.0.1",
            "169.254.169.254",  # cloud metadata
            "::1",
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "0.0.0.0",
            "224.0.0.1",  # multicast
        ],
    )
    def test_rejects_internal_ip_literal_sync(self, host):
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync(host)

    @pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "169.254.169.254"])
    async def test_rejects_internal_ip_literal_async(self, host):
        with pytest.raises(ssrf.SsrfBlockedError):
            await ssrf.assert_public_host(host)

    def test_rejects_empty_host(self):
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync("")

    def test_rejects_none_host(self):
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync(None)

    def test_exception_message_never_contains_the_host(self):
        secret_host = "10.13.37.99"
        with pytest.raises(ssrf.SsrfBlockedError) as excinfo:
            ssrf.assert_public_host_sync(secret_host)
        assert secret_host not in str(excinfo.value)


class TestAllowsPublicAddresses:
    @pytest.mark.parametrize("host", ["1.1.1.1", "8.8.8.8", "93.184.216.34"])
    def test_allows_public_ip_literal_sync(self, host):
        ssrf.assert_public_host_sync(host)  # must not raise

    async def test_allows_public_ip_literal_async(self):
        await ssrf.assert_public_host("1.1.1.1")  # must not raise


class TestUnresolvableHostFailsClosed:
    def test_dns_failure_is_treated_as_unsafe(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise OSError("name or service not known")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _boom)
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync("does-not-resolve.invalid")


class TestMixedRecordsBlockTheWholeHost:
    """A hostname resolving to a mix of a public AND a private address must
    be rejected outright — a naive "check the first record only" guard
    would let this through."""

    def test_one_private_record_among_public_ones_blocks(self, monkeypatch):
        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("93.184.216.34", "10.0.0.5")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync("mixed.example")

    def test_all_public_records_pass(self, monkeypatch):
        def _fake_getaddrinfo(host, *args, **kwargs):
            return _addrinfo_for("93.184.216.34", "1.1.1.1")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        ssrf.assert_public_host_sync("all-public.example")  # must not raise

    def test_no_records_at_all_blocks(self, monkeypatch):
        def _fake_getaddrinfo(host, *args, **kwargs):
            return []

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync("empty-answer.example")


# ─── Allow-list opt-in ──────────────────────────────────────────────────────


class TestAllowListBypass:
    def test_allow_listed_host_bypasses_resolution_entirely(self, monkeypatch):
        # If the guard tried to resolve this it would fail-closed (OSError) —
        # allow-listing must short-circuit BEFORE any getaddrinfo call.
        def _boom(*args, **kwargs):
            raise AssertionError("getaddrinfo must not be called for an allow-listed host")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _boom)
        monkeypatch.setattr(settings, "SSRF_ALLOW_PRIVATE_HOSTS", "internal.example,other.example")
        ssrf.assert_public_host_sync("internal.example")  # must not raise

    def test_allow_list_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(settings, "SSRF_ALLOW_PRIVATE_HOSTS", "Internal.Example")
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
        ssrf.assert_public_host_sync("internal.example")

    def test_allow_list_is_read_fresh_each_call(self, monkeypatch):
        # Empty by default: a plain private host is blocked...
        monkeypatch.setattr(settings, "SSRF_ALLOW_PRIVATE_HOSTS", "")
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync("10.0.0.9")
        # ...monkeypatching mid-test takes effect immediately (no import-time
        # caching of the allow-list).
        monkeypatch.setattr(settings, "SSRF_ALLOW_PRIVATE_HOSTS", "10.0.0.9")
        ssrf.assert_public_host_sync("10.0.0.9")

    def test_non_allow_listed_host_still_blocked(self, monkeypatch):
        monkeypatch.setattr(settings, "SSRF_ALLOW_PRIVATE_HOSTS", "internal.example")
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync("10.0.0.1")


# ─── DNS verdict cache ──────────────────────────────────────────────────────


class TestVerdictCache:
    def test_second_call_within_ttl_does_not_re_resolve(self, monkeypatch):
        calls = {"n": 0}

        def _fake_getaddrinfo(host, *args, **kwargs):
            calls["n"] += 1
            return _addrinfo_for("1.1.1.1")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        monkeypatch.setattr(settings, "SSRF_DNS_CACHE_SECONDS", 60)

        ssrf.assert_public_host_sync("cached.example")
        ssrf.assert_public_host_sync("cached.example")
        ssrf.assert_public_host_sync("cached.example")
        assert calls["n"] == 1

    def test_cached_negative_verdict_is_also_reused(self, monkeypatch):
        calls = {"n": 0}

        def _fake_getaddrinfo(host, *args, **kwargs):
            calls["n"] += 1
            return _addrinfo_for("10.0.0.1")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        monkeypatch.setattr(settings, "SSRF_DNS_CACHE_SECONDS", 60)

        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync("cached-private.example")
        with pytest.raises(ssrf.SsrfBlockedError):
            ssrf.assert_public_host_sync("cached-private.example")
        assert calls["n"] == 1

    def test_zero_ttl_disables_caching(self, monkeypatch):
        calls = {"n": 0}

        def _fake_getaddrinfo(host, *args, **kwargs):
            calls["n"] += 1
            return _addrinfo_for("1.1.1.1")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        monkeypatch.setattr(settings, "SSRF_DNS_CACHE_SECONDS", 0)

        ssrf.assert_public_host_sync("no-cache.example")
        ssrf.assert_public_host_sync("no-cache.example")
        assert calls["n"] == 2

    def test_clear_cache_forces_re_resolution(self, monkeypatch):
        calls = {"n": 0}

        def _fake_getaddrinfo(host, *args, **kwargs):
            calls["n"] += 1
            return _addrinfo_for("1.1.1.1")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        monkeypatch.setattr(settings, "SSRF_DNS_CACHE_SECONDS", 60)

        ssrf.assert_public_host_sync("clearme.example")
        ssrf.clear_cache()
        ssrf.assert_public_host_sync("clearme.example")
        assert calls["n"] == 2

    def test_cache_expires_after_ttl(self, monkeypatch):
        calls = {"n": 0}

        def _fake_getaddrinfo(host, *args, **kwargs):
            calls["n"] += 1
            return _addrinfo_for("1.1.1.1")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        monkeypatch.setattr(settings, "SSRF_DNS_CACHE_SECONDS", 60)

        fake_time = {"t": 1000.0}
        monkeypatch.setattr(ssrf.time, "monotonic", lambda: fake_time["t"])

        ssrf.assert_public_host_sync("expiring.example")
        fake_time["t"] += 61  # past the 60s TTL
        ssrf.assert_public_host_sync("expiring.example")
        assert calls["n"] == 2


# ─── httpx event hooks ──────────────────────────────────────────────────────


class TestHttpxEventHooks:
    def test_vet_request_sync_blocks_request_to_private_host(self):
        client = httpx.Client(event_hooks={"request": [ssrf.vet_request_sync]})
        try:
            with pytest.raises(ssrf.SsrfBlockedError):
                client.get("http://10.0.0.1/secret")
        finally:
            client.close()

    async def test_vet_request_async_blocks_request_to_private_host(self):
        client = httpx.AsyncClient(event_hooks={"request": [ssrf.vet_request]})
        try:
            with pytest.raises(ssrf.SsrfBlockedError):
                await client.get("http://169.254.169.254/latest/meta-data/")
        finally:
            await client.aclose()

    async def test_vet_request_hook_fires_on_redirect_hop(self, xtream_mock, monkeypatch):
        """The whole point of using an event hook (vs. a single pre-request
        check) is that httpx invokes it once per followed redirect too — a
        client with `follow_redirects=True` must still reject a redirect
        target that resolves to a private address, without any change at the
        call site."""

        def _fake_getaddrinfo(host, *args, **kwargs):
            if host == "cdn.example":
                return _addrinfo_for("93.184.216.34")
            if host == "internal.example":
                return _addrinfo_for("10.0.0.9")
            raise AssertionError(f"unexpected host {host!r}")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)

        xtream_mock.get("http://cdn.example/start").mock(
            return_value=httpx.Response(302, headers={"Location": "http://internal.example/evil"})
        )
        xtream_mock.get("http://internal.example/evil").mock(
            return_value=httpx.Response(200, content=b"should never be reached")
        )

        client = httpx.AsyncClient(
            event_hooks={"request": [ssrf.vet_request]}, follow_redirects=True,
        )
        try:
            with pytest.raises(ssrf.SsrfBlockedError):
                await client.get("http://cdn.example/start")
        finally:
            await client.aclose()
