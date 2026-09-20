"""`app/services/poster_match_service.py` — dHash/pHash poster comparison
for the manual scraper (ADR 0005 D5).

Covers: perceptual-hash behaviour on synthetic Pillow images (resized/
recompressed/slightly-cropped -> identical/close, a different image ->
different), the dedicated SSRF-vetted client (private-IP target blocked
before any fetch, no `api_key`/`apikey` ever reaches an image host),
byte-size and pixel-count caps, non-image content-type rejection, the
`image.tmdb.org` filename shortcut, and the positive hash cache.
"""
from __future__ import annotations

import asyncio
import io
import socket
import time

import httpx
import numpy as np
import pytest
import respx
from PIL import Image, ImageDraw

from app.config import settings
from app.services import poster_match_service as pm
from app.utils import ssrf

# NB: no `pytestmark = pytest.mark.asyncio` here (unlike some sibling test
# files) — this module mixes sync (`compute_hashes`, `badge_for`, ...) and
# async tests, and `pyproject.toml`'s `asyncio_mode = "auto"` already picks
# up async `def test_*` coroutines without an explicit mark; forcing the
# mark would incorrectly apply to the sync tests too.


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _reset_poster_state():
    pm._hash_cache_positive.clear()
    pm._hash_cache_negative.clear()
    pm._generic_registry.clear()
    pm._host_semaphores.clear()
    pm._global_semaphore = None
    pm._hash_semaphore = None
    ssrf.clear_cache()
    await pm.close()
    yield
    await pm.close()
    ssrf.clear_cache()


@pytest.fixture
def image_mock():
    """respx context with no base_url — caller registers full URLs. Mirrors
    `tests/conftest.py`'s `xtream_mock` (same intent: this module's client
    talks to arbitrary provider/CDN hosts, not one fixed base)."""
    with respx.mock(assert_all_called=False) as r:
        yield r


def _addrinfo_for(*ips: str) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


def _make_poster(seed: int, size: tuple[int, int] = (300, 450)) -> Image.Image:
    """A deterministic, non-uniform synthetic poster (random colored
    rectangles) — flat/uniform images would trip the generic-poster
    detector and defeat the "different image -> different hash" tests."""
    img = Image.new("RGB", size, (20, 20, 20))
    draw = ImageDraw.Draw(img)
    rng = np.random.RandomState(seed)
    for _ in range(30):
        x0, y0 = int(rng.randint(0, size[0])), int(rng.randint(0, size[1]))
        x1 = x0 + int(rng.randint(10, 80))
        y1 = y0 + int(rng.randint(10, 80))
        color = tuple(int(c) for c in rng.randint(0, 255, 3))
        draw.rectangle([x0, y0, x1, y1], fill=color)
    return img


