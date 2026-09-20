"""Manual scraper poster-match (ADR 0005 D5) — compares the Xtream poster of
a media item against every poster variant TMDB (or OMDb) offers for a
candidate, via perceptual image hashing (dHash + pHash), to help the
operator pick the right match when titles/years alone are ambiguous or a
provider's plot text is unreliable.

Client isolation (ADR 0005 F2): `tmdb_service`/`omdb_service` inject the
provider API key as a query param on EVERY request (`tmdb_service.py:139`).
This module therefore owns a DEDICATED `httpx.AsyncClient` — never reused
from those services — so an image download can never leak the API key.

SSRF (CLAUDE.md piège 19e): the client is built with
`event_hooks={"request": [ssrf.vet_request]}`, which fires on the initial
request AND every followed redirect hop (`follow_redirects=True`), exactly
like `app.plex_generator.storage`'s image client and `app.dav.relay`'s
upstream client. Rebinding DNS remains an accepted residual risk (same
doctrine as everywhere else this guard is used).

Secrets: an Xtream poster URL can embed the account's credentials in its
path/query (same shape as an Xtream stream URL). Nothing in this module
ever logs a URL or hostname — `PosterFetchError` messages are fixed/
URL-free, and the only per-request identifier that reaches a log line is
`_url_fingerprint()` (a short sha1 prefix), mirroring the convention
already used by `app.dav.relay`/`app.services.download_service`.

CPU-bound work (`compute_hashes`) is synchronous, pure Pillow/numpy code —
callers MUST invoke it via `asyncio.to_thread` (CLAUDE.md piège 11), never
directly on the event loop. It never mutates the global
`PIL.Image.MAX_IMAGE_PIXELS` (that would affect every other Pillow caller
in the process, e.g. `plex_generator/storage.py`'s poster/fanart
downloader) — the pixel-count cap is enforced locally, before any pixel
decoding happens.

Memory hardening (2GB Docker container, security review of the initial
W3 commit): a naively-decoded `POSTER_MAX_PIXELS` image converted to a
float64 numpy array is ~300MB for a single call, and `asyncio.to_thread`
uses the process-wide default executor with no concurrency limit of its
own — several concurrent candidate comparisons could each spawn a
full-size decode. Mitigated three ways: (1) JPEGs use `Image.draft()`
to have libjpeg decode directly at a reduced resolution (an IDCT-time
optimization, not a post-decode resize — the full-resolution buffer is
never materialized); (2) every format is additionally shrunk via
`Image.thumbnail()` to `_HASH_REDUCE_SIZE` before the numpy conversion;
(3) the numpy array itself is float32, not float64, and the actual
`compute_hashes` call (the only unbounded-fan-out point) is gated by a
dedicated `POSTER_HASH_CONCURRENCY` semaphore, independent of the
network-fetch semaphores below.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal, Optional, Sequence
from urllib.parse import urlsplit

import httpx
import numpy as np
from PIL import Image, ImageOps

from app.config import settings
from app.utils import ssrf
from app.utils.ttl_cache import TTLCache

logger = logging.getLogger("plexhub.poster")

Badge = Literal["identical", "close", "different", "unknown"]

# Known image CDNs exempt from the per-host concurrency cap (ADR 0005 D5) —
# TMDB's own image host, plus OMDb's usual Poster host (Amazon-hosted).
_CDN_HOSTS = frozenset({"image.tmdb.org", "m.media-amazon.com"})

# dHash grid (classic 9x8 -> 8x8 horizontal-gradient bits = 64 bits).
_DHASH_SIZE = (9, 8)  # (width, height) as passed to PIL.Image.resize
# pHash DCT grid + low-frequency corner kept (ADR 0005 D5: "DCT 2D numpy
# 32x32 -> coin 8x8").
_PHASH_DCT_SIZE = 32
_PHASH_CORNER = 8

# Letterbox trim: a border row/column whose pixel stddev is below this is
# considered a uniform bar (mattes/pillarbox), trimmed before hashing so a
# poster and a letterboxed re-encode of the same poster still hash close.
# Bounded to at most a quarter of each dimension so a genuinely flat/dark
# poster is never trimmed down to nothing.
_LETTERBOX_STDDEV_THRESHOLD = 4.0
_LETTERBOX_MAX_TRIM_FRACTION = 0.25

# Working-resolution cap applied via `Image.draft()` (JPEG) / `Image.
# thumbnail()` (every format) BEFORE the numpy conversion — hashing math
# only ever needs a 32x32 grid, so this is generous headroom, not a
# quality compromise.
_HASH_REDUCE_SIZE = (512, 512)

_hash_cache_positive: TTLCache[str, "PosterHash"] = TTLCache(max_size=4000, ttl_seconds=6 * 3600)
_hash_cache_negative: TTLCache[str, bool] = TTLCache(max_size=4000, ttl_seconds=15 * 60)
_CACHE_MISS = object()

# phash -> {sha1(url)[:16], ...} — generic/placeholder-poster detection
# (ADR 0005 D5: "pHash vu >= POSTER_GENERIC_MIN_REPEAT fois (URLs
# distinctes)"). Stores URL FINGERPRINTS, never raw URLs (an Xtream poster
# URL can embed credentials) — bounded LRU so this never grows unbounded
# across a long-running process.
_GENERIC_REGISTRY_CAP = 5000
_generic_registry: "OrderedDict[int, set[str]]" = OrderedDict()

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_global_semaphore: Optional[asyncio.Semaphore] = None
_host_semaphores: dict[str, asyncio.Semaphore] = {}
# Dedicated to the CPU-bound `compute_hashes` thread-pool call — separate
# from the network-fetch semaphores above, so this is the one knob that
# actually bounds concurrent decode/hash memory usage regardless of how
# many fetches are in flight.
_hash_semaphore: Optional[asyncio.Semaphore] = None

# Restrict Pillow's format sniffing to the handful of formats a poster CDN
# ever actually serves — narrows the attacker-reachable parser surface
# (review L5) rather than trusting Pillow's full autodetection.
_ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
# `image/*` content types that are also scriptable documents — rejected at
# fetch time so they can never reach the admin poster proxy's response.
_SCRIPTABLE_IMAGE_TYPES = frozenset({"image/svg+xml", "image/svg", "image/svg-xml"})


class PosterFetchError(Exception):
    """Raised for any poster-download/decode failure. Message is always
    fixed/URL-free — an Xtream poster URL can embed account credentials in
    its path/query, so nothing derived from the URL/host is ever allowed
    into this exception's message (mirrors `ssrf.SsrfBlockedError`)."""


