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

import io
import socket

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.config import settings
from app.services import poster_match_service as pm
from app.utils import ssrf

pytestmark = pytest.mark.asyncio


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _reset_poster_state():
    pm._hash_cache_positive.clear()
    pm._hash_cache_negative.clear()
    pm._generic_registry.clear()
    pm._host_semaphores.clear()
    pm._global_semaphore = None
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
    import respx

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

    async def test_upstream_error_status_is_rejected(self, monkeypatch, image_mock):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: _addrinfo_for("93.184.216.34"))
        image_mock.get("http://provider.example/missing.jpg").mock(return_value=httpx.Response(404))

        with pytest.raises(pm.PosterFetchError):
            await pm.fetch_image("http://provider.example/missing.jpg")


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