def _to_bytes(img: Image.Image, fmt: str = "JPEG", quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return buf.getvalue()


# --- Pillow hashing behaviour ---------------------------------------------------


class TestComputeHashesSyntheticImages:
    def test_resized_recompressed_poster_is_identical(self):
        base = _make_poster(seed=1)
        h1 = pm.compute_hashes(_to_bytes(base, quality=95))

        resized = base.resize((150, 225))
        h2 = pm.compute_hashes(_to_bytes(resized, quality=70))

        badge = pm.badge_for(pm.hamming(h1.phash, h2.phash), pm.hamming(h1.dhash, h2.dhash))
        assert badge == "identical"

    def test_slightly_cropped_poster_is_close_or_identical(self):
        base = _make_poster(seed=2)
        h1 = pm.compute_hashes(_to_bytes(base))

        w, h = base.size
        cropped = base.crop(
            (int(w * 0.03), int(h * 0.03), int(w * 0.97), int(h * 0.97))
        ).resize((w, h))
        h2 = pm.compute_hashes(_to_bytes(cropped))

        badge = pm.badge_for(pm.hamming(h1.phash, h2.phash), pm.hamming(h1.dhash, h2.dhash))
        assert badge in ("identical", "close")

    def test_different_poster_is_different(self):
        h1 = pm.compute_hashes(_to_bytes(_make_poster(seed=1)))
        h2 = pm.compute_hashes(_to_bytes(_make_poster(seed=99)))

        badge = pm.badge_for(pm.hamming(h1.phash, h2.phash), pm.hamming(h1.dhash, h2.dhash))
        assert badge == "different"

    def test_hamming_distance_to_self_is_zero(self):
        h = pm.compute_hashes(_to_bytes(_make_poster(seed=3)))
        assert pm.hamming(h.phash, h.phash) == 0
        assert pm.hamming(h.dhash, h.dhash) == 0

    def test_flat_image_is_flagged_generic(self):
        flat = Image.new("RGB", (300, 450), (128, 128, 128))
        h = pm.compute_hashes(_to_bytes(flat))
        assert h.is_generic is True

    def test_badge_for_unknown_when_either_distance_missing(self):
        assert pm.badge_for(None, 3) == "unknown"
        assert pm.badge_for(3, None) == "unknown"
        assert pm.badge_for(None, None) == "unknown"


class TestLargeImageMemoryHardening:
    """Review M1: a large decoded image must be reduced to `_HASH_REDUCE_SIZE`
    before the numpy conversion, not carried around at full resolution."""

    def test_large_flat_png_is_hashed_and_working_array_is_bounded(self, monkeypatch):
        seen_shapes: list[tuple[int, ...]] = []
        original_resize = pm._resize_gray

        def _spy(arr, size):
            seen_shapes.append(arr.shape)
            return original_resize(arr, size)

        monkeypatch.setattr(pm, "_resize_gray", _spy)

        huge = Image.new("RGB", (5000, 5000), (128, 128, 128))
        data = _to_bytes(huge, fmt="PNG")

        result = pm.compute_hashes(data)

        assert result is not None
        assert result.is_generic is True  # flat color
        assert seen_shapes, "expected _resize_gray to be invoked"
        for shape in seen_shapes:
            assert max(shape) <= 512

    def test_large_jpeg_uses_draft_before_full_decode(self, monkeypatch):
        """`Image.draft()` must be called (JPEG path) BEFORE `img.load()` —
        proven by spying on the actual `JpegImageFile.draft` override
        (the base `Image.Image.draft` is a no-op; JPEG's own IDCT-scaling
        implementation lives on the format-specific subclass)."""
        from PIL import JpegImagePlugin

        calls: list[tuple] = []
        original_draft = JpegImagePlugin.JpegImageFile.draft

        def _spy_draft(self, mode, size):
            calls.append((mode, size))
            return original_draft(self, mode, size)

        monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", _spy_draft)

        huge = _make_poster(seed=42, size=(3000, 3000))
        data = _to_bytes(huge, fmt="JPEG", quality=85)

        result = pm.compute_hashes(data)

        assert result is not None
        assert calls, "expected Image.draft() to be called for a JPEG"
        assert calls[0][0] == "L"

    async def test_hash_url_serializes_through_dedicated_hash_semaphore(self, monkeypatch):
        monkeypatch.setattr(settings, "POSTER_HASH_CONCURRENCY", 1)
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))

        concurrent = {"n": 0, "max": 0}
        real_compute = pm.compute_hashes

        def _tracked_compute(data: bytes):
            concurrent["n"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["n"])
            try:
                time.sleep(0.05)
                return real_compute(data)
            finally:
                concurrent["n"] -= 1

        monkeypatch.setattr(pm, "compute_hashes", _tracked_compute)

        poster_bytes = _to_bytes(_make_poster(seed=13))

        with respx.mock(assert_all_called=False) as r:
            r.get("http://provider.example/a.jpg").mock(
                return_value=httpx.Response(200, content=poster_bytes, headers={"Content-Type": "image/jpeg"})
            )
            r.get("http://provider.example/b.jpg").mock(
                return_value=httpx.Response(200, content=poster_bytes, headers={"Content-Type": "image/jpeg"})
            )

            await asyncio.gather(
                pm.hash_url("http://provider.example/a.jpg"),
                pm.hash_url("http://provider.example/b.jpg"),
            )

        assert concurrent["max"] == 1