@dataclass(frozen=True)
class PosterHash:
    dhash: int  # 64 bits, horizontal-gradient hash on a 9x8 grayscale grid
    phash: int  # 64 bits, DCT-II 2D on a 32x32 grayscale grid, 8x8 corner
    is_generic: bool  # flat/placeholder poster — see `compute_hashes`/`_record_generic_repeat`


@dataclass(frozen=True)
class FetchedImage:
    content: bytes
    content_type: str  # always "image/*" (checked before this is constructed)


@dataclass(frozen=True)
class PosterComparison:
    badge: Badge
    phash_distance: Optional[int]
    dhash_distance: Optional[int]
    image_score: Optional[float]  # max(0, 1 - phash_distance/32); None if unknown
    best_poster_url: Optional[str]  # candidate variant closest to the Xtream poster
    variants_compared: int
    xtream_generic: bool
    shortcut: bool  # True = matched by image.tmdb.org filename, no download needed


def _url_fingerprint(url: str) -> str:
    """Short, non-reversible identifier for a URL — safe to log (no
    credentials, no host). Mirrors the convention used by
    `app.dav.relay`/`app.services.download_service`."""
    return hashlib.sha1(url.encode("utf-8", errors="replace")).hexdigest()[:8]


# --- Pillow / numpy hashing (sync, CPU-bound — call via asyncio.to_thread) ---


