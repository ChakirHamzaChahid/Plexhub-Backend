"""DAV-1B: byte relay to the Xtream upstream, with HTTP Range support.

The counterpart to `app/dav/throttle.py`'s per-account concurrency gate —
`open_upstream` is the thing actually GATED by a throttle permit (the
router, `app/api/dav.py`, owns acquiring the permit before calling this and
releasing it once `UpstreamStream.body` is fully drained/`aclose`d; this
module itself knows nothing about accounts or the throttle).

Reuses the SAME redirect-following + SSRF guard as
`app.services.download_service.download_to_disk` — both call the shared
predicate in `app.utils.ssrf` (CR-S08) directly, rather than this module
importing from `app.services.download_service` (which would recreate the
`services ⇄ dav` import cycle `app.services.plex_generation_service`
already has the other way, via a deferred `app.dav.vfs` import — see
AUDIT-P4-007). A provider's stream URL can legitimately 302 to a CDN host,
and that hop must be verified to resolve to a public IP before being
fetched, exactly like a physical download.

Range handling: the client's `Range` header is passed straight through to
the upstream. Some Xtream panels ignore it and answer 200 with the full
file — `_apply_range_shim` (gated by `DAV_RANGE_SHIM`) re-slices that full
body into the requested window and synthesizes a 206, so rclone's chunked
reads never re-download the whole file per chunk. `DAV_RANGE_SHIM=false`
leaves an ignored Range as a raw 200 pass-through instead.

Secrets invariant (mirrors download_service): the upstream URL embeds the
Xtream account's user/password in its path/query. No exception message or
log line in this module ever contains `url`/a redirect `Location` value.

Observability (S5.3, AUDIT-P8-002): every body this module hands back
(plain pass-through AND the range-shim's re-sliced body — see
`_counted_body`) is wrapped so every chunk actually relayed to the DAV
client increments `plexhub_dav_relayed_bytes_total`, unlabelled. The
shim's own extra cost (draining and discarding the bytes before `start`
and after `end` while still holding the account's throttle permit) is
NOT double-counted here — only bytes the client actually receives are
counted — but it shows up anyway: holding the permit longer is exactly
what makes `plexhub_dav_permit_wait_seconds` (`app/dav/throttle.py`) rise
for the next request queued behind it, and eventually
`plexhub_dav_throttle_rejections_total` once that queueing exceeds
`DAV_QUEUE_TIMEOUT_SECONDS` — the shim's cost is visible through the
throttle metrics, not hidden by the byte counter.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx

from app.config import settings
from app.utils.metrics import dav_bytes_served_total, dav_relayed_bytes_total
from app.utils.ssrf import SsrfBlockedError, assert_public_host

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


async def _counted_body(
    body: AsyncIterator[bytes], *, from_cache: bool = False,
) -> AsyncIterator[bytes]:
    """Wrap a byte iterator so every chunk served to the DAV client increments
    `plexhub_dav_bytes_served_total`, and upstream-relayed chunks also increment
    `plexhub_dav_relayed_bytes_total`."""
    async for chunk in body:
        if chunk:
            n = len(chunk)
            dav_bytes_served_total.inc(n)
            if not from_cache:
                dav_relayed_bytes_total.inc(n)
        yield chunk


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

async def open_upstream(
    url: str,
    range_header: Optional[str],
    *,
    cache_key: Optional[str] = None,
    total_size: Optional[int] = None,
) -> UpstreamStream:
    """Fetch `url` (an already-built Xtream stream URL — never logged),
    following redirects (SSRF-guarded via `app.utils.ssrf.assert_public_host`,
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
                await assert_public_host(httpx.URL(next_url).host)
            except SsrfBlockedError:
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
        stream = await _apply_range_shim(resp, range_header)
        if cache_key and stream.status_code == 206 and total_size:
            parsed_range = _parse_range_header(range_header, total_size)
            if parsed_range is not None:
                stream = _maybe_tee_probe_stream(stream, cache_key, parsed_range[0], parsed_range[1], total_size)
        return stream

    body = _counted_body(resp.aiter_bytes(settings.DOWNLOAD_CHUNK_BYTES))
    stream = UpstreamStream(
        resp.status_code, _passthrough_headers(resp), body, resp.aclose,
    )
    if cache_key and resp.status_code == 206 and range_header and total_size:
        parsed_range = _parse_range_header(range_header, total_size)
        if parsed_range is not None:
            stream = _maybe_tee_probe_stream(stream, cache_key, parsed_range[0], parsed_range[1], total_size)
    return stream


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
            _counted_body(resp.aiter_bytes(settings.DOWNLOAD_CHUNK_BYTES)), resp.aclose,
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
    body = _counted_body(_shim_ranged_body(resp, start, end, settings.DOWNLOAD_CHUNK_BYTES))
    return UpstreamStream(206, headers, body, resp.aclose)


# --- Header + Tail Probe Cache (Lot 12 T12.1) --------------------------------

DAV_PROBE_HEADER_BYTES = 16 * 1024 * 1024  # 16 MiB
DAV_PROBE_TAIL_BYTES = 32 * 1024 * 1024    # 32 MiB
_PREWARM_TASKS: set[asyncio.Task] = set()


def probe_cache_key(server_id: str, rating_key: str) -> str:
    """Deterministic, path-safe cache key for `(server_id, rating_key)`."""
    return hashlib.sha256(f"{server_id}:{rating_key}".encode("utf-8")).hexdigest()


def _probe_cache_root() -> Optional[Path]:
    if not settings.DAV_PROBE_CACHE_DIR or settings.DAV_PROBE_CACHE_MAX_MB <= 0:
        return None
    return Path(settings.DAV_PROBE_CACHE_DIR).resolve()


def _confined_cache_file(root: Path, filename: str) -> Path:
    target = (root / filename).resolve()
    if not target.is_relative_to(root):
        raise ValueError("probe cache path escapes DAV_PROBE_CACHE_DIR")
    return target


def _enforce_probe_cache_budget(root: Path, max_bytes: int) -> None:
    """Evict oldest `.head`/`.tail` slices (by mtime) until total <= max_bytes."""
    if max_bytes <= 0 or not root.is_dir():
        return
    files: list[tuple[float, int, Path]] = []
    total = 0
    for p in root.iterdir():
        if not p.is_file() or p.suffix not in (".head", ".tail"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    if total <= max_bytes:
        return
    files.sort(key=lambda item: item[0])  # oldest first
    for _mtime, sz, p in files:
        if total <= max_bytes:
            break
        try:
            p.unlink(missing_ok=True)
            total -= sz
        except OSError:
            continue


def write_probe_slice(cache_key: str, kind: str, data: bytes, total_size: int) -> None:
    """Atomically persist a `.head` or `.tail` probe slice under `DAV_PROBE_CACHE_DIR`."""
    if kind not in ("head", "tail") or not data or total_size <= 0:
        return
    root = _probe_cache_root()
    if root is None:
        return
    max_bytes = settings.DAV_PROBE_CACHE_MAX_MB * 1024 * 1024
    if len(data) > max_bytes:
        return
    root.mkdir(parents=True, exist_ok=True)
    target = _confined_cache_file(root, f"{cache_key}.{kind}")
    tmp = _confined_cache_file(root, f"{cache_key}.{kind}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)

    meta_path = _confined_cache_file(root, f"{cache_key}.meta")
    meta_tmp = _confined_cache_file(root, f"{cache_key}.meta.tmp")
    meta_tmp.write_text(json.dumps({"size": total_size}), encoding="utf-8")
    os.replace(meta_tmp, meta_path)

    _enforce_probe_cache_budget(root, max_bytes)


def _read_cached_slice(
    root: Path, cache_key: str, start: int, end: int, total_size: int,
) -> Optional[bytes]:
    if end < DAV_PROBE_HEADER_BYTES:
        head_path = _confined_cache_file(root, f"{cache_key}.head")
        if not head_path.is_file():
            return None
        if head_path.stat().st_size <= end:
            return None
        os.utime(head_path, None)
        with head_path.open("rb") as fh:
            fh.seek(start)
            return fh.read(end - start + 1)

    tail_base = max(0, total_size - DAV_PROBE_TAIL_BYTES)
    if start >= tail_base:
        tail_path = _confined_cache_file(root, f"{cache_key}.tail")
        meta_path = _confined_cache_file(root, f"{cache_key}.meta")
        if not tail_path.is_file() or not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if meta.get("size") != total_size:
            return None
        expected_tail_len = total_size - tail_base
        if tail_path.stat().st_size != expected_tail_len:
            return None
        os.utime(tail_path, None)
        rel_start = start - tail_base
        with tail_path.open("rb") as fh:
            fh.seek(rel_start)
            return fh.read(end - start + 1)

    return None


async def _single_chunk_body(data: bytes) -> AsyncIterator[bytes]:
    if data:
        yield data


async def try_serve_cached_range(
    cache_key: str, range_header: Optional[str], total_size: Optional[int],
) -> Optional[UpstreamStream]:
    """Serve a Range request from `DAV_PROBE_CACHE_DIR` if it falls within
    the cached header (first 16 MiB) or tail (last 32 MiB). Returns None on
    cache miss or when caching is disabled."""
    root = _probe_cache_root()
    if root is None or not range_header or not total_size or total_size <= 0:
        return None
    parsed = _parse_range_header(range_header, total_size)
    if parsed is None:
        return None
    start, end = parsed
    if start > end or start >= total_size:
        return None

    data = await asyncio.to_thread(_read_cached_slice, root, cache_key, start, end, total_size)
    if data is None:
        return None

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{total_size}",
        "Content-Length": str(len(data)),
    }
    return UpstreamStream(
        206, headers, _counted_body(_single_chunk_body(data), from_cache=True), _noop_aclose,
    )


def _classify_probe_window(start: int, end: int, total_size: int) -> Optional[str]:
    if start == 0 and end == min(total_size, DAV_PROBE_HEADER_BYTES) - 1:
        return "head"
    tail_base = max(0, total_size - DAV_PROBE_TAIL_BYTES)
    if start == tail_base and end == total_size - 1:
        return "tail"
    return None


def _maybe_tee_probe_stream(
    stream: UpstreamStream, cache_key: str, start: int, end: int, total_size: int,
) -> UpstreamStream:
    root = _probe_cache_root()
    kind = _classify_probe_window(start, end, total_size)
    if root is None or kind is None:
        return stream

    expected_len = end - start + 1
    max_cap = DAV_PROBE_TAIL_BYTES if kind == "tail" else DAV_PROBE_HEADER_BYTES
    if expected_len <= 0 or expected_len > max_cap:
        return stream

    async def _tee() -> AsyncIterator[bytes]:
        buf = bytearray()
        completed = False
        try:
            async for chunk in stream.body:
                if len(buf) + len(chunk) <= max_cap:
                    buf.extend(chunk)
                yield chunk
            completed = True
        finally:
            if completed and len(buf) == expected_len:
                try:
                    await asyncio.to_thread(write_probe_slice, cache_key, kind, bytes(buf), total_size)
                except Exception:
                    logger.debug("DAV probe cache write skipped", exc_info=True)

    return UpstreamStream(stream.status_code, stream.headers, _tee(), stream.aclose)


async def prewarm_tree_probes(
    tree,
    resolve_url: Callable[[object], Awaitable[Optional[str]]],
    *,
    max_items: int = 50,
) -> int:
    """Best-effort background prewarm of header+tail slices for *tree* entries."""
    root = _probe_cache_root()
    if root is None:
        return 0
    warmed = 0
    for entry in list(getattr(tree, "entries", {}).values()):
        if warmed >= max_items:
            break
        if getattr(entry, "is_dir", True):
            continue
        size = getattr(entry, "size", None)
        server_id = getattr(entry, "server_id", None)
        rating_key = getattr(entry, "rating_key", None)
        if not size or size <= 0 or not server_id or not rating_key:
            continue
        key = probe_cache_key(server_id, rating_key)
        head_file = _confined_cache_file(root, f"{key}.head")
        tail_file = _confined_cache_file(root, f"{key}.tail")
        if head_file.is_file() and tail_file.is_file():
            continue
        try:
            url = await resolve_url(entry)
            if not url:
                continue
            head_end = min(size, DAV_PROBE_HEADER_BYTES) - 1
            if not head_file.is_file():
                s = await open_upstream(url, f"bytes=0-{head_end}", cache_key=key, total_size=size)
                try:
                    async for _ in s.body:
                        pass
                finally:
                    await s.aclose()
            tail_start = max(0, size - DAV_PROBE_TAIL_BYTES)
            if not tail_file.is_file():
                s = await open_upstream(url, f"bytes={tail_start}-{size - 1}", cache_key=key, total_size=size)
                try:
                    async for _ in s.body:
                        pass
                finally:
                    await s.aclose()
            warmed += 1
        except Exception:
            logger.debug("DAV probe prewarm skipped for %r", getattr(entry, "name", "?"), exc_info=True)
    return warmed


def schedule_probe_prewarm(
    tree,
    resolve_url: Optional[Callable[[object], Awaitable[Optional[str]]]] = None,
) -> Optional[asyncio.Task]:
    """Schedule non-blocking background prewarm when `DAV_PROBE_CACHE_DIR` is enabled."""
    if _probe_cache_root() is None or resolve_url is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(prewarm_tree_probes(tree, resolve_url))
    _PREWARM_TASKS.add(task)
    task.add_done_callback(_PREWARM_TASKS.discard)
    return task