class TestDecompressionBombAndOversizedDims:
    def test_oversized_declared_pixels_rejected_before_decode(self, monkeypatch):
        monkeypatch.setattr(settings, "POSTER_MAX_PIXELS", 100)  # far below any real poster
        original_max_image_pixels = Image.MAX_IMAGE_PIXELS

        data = _to_bytes(_make_poster(seed=5))
        with pytest.raises(pm.PosterFetchError):
            pm.compute_hashes(data)

        # The global Pillow setting must never be mutated by this module.
        assert Image.MAX_IMAGE_PIXELS == original_max_image_pixels

    def test_undecodable_bytes_raise_poster_fetch_error(self):
        with pytest.raises(pm.PosterFetchError):
            pm.compute_hashes(b"not an image at all")


# --- SSRF -----------------------------------------------------------------------


class TestSSRF:
    async def test_private_ip_target_is_blocked_before_any_http_call(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("10.0.0.5"))
        route = image_mock.get("http://provider.example/poster.jpg")

        with pytest.raises(pm.PosterFetchError):
            await pm.fetch_image("http://provider.example/poster.jpg")

        assert route.call_count == 0

    async def test_private_ip_via_redirect_is_blocked(self, monkeypatch, image_mock):
        # First hop resolves public, redirect target resolves private.
        def _fake_getaddrinfo(host, *a, **k):
            if host == "provider.example":
                return _addrinfo_for("93.184.216.34")
            return _addrinfo_for("169.254.169.254")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        image_mock.get("http://provider.example/poster.jpg").mock(
            return_value=httpx.Response(302, headers={"Location": "http://internal.example/x.jpg"})
        )
        image_mock.get("http://internal.example/x.jpg").mock(
            return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/jpeg"})
        )

        with pytest.raises(pm.PosterFetchError):
            await pm.fetch_image("http://provider.example/poster.jpg")

    async def test_hash_url_returns_none_for_blocked_host_and_caches_negative(
        self, monkeypatch, image_mock,
    ):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("10.0.0.5"))
        route = image_mock.get("http://provider.example/poster.jpg")

        result = await pm.hash_url("http://provider.example/poster.jpg")
        assert result is None
        assert route.call_count == 0

        # Cached negative result — a second call must not re-attempt the fetch.
        result2 = await pm.hash_url("http://provider.example/poster.jpg")
        assert result2 is None
        assert route.call_count == 0


# --- Size / content-type guards ---------------------------------------------------