def _trim_letterbox(arr: np.ndarray) -> np.ndarray:
    """Trim near-uniform border rows/columns (mattes/pillarbox bars) so a
    letterboxed re-encode of the same poster still hashes close to the
    original. Bounded to `_LETTERBOX_MAX_TRIM_FRACTION` of each dimension."""
    height, width = arr.shape
    max_trim_h = int(height * _LETTERBOX_MAX_TRIM_FRACTION)
    max_trim_w = int(width * _LETTERBOX_MAX_TRIM_FRACTION)
    top, bottom, left, right = 0, height, 0, width

    while (
        top < bottom - 1
        and top < max_trim_h
        and float(np.std(arr[top, left:right])) < _LETTERBOX_STDDEV_THRESHOLD
    ):
        top += 1
    while (
        bottom > top + 1
        and (height - bottom) < max_trim_h
        and float(np.std(arr[bottom - 1, left:right])) < _LETTERBOX_STDDEV_THRESHOLD
    ):
        bottom -= 1
    while (
        left < right - 1
        and left < max_trim_w
        and float(np.std(arr[top:bottom, left])) < _LETTERBOX_STDDEV_THRESHOLD
    ):
        left += 1
    while (
        right > left + 1
        and (width - right) < max_trim_w
        and float(np.std(arr[top:bottom, right - 1])) < _LETTERBOX_STDDEV_THRESHOLD
    ):
        right -= 1

    return arr[top:bottom, left:right]


def _resize_gray(arr: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """`size` = (width, height), same order as `PIL.Image.resize`."""
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="L")
    resized = img.resize(size, Image.LANCZOS)
    return np.asarray(resized, dtype=np.float32)


def _compute_dhash(arr: np.ndarray) -> int:
    grid = _resize_gray(arr, _DHASH_SIZE)  # shape (8, 9) — (height, width)
    diff = grid[:, :-1] > grid[:, 1:]  # shape (8, 8)
    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bit)
    return value


def _dct_matrix(n: int) -> np.ndarray:
    """DCT-II basis matrix (orthonormal), so a 2D DCT is `M @ arr @ M.T`
    without a scipy dependency (ADR 0005: "DCT 2D numpy ... pas de
    imagehash")."""
    k = np.arange(n).reshape(-1, 1)
    nn = np.arange(n).reshape(1, -1)
    matrix = np.cos(np.pi / n * (nn + 0.5) * k)
    matrix[0, :] *= 1 / np.sqrt(2)
    matrix *= np.sqrt(2.0 / n)
    return matrix


_DCT_BASIS = _dct_matrix(_PHASH_DCT_SIZE)


def _compute_phash(arr: np.ndarray) -> tuple[int, float]:
    """Returns (phash, stddev-of-the-32x32-grayscale-input) — the stddev is
    the raw-pixel signal `compute_hashes` uses for generic-poster
    detection, computed BEFORE the frequency transform."""
    grid = _resize_gray(arr, (_PHASH_DCT_SIZE, _PHASH_DCT_SIZE))
    stddev = float(np.std(grid))
    dct = _DCT_BASIS @ grid @ _DCT_BASIS.T
    corner = dct[:_PHASH_CORNER, :_PHASH_CORNER].flatten()  # 64 values, DC first
    median_excl_dc = float(np.median(corner[1:]))
    bits = corner > median_excl_dc
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value, stddev


