"""Shared SSRF guard (CR-S08): a single "is this host safe to connect to"
predicate, reused across every outbound HTTP client that follows a
server-supplied or provider-supplied URL (physical downloads, the DAV
relay, library image downloads, stream health-checks).

Originally this guard lived only in `app.services.download_service`
(`_assert_public_redirect_host`, DL-01) and was applied to exactly two
call sites (the manual redirect loop in `download_to_disk` and the DAV
relay's own redirect-follow). Two more surfaces fetch provider-controlled
URLs without any vetting at all: library poster/fanart downloads
(`app.plex_generator.storage`) and stream validation HEAD/GET probes
(`app.workers.health_check_worker`). This module extracts the predicate so
all four surfaces share one implementation instead of four independent
(and driftable) copies.

What this closes: a provider (Xtream category/VOD entry, TMDB/OMDb-sourced
poster URL, or a 3xx `Location` header from any of the above) pointing the
backend at an internal address — loopback, RFC1918, link-local (including
the `169.254.169.254` cloud metadata endpoint), other IANA-reserved
ranges, or multicast. Every address a hostname resolves to must be public
for the host to be allowed — a DNS name that answers with a mix of public
and private A/AAAA records is rejected outright (closes the "return one
public + one private record" bypass; a naive "first record only" check
would not).

Residual caveat — DNS rebinding (TOCTOU), NOT closed by this module: this
validates the hostname's *resolution at guard time*. httpx re-resolves the
same hostname independently when it actually opens the connection, a
moment later. A DNS server that answers this guard's lookup with a public
IP and then flips to a private one for httpx's own connect would still
get through. Closing that would require a resolve-then-connect-on-a-
pinned-IP custom transport, which this stack does not implement. This is a
far more involved attack than the plain "provider 302s us to 127.0.0.1"
case this guard exists for, and is accepted as out of scope for a
self-hosted puller against an operator-chosen provider — same tradeoff the
original DL-01 guard already documented.

`SSRF_DNS_CACHE_SECONDS` (default 60) caches the public/private verdict
per hostname so the stream-validation worker (thousands of probes against
a handful of provider hostnames) doesn't pay a fresh DNS round-trip per
probe. Widening that cache window is a throughput tradeoff, NOT a security
improvement — it makes the TOCTOU window above proportionally wider, not
narrower. `SSRF_ALLOW_PRIVATE_HOSTS` (default empty) is an explicit,
operator-opted-in escape hatch for a self-hosted provider that
legitimately lives on a private address; nothing in this codebase sets it,
and the house default keeps every private range blocked.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Optional

import httpx

from app.config import settings

# Fixed, host/URL-free message — an Xtream stream URL or a redirect Location
# can embed account credentials in its path/query, so nothing derived from
# the checked host or URL is ever allowed into this exception's message.
_BLOCKED_MESSAGE = "unsafe host (SSRF guard)"


class SsrfBlockedError(Exception):
    """Raised when a host is not safe to connect to. Message is always the
    fixed, host/URL-free `_BLOCKED_MESSAGE` above — callers must not
    interpolate the offending host/url into a re-raised message either."""


# host -> (is_public, expires_at_monotonic). Module-level so the verdict is
# shared across every caller (download_service, dav.relay, storage,
# health_check_worker) within a process.
_verdict_cache: dict[str, tuple[bool, float]] = {}


def clear_cache() -> None:
    """Drop every cached verdict. Tests use this to make sure a
    `socket.getaddrinfo` monkeypatch for a previously-seen host actually
    takes effect instead of returning a stale cached verdict."""
    _verdict_cache.clear()


def _allow_listed_hosts() -> frozenset[str]:
    # Re-read from `settings` on every call (not cached at import time) so
    # tests can monkeypatch `settings.SSRF_ALLOW_PRIVATE_HOSTS` and see the
    # effect immediately.
    raw = settings.SSRF_ALLOW_PRIVATE_HOSTS or ""
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _is_allow_listed(host: str) -> bool:
    return host.lower() in _allow_listed_hosts()


def _addrinfo_all_public(infos: list) -> bool:
    """Every address a hostname resolves to must be public. A single
    private/reserved address anywhere in the result set fails the whole
    host — this is what blocks the "one public + one private A record"
    bypass."""
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return False
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return False
    return True


def _resolve_is_public(host: str) -> bool:
    """Blocking DNS resolution + verdict. A resolution failure is
    fail-closed (treated as NOT public) — an unresolvable host is refused
    rather than silently let through."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return _addrinfo_all_public(infos)


def _is_public_cached(host: str) -> bool:
    ttl_seconds = settings.SSRF_DNS_CACHE_SECONDS
    now = time.monotonic()
    if ttl_seconds > 0:
        cached = _verdict_cache.get(host)
        if cached is not None and now < cached[1]:
            return cached[0]
    result = _resolve_is_public(host)
    if ttl_seconds > 0:
        _verdict_cache[host] = (result, now + ttl_seconds)
    return result


def assert_public_host_sync(host: Optional[str]) -> None:
    """Raise `SsrfBlockedError` unless `host` is allow-listed or every
    address it resolves to is a public, routable IP. Blocking (DNS
    resolution + `ipaddress` checks only) — safe to call from a worker
    thread, never from the event loop. See `assert_public_host` for the
    async wrapper."""
    if not host:
        raise SsrfBlockedError(_BLOCKED_MESSAGE)
    if _is_allow_listed(host):
        return
    if not _is_public_cached(host):
        raise SsrfBlockedError(_BLOCKED_MESSAGE)


async def assert_public_host(host: Optional[str]) -> None:
    """Async wrapper around `assert_public_host_sync`. The actual DNS
    resolution is blocking (`socket.getaddrinfo`), so it is offloaded via
    `asyncio.to_thread` — never call `assert_public_host_sync` directly
    from a coroutine running on the event loop."""
    if not host:
        raise SsrfBlockedError(_BLOCKED_MESSAGE)
    if _is_allow_listed(host):
        return
    is_public = await asyncio.to_thread(_is_public_cached, host)
    if not is_public:
        raise SsrfBlockedError(_BLOCKED_MESSAGE)


# --- httpx event hooks --------------------------------------------------------
#
# httpx invokes `event_hooks["request"]` callbacks once per request — INCLUDING
# once per followed redirect hop, before that hop is sent — so registering
# these on a client built with `follow_redirects=True` vets the initial
# request AND every hop it's redirected through, with no per-call-site
# change needed. Verified against httpx 0.28's `_send_handling_redirects`.


def vet_request_sync(request: httpx.Request) -> None:
    """Sync `event_hooks["request"]` hook for `httpx.Client`. Raises
    `SsrfBlockedError` (never mentions the host/URL) if the request target
    is not a public host."""
    assert_public_host_sync(request.url.host)


async def vet_request(request: httpx.Request) -> None:
    """Async `event_hooks["request"]` hook for `httpx.AsyncClient`. Raises
    `SsrfBlockedError` (never mentions the host/URL) if the request target
    is not a public host."""
    await assert_public_host(request.url.host)
