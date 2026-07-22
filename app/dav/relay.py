"""DAV-1B: byte relay to the Xtream upstream, with HTTP Range support.

The counterpart to `app/dav/throttle.py`'s per-account concurrency gate —
`open_upstream` is the thing actually GATED by a throttle permit (the
router, `app/api/dav.py`, owns acquiring the permit before calling this and
releasing it once `UpstreamStream.body` is fully drained/`aclose`d; this
module itself knows nothing about accounts or the throttle).

Reuses the SAME redirect-following + SSRF guard as
`app.services.download_service.download_to_disk`
(`assert_public_redirect_host`) — a provider's stream URL can legitimately
302 to a CDN host, and that hop must be verified to resolve to a public IP
before being fetched, exactly like a physical download.

Range handling: the client's `Range` header is passed straight through to
the upstream. Some Xtream panels ignore it and answer 200 with the full
file — `_apply_range_shim` (gated by `DAV_RANGE_SHIM`) re-slices that full
body into the requested window and synthesizes a 206, so rclone's chunked
reads never re-download the whole file per chunk. `DAV_RANGE_SHIM=false`
leaves an ignored Range as a raw 200 pass-through instead.

Secrets invariant (mirrors download_service): the upstream URL embeds the
Xtream account's user/password in its path/query. No exception message or
log line in this module ever contains `url`/a redirect `Location` value.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx

from app.config import settings
from app.services.download_service import DownloadPermanentError, assert_public_redirect_host

logger = logging.getLogger("plexhub.dav")

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


# --- Exceptions --------------------------------------------------------------

class UpstreamNotFound(Exception):
    """Upstream answered a 4xx status — the router maps this to a 404."""


class UpstreamError(Exception):
    """Upstream answered a 5xx status, a redirect could not be followed
    (SSRF-rejected target / too many hops / missing Location), or the
    request failed at the transport level (connection refused/reset,
    malformed response, ...) — the router maps this to a 502."""


class UpstreamTimeout(UpstreamError):
    """Upstream connect/read timed out. A distinguishable `UpstreamError`
    subclass: a router that wants a 504 specifically for timeouts can catch
    this first, while one that only cares about "upstream is unwell" can
    still catch the parent `UpstreamError` alone."""


# --- Result shape --------------------------------------------------------------

@dataclass
class UpstreamStream:
    """What `open_upstream` hands back for a servable (200/206/416)
    response. `body` is NOT consumed yet; `aclose` MUST be called exactly
    once by the caller when done with `body` (in a `finally`, so a
    mid-stream DAV-client disconnect still closes the upstream connection —
    same discipline `download_to_disk` applies to its local file handle,
    here applied to a live network stream)."""

    status_code: int  # 200, 206 or 416 — sent to the DAV client verbatim
    headers: dict[str, str]  # Content-Length / Content-Range / Content-Type / Accept-Ranges
    body: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]]


# --- Pooled client (module-level, lazy) ---------------------------------------

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def get_client() -> httpx.AsyncClient:
    """Shared `httpx.AsyncClient`, built once and reused across every
    `open_upstream` call — the relay serves many short-lived GET/HEAD
    requests per mounted-rclone read, so connection pooling/keep-alive
    matters here, unlike `download_to_disk`'s per-transfer client (one
    long-lived request each, opened/closed around a single file). Double-
    checked-locking so concurrent first-callers don't race to build two
    clients. `follow_redirects=False` — this module vets and follows
    redirects itself (SSRF guard), same convention as `download_service`.
    """
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            timeout = httpx.Timeout(
                connect=settings.DAV_CONNECT_TIMEOUT,
                read=settings.DAV_READ_TIMEOUT,
                write=settings.DAV_READ_TIMEOUT,
                pool=settings.DAV_CONNECT_TIMEOUT,
            )
            _client = httpx.AsyncClient(follow_redirects=False, timeout=timeout)
        return _client


async def close_client() -> None:
    """Close the pooled client, if one was ever built. Called from `app.main`'s
    lifespan shutdown (mirrors `xtream_service.close()`/`tmdb_service.close()`
    just above it there) — without this, `_client`'s underlying connection
    pool (sockets/keep-alives to every Xtream provider this process has
    relayed a GET for) is simply abandoned at process exit instead of
    torn down cleanly. Safe to call even if no `/dav` GET was ever served
    (module-level `_client` stays `None`, nothing to close) and safe to call
    more than once (closing an already-closed `httpx.AsyncClient` is a no-op).
    """
    global _client
    async with _client_lock:
        if _client is not None:
            await _client.aclose()
            _client = None


# --- Range parsing / shim ------------------------------------------------------

def _parse_range_header(range_header: str, total: int) -> Optional[tuple[int, int]]:
    """Resolve a single-range `Range: bytes=<spec>` client header against a
    known `total` content length -> inclusive `(start, end)` byte offsets,
    or `None` if unparseable. Supports `start-end`, `start-` (open-ended)
    and `-suffix_len` (last N bytes); a multi-range header (`bytes=0-1,5-6`)
    uses only its FIRST range — Xtream panels/rclone never send multi-range
    requests, so this deliberately doesn't try to serve one.
    """
    if not range_header or "=" not in range_header:
        return None
    unit, _, spec = range_header.partition("=")
    if unit.strip().lower() != "bytes":
        return None
    first_spec = spec.split(",", 1)[0].strip()
    if "-" not in first_spec:
        return None
    start_str, _, end_str = first_spec.partition("-")
    start_str, end_str = start_str.strip(), end_str.strip()

    if start_str == "":
        # Suffix range: "bytes=-500" -> last 500 bytes.
        if not end_str.isdigit():
            return None
        suffix_len = int(end_str)
        if suffix_len <= 0:
            return None
        return (max(0, total - suffix_len), total - 1)

    if not start_str.isdigit():
        return None
    start = int(start_str)
    end = int(end_str) if end_str.isdigit() else total - 1
    return (start, min(end, total - 1))


async def _shim_ranged_body(
    resp: httpx.Response, start: int, end: int, chunk_bytes: int,
) -> AsyncIterator[bytes]:
    """Re-slice a full-body 200 response into `[start, end]` (inclusive)
    when the upstream ignored the client's `Range` header. Reads (and
    discards) every byte before `start`, and keeps draining after `end`
    rather than closing the response early — this is the shim's entire
    cost/tradeoff (see `open_upstream`'s docstring and
    `docs/30-ops-plex-webdav.md`): correctness over upstream bandwidth,
    only paid by providers that don't honour Range at all.
    """
    position = 0
    async for chunk in resp.aiter_bytes(chunk_bytes):
        if not chunk:
            continue
        chunk_start = position
        chunk_end = position + len(chunk)
        position = chunk_end
        if chunk_end <= start or chunk_start > end:
            continue  # entirely outside [start, end] -> drain quietly
        slice_start = max(0, start - chunk_start)
        slice_end = min(len(chunk), end + 1 - chunk_start)
        if slice_start < slice_end:
            yield chunk[slice_start:slice_end]


async def _empty_body() -> AsyncIterator[bytes]:
    """An async generator that yields nothing — used for synthesized 416
    responses, which never have a body."""
    return
    yield  # pragma: no cover - unreachable; makes this an async generator


async def _noop_aclose() -> None:
    """`UpstreamStream.aclose` for a stream whose real upstream response
    was already closed before returning (the synthesized-416 paths) — safe
    for the caller to await unconditionally regardless of which path built
    the `UpstreamStream`."""
    return None


def _passthrough_headers(resp: httpx.Response) -> dict[str, str]:
    headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    for src, dst in (
        ("content-length", "Content-Length"),
        ("content-range", "Content-Range"),
        ("content-type", "Content-Type"),
    ):
        value = resp.headers.get(src)
        if value is not None:
            headers[dst] = value
    return headers


# --- Redirect-following send ---------------------------------------------------

def _request_headers(range_header: Optional[str]) -> dict[str, str]:
    headers = {"User-Agent": settings.XTREAM_USER_AGENT}
    if range_header:
        headers["Range"] = range_header
    return headers


async def _send(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    """One streaming GET. Never lets `url` leak into a raised message (it
    carries Xtream credentials) — mirrors `download_to_disk`'s exception
    mapping, `from None` so the original exception's repr (which can embed
    host/connection details) is never chained into anything a caller might
    log."""
    try:
        request = client.build_request("GET", url, headers=headers)
        return await client.send(request, stream=True)
    except httpx.TimeoutException:
        raise UpstreamTimeout("upstream timeout") from None
    except httpx.TransportError:
        raise UpstreamError("upstream transport error") from None
    except httpx.HTTPError:
        raise UpstreamError("upstream error") from None


# --- Public entry point ---------------------------------------------------------

async def open_upstream(url: str, range_header: Optional[str]) -> UpstreamStream:
    """Fetch `url` (an already-built Xtream stream URL — never logged),
    following redirects (SSRF-guarded via `assert_public_redirect_host`,
    up to `settings.DOWNLOAD_MAX_REDIRECTS` hops) and passing `range_header`
    (the DAV client's `Range` header, or `None`) through to the upstream.

    Returns a servable `UpstreamStream` for 200/206/416 — `body` is NOT
    consumed yet; the caller MUST call `aclose()` exactly once when done
    with it (success, early stop, or client disconnect alike).

    Raises `UpstreamNotFound` for any 4xx upstream status, `UpstreamError`
    for a 5xx status / an unfollowable or SSRF-rejected redirect /
    transport-level failure, or `UpstreamTimeout` (an `UpstreamError`
    subclass) specifically for a connect/read timeout. Never raises for a
    normal 200/206/416 — those come back as a value.
    """
    client = await get_client()
    current_url = url
    redirects_left = settings.DOWNLOAD_MAX_REDIRECTS

    while True:
        resp = await _send(client, current_url, _request_headers(range_header))

        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            await resp.aclose()
            if not location or redirects_left <= 0:
                raise UpstreamError("too many redirects (or missing Location)")
            next_url = str(resp.url.join(location))
            try:
                await assert_public_redirect_host(httpx.URL(next_url).host)
            except DownloadPermanentError:
                logger.warning("DAV relay: rejected redirect to a non-public host")
                raise UpstreamError("unsafe redirect target") from None
            current_url = next_url
            redirects_left -= 1
            continue

        break

    if resp.status_code == 416:
        content_range = resp.headers.get("content-range")
        await resp.aclose()
        headers = {"Accept-Ranges": "bytes"}
        if content_range:
            headers["Content-Range"] = content_range
        return UpstreamStream(416, headers, _empty_body(), _noop_aclose)

    if 400 <= resp.status_code < 500:
        await resp.aclose()
        raise UpstreamNotFound(f"upstream responded {resp.status_code}")

    if resp.status_code >= 500:
        await resp.aclose()
        raise UpstreamError(f"upstream responded {resp.status_code}")

    if resp.status_code not in (200, 206):
        await resp.aclose()
        raise UpstreamError(f"unexpected upstream status {resp.status_code}")

    if resp.status_code == 200 and range_header and settings.DAV_RANGE_SHIM:
        return await _apply_range_shim(resp, range_header)

    return UpstreamStream(
        resp.status_code, _passthrough_headers(resp),
        resp.aiter_bytes(settings.DOWNLOAD_CHUNK_BYTES), resp.aclose,
    )


async def _apply_range_shim(resp: httpx.Response, range_header: str) -> UpstreamStream:
    """`resp` is a 200 (upstream ignored the client's Range). Slice it into
    the requested window and synthesize a 206 — or, if the window can't be
    determined/is unsatisfiable, degrade to a plain 200 pass-through / a
    synthesized 416, respectively."""
    content_length = resp.headers.get("content-length")
    total = int(content_length) if content_length and content_length.isdigit() else None
    parsed = _parse_range_header(range_header, total) if total is not None else None

    if parsed is None:
        # Unknown total size, or an unparseable Range -> can't slice safely;
        # pass the 200 through unshimmed rather than guess.
        return UpstreamStream(
            200, _passthrough_headers(resp),
            resp.aiter_bytes(settings.DOWNLOAD_CHUNK_BYTES), resp.aclose,
        )

    start, end = parsed
    if start > end or start >= total:
        await resp.aclose()
        return UpstreamStream(
            416, {"Accept-Ranges": "bytes", "Content-Range": f"bytes */{total}"},
            _empty_body(), _noop_aclose,
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(end - start + 1),
    }
    content_type = resp.headers.get("content-type")
    if content_type is not None:
        headers["Content-Type"] = content_type
    body = _shim_ranged_body(resp, start, end, settings.DOWNLOAD_CHUNK_BYTES)
    return UpstreamStream(206, headers, body, resp.aclose)