def compute_hashes(image_bytes: bytes) -> PosterHash:
    """SYNC, CPU-bound — call ONLY via `asyncio.to_thread`. Raises
    `PosterFetchError` (never a raw Pillow exception) on any undecodable/
    oversized image. `Image.MAX_IMAGE_PIXELS` is never mutated globally —
    the pixel-count cap is `settings.POSTER_MAX_PIXELS`, enforced from the
    header BEFORE any pixel is decoded.

    Working resolution is bounded to `_HASH_REDUCE_SIZE` before the numpy
    conversion (review M1): JPEGs use `Image.draft()` so libjpeg decodes
    directly at reduced resolution (the full-size buffer never exists);
    every format is then additionally shrunk with `Image.thumbnail()`,
    which is a no-op if the image was already smaller. Hashing only ever
    needs a 32x32 grid, so this doesn't affect hash quality.

    Deliberately does NOT touch Python's global `warnings` filter state
    (that was the initial W3 commit's approach, reverted per review L4 —
    `warnings.catch_warnings()` mutates process-global state and is not
    thread-safe, and this function runs concurrently across worker
    threads via `asyncio.to_thread`). `Image.DecompressionBombError` is
    still caught explicitly; the (non-raising) `DecompressionBombWarning`
    is not specially handled — `settings.POSTER_MAX_PIXELS` is enforced
    well below Pillow's own warning threshold anyway.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes), formats=_ALLOWED_FORMATS)
        width, height = img.size
        if width <= 0 or height <= 0:
            raise PosterFetchError("invalid image dimensions")
        if width * height > settings.POSTER_MAX_PIXELS:
            raise PosterFetchError("image exceeds pixel cap")
        if img.format == "JPEG":
            # Must be called before any operation that forces a full
            # decode. Only shrinks by powers of ~2 and only affects JPEG,
            # hence the `thumbnail()` pass below for the exact/final size
            # and for every other format.
            img.draft("L", _HASH_REDUCE_SIZE)
        img = ImageOps.exif_transpose(img)
        img = img.convert("L")
        img.thumbnail(_HASH_REDUCE_SIZE, Image.LANCZOS)
        img.load()
    except PosterFetchError:
        raise
    except Image.DecompressionBombError as exc:
        raise PosterFetchError(f"decompression bomb rejected ({type(exc).__name__})") from None
    except Exception as exc:  # noqa: BLE001 — any Pillow decode failure -> uniform error type
        raise PosterFetchError(f"undecodable image ({type(exc).__name__})") from None

    arr = np.asarray(img, dtype=np.float32)
    arr = _trim_letterbox(arr)
    if arr.size == 0:
        raise PosterFetchError("image trimmed to empty")

    dhash_value = _compute_dhash(arr)
    phash_value, stddev = _compute_phash(arr)
    is_generic = stddev < settings.POSTER_GENERIC_STDDEV
    return PosterHash(dhash=dhash_value, phash=phash_value, is_generic=is_generic)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def badge_for(phash_distance: Optional[int], dhash_distance: Optional[int]) -> Badge:
    if phash_distance is None or dhash_distance is None:
        return "unknown"
    if phash_distance <= settings.POSTER_PHASH_IDENTICAL and dhash_distance <= settings.POSTER_DHASH_IDENTICAL:
        return "identical"
    if phash_distance <= settings.POSTER_PHASH_CLOSE:
        return "close"
    return "different"


def _record_generic_repeat(url: str, phash: int) -> bool:
    """Tracks how many DISTINCT Xtream poster URLs have hashed to the same
    `phash` (a provider reusing one placeholder image across titles).
    Stores a URL fingerprint, never the raw URL. Returns True once the
    count reaches `settings.POSTER_GENERIC_MIN_REPEAT`."""
    fp = _url_fingerprint(url)
    urls = _generic_registry.get(phash)
    if urls is None:
        urls = set()
        _generic_registry[phash] = urls
        if len(_generic_registry) > _GENERIC_REGISTRY_CAP:
            _generic_registry.popitem(last=False)
    urls.add(fp)
    _generic_registry.move_to_end(phash)
    return len(urls) >= settings.POSTER_GENERIC_MIN_REPEAT


# --- HTTP client (dedicated, SSRF-vetted, never tmdb_service's) --------------


async def get_client() -> httpx.AsyncClient:
    """Shared `httpx.AsyncClient`, dedicated to image downloads — NEVER
    `tmdb_service`/`omdb_service`'s client, which injects the provider API
    key as a query param on every request (ADR 0005 F2)."""
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=5,
                timeout=httpx.Timeout(settings.POSTER_FETCH_TIMEOUT),
                event_hooks={"request": [ssrf.vet_request]},
            )
    return _client


async def close() -> None:
    """Closes the pooled client, if one was ever built. Safe no-op
    otherwise (mirrors `app.dav.relay.close_client`) — called from
    `app.main`'s lifespan shutdown."""
    global _client
    async with _client_lock:
        if _client is not None:
            await _client.aclose()
            _client = None


def _get_global_semaphore() -> asyncio.Semaphore:
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(settings.POSTER_CONCURRENCY)
    return _global_semaphore


def _get_host_semaphore(host: str) -> asyncio.Semaphore:
    sem = _host_semaphores.get(host)
    if sem is None:
        sem = asyncio.Semaphore(settings.POSTER_PER_HOST_CONCURRENCY)
        _host_semaphores[host] = sem
    return sem


def _get_hash_semaphore() -> asyncio.Semaphore:
    global _hash_semaphore
    if _hash_semaphore is None:
        _hash_semaphore = asyncio.Semaphore(settings.POSTER_HASH_CONCURRENCY)
    return _hash_semaphore