class TestSizeAndContentTypeGuards:
    async def test_oversized_body_is_rejected_mid_stream(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        monkeypatch.setattr(settings, "POSTER_MAX_BYTES", 10)
        image_mock.get("http://provider.example/big.jpg").mock(
            return_value=httpx.Response(
                200, content=b"0123456789ABCDEF", headers={"Content-Type": "image/jpeg"},
            )
        )

        with pytest.raises(pm.PosterFetchError):
            await pm.fetch_image("http://provider.example/big.jpg")

    async def test_non_image_content_type_is_rejected(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        image_mock.get("http://provider.example/notimage").mock(
            return_value=httpx.Response(200, content=b"<html></html>", headers={"Content-Type": "text/html"})
        )

        with pytest.raises(pm.PosterFetchError):
            await pm.fetch_image("http://provider.example/notimage")

    async def test_svg_content_type_is_rejected(self, monkeypatch, image_mock):
        """W4 review B2: SVG is an image AND a scriptable document. The admin
        poster proxy serves these bytes back from the /admin origin, so a
        provider-controlled thumb_url answering image/svg+xml would run
        script under the operator's ambient Basic Auth when the image is
        opened in a tab."""
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        image_mock.get("http://provider.example/evil.svg").mock(
            return_value=httpx.Response(
                200,
                content=b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>',
                headers={"Content-Type": "image/svg+xml"},
            )
        )

        with pytest.raises(pm.PosterFetchError):
            await pm.fetch_image("http://provider.example/evil.svg")

    async def test_upstream_error_status_is_rejected(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        image_mock.get("http://provider.example/missing.jpg").mock(return_value=httpx.Response(404))

        with pytest.raises(pm.PosterFetchError):
            await pm.fetch_image("http://provider.example/missing.jpg")

    async def test_compressed_response_is_rejected(self, monkeypatch, image_mock):
        """Review L3: a `Content-Encoding` other than identity/absent is
        rejected outright rather than transparently inflated — `aiter_raw()`
        (used by `_download`) never decompresses, so counting raw bytes
        against `POSTER_MAX_BYTES` is only a real memory bound if a
        compressed body can't sneak through in the first place."""
        import gzip

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        # Real gzip bytes (httpx eagerly decodes based on Content-Encoding
        # when constructing a Response from raw `content=`) — the point of
        # this test is the header-based rejection in `_download`, not
        # exercising a malformed-gzip decode error.
        image_mock.get("http://provider.example/gzipped.jpg").mock(
            return_value=httpx.Response(
                200, content=gzip.compress(b"fake-image-bytes"),
                headers={"Content-Type": "image/jpeg", "Content-Encoding": "gzip"},
            )
        )

        with pytest.raises(pm.PosterFetchError):
            await pm.fetch_image("http://provider.example/gzipped.jpg")

    async def test_requests_send_accept_encoding_identity(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        route = image_mock.get("http://provider.example/plain.jpg").mock(
            return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/jpeg"})
        )

        await pm.fetch_image("http://provider.example/plain.jpg")

        assert route.calls[0].request.headers.get("accept-encoding") == "identity"


# --- Malformed URLs (review L1) -----------------------------------------------


class TestMalformedUrls:
    async def test_fetch_image_rejects_unparsable_url_without_leaking_it(self, caplog):
        malformed = "http://[::not-valid-ipv6/poster.jpg"

        with pytest.raises(pm.PosterFetchError) as exc_info:
            await pm.fetch_image(malformed)

        assert malformed not in str(exc_info.value)

    async def test_hash_url_returns_none_and_caches_negative_for_malformed_url(self):
        malformed = "http://[::not-valid-ipv6/poster.jpg"

        result = await pm.hash_url(malformed)
        assert result is None

    def test_is_tmdb_image_host_is_false_for_malformed_url(self):
        assert pm._is_tmdb_image_host("http://[::not-valid-ipv6/x.jpg") is False

    def test_basename_is_empty_for_malformed_url(self):
        assert pm._basename("http://[::not-valid-ipv6/x.jpg") == ""

    async def test_compare_posters_does_not_raise_on_malformed_xtream_url(self):
        result = await pm.compare_posters(
            "http://[::not-valid-ipv6/poster.jpg",
            ["https://image.tmdb.org/t/p/w185/x.jpg"],
        )
        assert result.badge == "unknown"


# --- Total fetch deadline (review L2) -----------------------------------------


class _SlowTransport(httpx.AsyncBaseTransport):
    """A transport that ignores httpx's own per-operation `Timeout`
    entirely (unlike respx, which would still respect it) — the only thing
    that can bound this is `fetch_image`'s own `asyncio.wait_for` wrapper."""

    def __init__(self, delay_seconds: float):
        self._delay = delay_seconds

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self._delay)
        return httpx.Response(200, content=b"x", headers={"Content-Type": "image/jpeg"})


class TestTotalFetchDeadline:
    async def test_slow_trickling_upstream_is_bounded_by_total_deadline(self, monkeypatch):
        monkeypatch.setattr(settings, "POSTER_FETCH_TIMEOUT", 0.05)
        slow_client = httpx.AsyncClient(transport=_SlowTransport(delay_seconds=2.0))

        async def _fake_get_client() -> httpx.AsyncClient:
            return slow_client

        monkeypatch.setattr(pm, "get_client", _fake_get_client)

        start = time.monotonic()
        try:
            with pytest.raises(pm.PosterFetchError):
                await pm.fetch_image("http://provider.example/poster.jpg")
        finally:
            await slow_client.aclose()
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, "fetch must be bounded by POSTER_FETCH_TIMEOUT, not the transport's own delay"


# --- TMDB filename shortcut ---------------------------------------------------


class TestFilenameShortcut:
    async def test_matching_tmdb_basename_is_identical_without_any_fetch(self, image_mock):
        xtream_url = "https://image.tmdb.org/t/p/original/abc123.jpg"
        candidates = [
            "https://image.tmdb.org/t/p/w185/other.jpg",
            "https://image.tmdb.org/t/p/w185/abc123.jpg",
        ]

        result = await pm.compare_posters(xtream_url, candidates)

        assert result.shortcut is True
        assert result.badge == "identical"
        assert result.best_poster_url == candidates[1]
        assert result.variants_compared == 0
        assert len(image_mock.calls) == 0

    async def test_non_matching_tmdb_host_falls_through_to_hashing(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        xtream_url = "https://image.tmdb.org/t/p/original/nomatch.jpg"
        candidate_url = "https://image.tmdb.org/t/p/w185/other.jpg"

        poster_bytes = _to_bytes(_make_poster(seed=7))
        image_mock.get(xtream_url).mock(
            return_value=httpx.Response(200, content=poster_bytes, headers={"Content-Type": "image/jpeg"})
        )
        image_mock.get(candidate_url).mock(
            return_value=httpx.Response(200, content=poster_bytes, headers={"Content-Type": "image/jpeg"})
        )

        result = await pm.compare_posters(xtream_url, [candidate_url])

        assert result.shortcut is False
        assert result.badge == "identical"
        assert result.variants_compared == 1


# --- No api_key/apikey ever reaches an image host --------------------------------


class TestNoProviderKeyLeaksToImageHosts:
    async def test_fetch_image_never_adds_api_key_query_params(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        route = image_mock.get("https://image.tmdb.org/t/p/w185/poster.jpg").mock(
            return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/jpeg"})
        )

        await pm.fetch_image("https://image.tmdb.org/t/p/w185/poster.jpg")

        assert route.call_count == 1
        sent_request = route.calls[0].request
        query = sent_request.url.params
        assert "api_key" not in query
        assert "apikey" not in query

    async def test_compare_posters_never_adds_api_key_query_params(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        poster_bytes = _to_bytes(_make_poster(seed=8))
        xtream_url = "http://provider.example/vod/user/pass/12345.jpg"
        candidate_url = "https://image.tmdb.org/t/p/w185/candidate.jpg"

        route_xtream = image_mock.get(xtream_url).mock(
            return_value=httpx.Response(200, content=poster_bytes, headers={"Content-Type": "image/jpeg"})
        )
        route_candidate = image_mock.get(candidate_url).mock(
            return_value=httpx.Response(200, content=poster_bytes, headers={"Content-Type": "image/jpeg"})
        )

        await pm.compare_posters(xtream_url, [candidate_url])

        for route in (route_xtream, route_candidate):
            for call in route.calls:
                query = call.request.url.params
                assert "api_key" not in query
                assert "apikey" not in query


# --- Cache -----------------------------------------------------------------------


class TestHashUrlCaching:
    async def test_cache_hit_avoids_refetch(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        poster_bytes = _to_bytes(_make_poster(seed=9))
        route = image_mock.get("http://provider.example/poster.jpg").mock(
            return_value=httpx.Response(200, content=poster_bytes, headers={"Content-Type": "image/jpeg"})
        )

        h1 = await pm.hash_url("http://provider.example/poster.jpg")
        h2 = await pm.hash_url("http://provider.example/poster.jpg")

        assert h1 is not None
        assert h1 == h2
        assert route.call_count == 1


# --- Client lifecycle --------------------------------------------------------------


class TestClientLifecycle:
    async def test_get_client_returns_same_instance(self):
        c1 = await pm.get_client()
        c2 = await pm.get_client()
        assert c1 is c2

    async def test_close_is_a_safe_noop_when_never_built(self):
        pm._client = None
        await pm.close()
        assert pm._client is None

    async def test_close_then_get_client_builds_a_fresh_client(self):
        first = await pm.get_client()
        await pm.close()
        assert pm._client is None
        assert first.is_closed
        second = await pm.get_client()
        assert second is not first
        assert not second.is_closed

    async def test_client_has_ssrf_vet_request_hook(self):
        client = await pm.get_client()
        assert ssrf.vet_request in client.event_hooks.get("request", [])


# --- Empty / missing input ---------------------------------------------------------


class TestCompareNoXtreamPoster:
    async def test_no_xtream_url_returns_unknown_without_any_fetch(self, image_mock):
        result = await pm.compare_posters(None, ["https://image.tmdb.org/t/p/w185/x.jpg"])
        assert result.badge == "unknown"
        assert result.shortcut is False
        assert len(image_mock.calls) == 0