async def _download(client: httpx.AsyncClient, url: str, fingerprint: str) -> FetchedImage:
    try:
        # `Accept-Encoding: identity` + rejecting any `Content-Encoding`
        # actually returned, PLUS reading via `aiter_raw()` instead of
        # `aiter_bytes()` (review L3): `aiter_bytes()` transparently
        # inflates a compressed body, so a small compressed response could
        # balloon to far more than `POSTER_MAX_BYTES` in memory inside a
        # SINGLE chunk, before our own running-total check ever sees it.
        # `aiter_raw()` never inflates anything, so the byte-cap check
        # below is a true bound on memory regardless of what a malicious
        # or misconfigured upstream sends.
        async with client.stream(
            "GET", url, headers={"Accept-Encoding": "identity"},
        ) as response:
            if response.status_code >= 400:
                raise PosterFetchError(f"upstream status {response.status_code}")
            content_encoding = (response.headers.get("content-encoding") or "").strip().lower()
            if content_encoding and content_encoding != "identity":
                raise PosterFetchError("compressed response rejected")
            content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                raise PosterFetchError("non-image content-type")
            if content_type in _SCRIPTABLE_IMAGE_TYPES:
                # SVG is an image AND a scriptable document. Pillow can't
                # hash it anyway, and the admin poster proxy serves these
                # bytes back from the /admin origin: a provider-controlled
                # `thumb_url` answering image/svg+xml would run script under
                # the operator's ambient Basic Auth as soon as the image is
                # opened in a tab.
                raise PosterFetchError("scriptable image content-type rejected")
            buf = bytearray()
            max_bytes = settings.POSTER_MAX_BYTES
            async for chunk in response.aiter_raw():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise PosterFetchError("image exceeds byte cap")
            return FetchedImage(content=bytes(buf), content_type=content_type)
    except PosterFetchError:
        raise
    except ssrf.SsrfBlockedError:
        logger.warning("poster fetch blocked by SSRF guard url_hash=%s", fingerprint)
        raise PosterFetchError("unsafe host (SSRF guard)") from None
    except httpx.HTTPError as exc:
        logger.debug("poster fetch transport error (%s) url_hash=%s", type(exc).__name__, fingerprint)
        raise PosterFetchError(f"transport error ({type(exc).__name__})") from None


async def fetch_image(url: str) -> FetchedImage:
    """Downloads `url` through the dedicated, SSRF-vetted client. Raises
    `PosterFetchError` on any failure: an unparsable URL, non-2xx status,
    a compressed/non-`image/*` response, a body exceeding
    `settings.POSTER_MAX_BYTES` (checked while streaming, not just via
    `Content-Length`, which a server can omit or lie about), or the whole
    fetch exceeding `settings.POSTER_FETCH_TIMEOUT` as a TOTAL deadline
    (review L2) — `httpx.Timeout` alone only bounds each individual
    connect/read operation, so a server trickling one byte just inside
    that window forever would otherwise hold a concurrency-semaphore slot
    indefinitely."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as exc:
        raise PosterFetchError(f"invalid poster url ({type(exc).__name__})") from None
    client = await get_client()
    fingerprint = _url_fingerprint(url)
    host = (parsed.host or "").lower()
    global_sem = _get_global_semaphore()
    try:
        async with global_sem:
            if host in _CDN_HOSTS:
                coro = _download(client, url, fingerprint)
            else:
                async def _with_host_semaphore() -> FetchedImage:
                    async with _get_host_semaphore(host):
                        return await _download(client, url, fingerprint)
                coro = _with_host_semaphore()
            return await asyncio.wait_for(coro, timeout=settings.POSTER_FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        raise PosterFetchError("fetch exceeded total deadline") from None


async def hash_url(url: str) -> Optional[PosterHash]:
    """Fetches + hashes `url`, with a positive (6 h) / negative (15 min)
    TTL cache keyed by URL — an operator re-opening the same scrape panel,
    or the batch worker re-visiting the same candidate poster, never
    re-downloads it within the cache window. The CPU-bound hash itself is
    gated by a dedicated `POSTER_HASH_CONCURRENCY` semaphore (review M1),
    independent of the network-fetch concurrency above."""
    cached = _hash_cache_positive.get(url, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    if _hash_cache_negative.get(url, _CACHE_MISS) is not _CACHE_MISS:
        return None

    try:
        fetched = await fetch_image(url)
        async with _get_hash_semaphore():
            result = await asyncio.to_thread(compute_hashes, fetched.content)
    except PosterFetchError as exc:
        logger.debug(
            "poster hash unavailable (%s) url_hash=%s", type(exc).__name__, _url_fingerprint(url)
        )
        _hash_cache_negative.set(url, True)
        return None

    _hash_cache_positive.set(url, result)
    return result


def _safe_urlsplit(url: str):
    """`urlsplit` raises `ValueError` on some malformed inputs (e.g. an
    unparsable IPv6-literal-looking host) — never let that escape into
    `compare_posters` uncaught (review L1)."""
    try:
        return urlsplit(url)
    except ValueError:
        return None


def _is_tmdb_image_host(url: str) -> bool:
    parts = _safe_urlsplit(url)
    if parts is None:
        return False
    return (parts.hostname or "").lower() == "image.tmdb.org"


def _basename(url: str) -> str:
    parts = _safe_urlsplit(url)
    if parts is None:
        return ""
    return parts.path.rsplit("/", 1)[-1]


async def compare_posters(
    xtream_url: Optional[str], candidate_poster_urls: Sequence[str],
) -> PosterComparison:
    """1) `xtream_url` empty -> unknown, no fetch. 2) filename shortcut: if
    `xtream_url` is itself an `image.tmdb.org` URL and its basename matches
    a candidate variant's basename, that's a match without downloading
    anything. 3) otherwise hash the Xtream poster, then each candidate
    variant (capped at `POSTER_MAX_VARIANTS`) in order, keeping the MINIMUM
    distance and stopping early on the first `identical` pair. A generic/
    placeholder Xtream poster caps the badge at `close` — a placeholder is
    never reported `identical`."""
    if not xtream_url:
        return PosterComparison(
            badge="unknown", phash_distance=None, dhash_distance=None, image_score=None,
            best_poster_url=None, variants_compared=0, xtream_generic=False, shortcut=False,
        )

    variants = list(candidate_poster_urls[: settings.POSTER_MAX_VARIANTS])

    if _is_tmdb_image_host(xtream_url):
        xtream_basename = _basename(xtream_url)
        for candidate_url in variants:
            # Both sides must be an image.tmdb.org URL (review nit) — a
            # same-basename match against a non-TMDB candidate host would
            # be a coincidence, not evidence of the same image.
            if _is_tmdb_image_host(candidate_url) and _basename(candidate_url) == xtream_basename:
                return PosterComparison(
                    badge="identical", phash_distance=0, dhash_distance=0, image_score=1.0,
                    best_poster_url=candidate_url, variants_compared=0,
                    xtream_generic=False, shortcut=True,
                )

    xtream_hash = await hash_url(xtream_url)
    if xtream_hash is None:
        return PosterComparison(
            badge="unknown", phash_distance=None, dhash_distance=None, image_score=None,
            best_poster_url=None, variants_compared=0, xtream_generic=False, shortcut=False,
        )
    xtream_generic = xtream_hash.is_generic or _record_generic_repeat(xtream_url, xtream_hash.phash)

    best_phash_distance: Optional[int] = None
    best_dhash_distance: Optional[int] = None
    best_url: Optional[str] = None
    compared = 0

    for candidate_url in variants:
        candidate_hash = await hash_url(candidate_url)
        compared += 1
        if candidate_hash is None:
            continue
        phash_distance = hamming(xtream_hash.phash, candidate_hash.phash)
        dhash_distance = hamming(xtream_hash.dhash, candidate_hash.dhash)
        if best_phash_distance is None or phash_distance < best_phash_distance:
            best_phash_distance = phash_distance
            best_dhash_distance = dhash_distance
            best_url = candidate_url
        if (
            phash_distance <= settings.POSTER_PHASH_IDENTICAL
            and dhash_distance <= settings.POSTER_DHASH_IDENTICAL
        ):
            break  # early stop — already as good as it gets

    badge = badge_for(best_phash_distance, best_dhash_distance)
    if xtream_generic and badge == "identical":
        badge = "close"  # a placeholder poster is never reported identical

    image_score = None
    if best_phash_distance is not None:
        image_score = max(0.0, 1 - best_phash_distance / 32)

    return PosterComparison(
        badge=badge,
        phash_distance=best_phash_distance,
        dhash_distance=best_dhash_distance,
        image_score=image_score,
        best_poster_url=best_url,
        variants_compared=compared,
        xtream_generic=xtream_generic,
        shortcut=False,
    )
