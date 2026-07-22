"""`app/dav/relay.py` — the DAV byte-relay to the Xtream upstream.

Mirrors `tests/test_download_transfer.py`'s structure/fixtures (respx via
the `xtream_mock` fixture, no base_url — full URLs registered here). First
streaming-response (`client.send(..., stream=True)`, not `client.stream()`)
respx usage in this repo, needed because `open_upstream` hands the caller a
live, not-yet-drained response.
"""
from __future__ import annotations

import contextlib
import logging

import httpx
import pytest

from app.config import settings
from app.dav import relay as relay_module
from app.dav.relay import (
    UpstreamError,
    UpstreamNotFound,
    UpstreamStream,
    UpstreamTimeout,
    close_client,
    get_client,
    open_upstream,
)

URL = "http://provider.example/movie/u/p/1.mkv"
BODY = b"0123456789" * 10  # 100 bytes


async def _drain(stream: UpstreamStream) -> bytes:
    chunks: list[bytes] = []
    try:
        async for chunk in stream.body:
            chunks.append(chunk)
    finally:
        await stream.aclose()
    return b"".join(chunks)


@contextlib.contextmanager
def _capture_dav_logger(caplog):
    """`app/main.py` sets the `plexhub` logger (parent of this module's own
    `plexhub.dav`) to `propagate=False` — pytest's caplog handler, attached
    on the ROOT logger, never sees a record climbing past `plexhub` through
    `caplog.at_level(...)` alone (`propagate=False` only stops a record
    climbing PAST `plexhub`; it doesn't stop a handler attached directly
    on/under it from firing). Without this, every `caplog.records` loop in
    `TestNoSecretLeakage` below iterates an EMPTY list — a real leak into a
    `plexhub.dav` log line would pass those tests silently. Mirrors
    `tests/test_dav_router.py`'s `test_size_mismatch_warning_never_logs_the_
    url` workaround: attach the handler directly onto `plexhub.dav` for the
    scope of the `with` block instead of relying on propagation."""
    dav_logger = logging.getLogger("plexhub.dav")
    dav_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="plexhub.dav"):
            yield
    finally:
        dav_logger.removeHandler(caplog.handler)


# ─── Pooled client ───────────────────────────────────────────────────────


class TestPooledClient:
    async def test_get_client_returns_the_same_instance_across_calls(self):
        client1 = await get_client()
        client2 = await get_client()
        assert client1 is client2

    async def test_close_client_is_a_safe_no_op_when_none_was_ever_built(self):
        """`app/main.py`'s lifespan shutdown calls `close_client()`
        unconditionally, even for a `DAV_ENABLED=false` deployment that
        never built a client in the first place (module-level `_client`
        stays `None` until the first `get_client()`/`open_upstream` call) —
        must never raise in that case."""
        relay_module._client = None  # force the "never built" state
        await close_client()
        assert relay_module._client is None

    async def test_close_client_closes_and_clears_the_pooled_client(self):
        """After `close_client()`: the pooled client is actually closed
        (`httpx.AsyncClient.is_closed`) AND the module-level singleton is
        reset, so the NEXT `get_client()` call builds a fresh, usable client
        instead of handing back a closed one."""
        first = await get_client()
        assert not first.is_closed

        await close_client()

        assert relay_module._client is None
        assert first.is_closed

        second = await get_client()
        assert second is not first
        assert not second.is_closed
        # Leave the module in a working state — `_client` is shared process-
        # wide across every other test in this file (and test_dav_router.py)
        # exactly like `account_throttle`'s singleton (see app/dav/throttle.
        # py's own module docstring); a subsequent `get_client()`/
        # `open_upstream` call anywhere else must not see a closed client.
        await close_client()


# ─── Nominal 200/206 pass-through ───────────────────────────────────────


class TestFullStream:
    async def test_200_full_body_matches_exactly(self, xtream_mock):
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
        )

        stream = await open_upstream(URL, range_header=None)

        assert stream.status_code == 200
        assert stream.headers["Content-Length"] == str(len(BODY))
        assert stream.headers["Accept-Ranges"] == "bytes"
        assert await _drain(stream) == BODY

    async def test_content_type_is_passed_through(self, xtream_mock):
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(
                200, content=BODY,
                headers={"Content-Length": str(len(BODY)), "Content-Type": "video/x-matroska"},
            )
        )
        stream = await open_upstream(URL, range_header=None)
        assert stream.headers["Content-Type"] == "video/x-matroska"
        await _drain(stream)


class TestRangePassThrough:
    async def test_range_header_is_forwarded_and_206_is_passed_through(self, xtream_mock):
        seen_range: dict = {}

        def _responder(request: httpx.Request) -> httpx.Response:
            seen_range["value"] = request.headers.get("range")
            return httpx.Response(
                206, content=BODY[10:20],
                headers={"Content-Range": f"bytes 10-19/{len(BODY)}", "Content-Length": "10"},
            )

        xtream_mock.get(URL).mock(side_effect=_responder)

        stream = await open_upstream(URL, range_header="bytes=10-19")

        assert seen_range["value"] == "bytes=10-19"
        assert stream.status_code == 206
        assert stream.headers["Content-Range"] == f"bytes 10-19/{len(BODY)}"
        assert stream.headers["Accept-Ranges"] == "bytes"
        assert await _drain(stream) == BODY[10:20]

    async def test_user_agent_is_xtream_user_agent(self, xtream_mock):
        seen_ua: dict = {}

        def _responder(request: httpx.Request) -> httpx.Response:
            seen_ua["value"] = request.headers.get("user-agent")
            return httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})

        xtream_mock.get(URL).mock(side_effect=_responder)

        stream = await open_upstream(URL, range_header=None)
        await _drain(stream)
        assert seen_ua["value"] == settings.XTREAM_USER_AGENT


class Test416PassThrough:
    async def test_upstream_416_is_returned_as_is_with_empty_body(self, xtream_mock):
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(416, headers={"Content-Range": f"bytes */{len(BODY)}"})
        )

        stream = await open_upstream(URL, range_header="bytes=99999-100000")

        assert stream.status_code == 416
        assert stream.headers["Content-Range"] == f"bytes */{len(BODY)}"
        assert await _drain(stream) == b""


# ─── Range shim: upstream ignores Range, backend synthesizes 206 ─────────


class TestRangeShim:
    async def test_upstream_ignores_range_client_still_gets_206(self, xtream_mock, monkeypatch):
        monkeypatch.setattr(settings, "DAV_RANGE_SHIM", True)
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
        )

        stream = await open_upstream(URL, range_header="bytes=10-19")

        assert stream.status_code == 206
        assert stream.headers["Content-Range"] == f"bytes 10-19/{len(BODY)}"
        assert stream.headers["Content-Length"] == "10"
        assert await _drain(stream) == BODY[10:20]

    async def test_open_ended_range_shim_serves_to_end_of_file(self, xtream_mock, monkeypatch):
        monkeypatch.setattr(settings, "DAV_RANGE_SHIM", True)
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
        )

        stream = await open_upstream(URL, range_header="bytes=90-")

        assert stream.status_code == 206
        assert stream.headers["Content-Range"] == f"bytes 90-{len(BODY) - 1}/{len(BODY)}"
        assert await _drain(stream) == BODY[90:]

    async def test_shim_synthesizes_416_when_range_start_is_beyond_total(self, xtream_mock, monkeypatch):
        monkeypatch.setattr(settings, "DAV_RANGE_SHIM", True)
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
        )

        stream = await open_upstream(URL, range_header=f"bytes={len(BODY) + 50}-")

        assert stream.status_code == 416
        assert stream.headers["Content-Range"] == f"bytes */{len(BODY)}"
        assert await _drain(stream) == b""

    async def test_shim_disabled_leaves_ignored_range_as_raw_200(self, xtream_mock, monkeypatch):
        monkeypatch.setattr(settings, "DAV_RANGE_SHIM", False)
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
        )

        stream = await open_upstream(URL, range_header="bytes=10-19")

        assert stream.status_code == 200
        assert "Content-Range" not in stream.headers
        assert await _drain(stream) == BODY

    async def test_shim_falls_back_to_pass_through_when_content_length_is_missing(
        self, xtream_mock, monkeypatch,
    ):
        monkeypatch.setattr(settings, "DAV_RANGE_SHIM", True)

        def _no_length(request: httpx.Request) -> httpx.Response:
            resp = httpx.Response(200, content=BODY)
            del resp.headers["content-length"]
            return resp

        xtream_mock.get(URL).mock(side_effect=_no_length)

        stream = await open_upstream(URL, range_header="bytes=10-19")

        assert stream.status_code == 200, "can't slice without a known total -> unshimmed pass-through"
        assert await _drain(stream) == BODY


# ─── Redirects (SSRF-guarded, mirrors download_service) ───────────────────


class TestRedirects:
    CDN = "http://93.184.216.34/cdn/1.mkv"  # public IP literal, no DNS needed
    PRIVATE = "http://10.0.0.5/internal.mkv"  # RFC1918

    async def test_follows_public_redirect_with_ua_preserved(self, xtream_mock):
        seen_ua: dict = {}

        def _target(request: httpx.Request) -> httpx.Response:
            seen_ua["value"] = request.headers.get("user-agent")
            return httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})

        xtream_mock.get(URL).mock(return_value=httpx.Response(302, headers={"Location": self.CDN}))
        xtream_mock.get(self.CDN).mock(side_effect=_target)

        stream = await open_upstream(URL, range_header=None)

        assert stream.status_code == 200
        assert await _drain(stream) == BODY
        assert seen_ua["value"] == settings.XTREAM_USER_AGENT

    async def test_rejects_redirect_to_private_address_without_fetching_target(self, xtream_mock):
        origin_route = xtream_mock.get(URL).mock(
            return_value=httpx.Response(302, headers={"Location": self.PRIVATE})
        )
        target_route = xtream_mock.get(self.PRIVATE).mock(
            return_value=httpx.Response(200, content=b"internal-secret-payload")
        )

        with pytest.raises(UpstreamError):
            await open_upstream(URL, range_header=None)

        assert origin_route.call_count == 1
        assert target_route.call_count == 0, "the redirect target must NEVER be fetched"

    async def test_redirect_to_metadata_ip_is_rejected(self, xtream_mock):
        target = "http://169.254.169.254/latest/meta-data/secret"
        xtream_mock.get(URL).mock(return_value=httpx.Response(302, headers={"Location": target}))
        target_route = xtream_mock.get(target).mock(return_value=httpx.Response(200, content=b"x"))

        with pytest.raises(UpstreamError):
            await open_upstream(URL, range_header=None)
        assert target_route.call_count == 0

    async def test_too_many_redirects_raises_upstream_error(self, xtream_mock, monkeypatch):
        monkeypatch.setattr(settings, "DOWNLOAD_MAX_REDIRECTS", 0)
        xtream_mock.get(URL).mock(return_value=httpx.Response(302, headers={"Location": self.CDN}))

        with pytest.raises(UpstreamError):
            await open_upstream(URL, range_header=None)


# ─── Upstream error mapping ─────────────────────────────────────────────


class TestUpstreamErrorMapping:
    @pytest.mark.parametrize("status_code", [400, 403, 404])
    async def test_4xx_raises_upstream_not_found(self, xtream_mock, status_code):
        xtream_mock.get(URL).mock(return_value=httpx.Response(status_code))
        with pytest.raises(UpstreamNotFound):
            await open_upstream(URL, range_header=None)

    @pytest.mark.parametrize("status_code", [500, 502, 503])
    async def test_5xx_raises_upstream_error(self, xtream_mock, status_code):
        xtream_mock.get(URL).mock(return_value=httpx.Response(status_code))
        with pytest.raises(UpstreamError):
            await open_upstream(URL, range_header=None)

    async def test_unexpected_2xx_status_raises_upstream_error(self, xtream_mock):
        xtream_mock.get(URL).mock(return_value=httpx.Response(201))
        with pytest.raises(UpstreamError):
            await open_upstream(URL, range_header=None)

    async def test_connect_timeout_raises_upstream_timeout(self, xtream_mock):
        xtream_mock.get(URL).mock(side_effect=httpx.ConnectTimeout("connect timed out to secret-host"))
        with pytest.raises(UpstreamTimeout):
            await open_upstream(URL, range_header=None)

    async def test_transport_error_raises_upstream_error(self, xtream_mock):
        xtream_mock.get(URL).mock(side_effect=httpx.ConnectError("connection refused to secret-host"))
        with pytest.raises(UpstreamError):
            await open_upstream(URL, range_header=None)


# ─── No credential/URL leakage ──────────────────────────────────────────


class TestNoSecretLeakage:
    _TOKEN = "SEKRET-XTREAM-TOKEN-99"  # distinctive -> provable absence from logs/exceptions
    _URL = f"http://provider.example/movie/u/{_TOKEN}/1.mkv"

    async def test_404_error_message_never_contains_the_url_or_token(self, xtream_mock, caplog):
        xtream_mock.get(self._URL).mock(return_value=httpx.Response(404))

        with _capture_dav_logger(caplog):
            with pytest.raises(UpstreamNotFound) as excinfo:
                await open_upstream(self._URL, range_header=None)

        assert self._TOKEN not in str(excinfo.value)
        # `open_upstream`'s 4xx path (`raise UpstreamNotFound(...)`) never
        # logs anything itself (only the redirect-rejection path below does)
        # -> caplog.records is legitimately empty here even with capture
        # correctly wired. This loop is still meaningful as a regression
        # guard should logging ever be added to this path; the *capture
        # plumbing itself* is proven non-vacuous by the redirect test below,
        # which asserts a record IS present.
        for record in caplog.records:
            assert self._TOKEN not in record.getMessage()

    async def test_timeout_error_message_never_contains_the_url_or_underlying_repr(
        self, xtream_mock, caplog,
    ):
        xtream_mock.get(self._URL).mock(
            side_effect=httpx.ConnectTimeout(f"connect timed out to {self._TOKEN}")
        )

        with _capture_dav_logger(caplog):
            with pytest.raises(UpstreamTimeout) as excinfo:
                await open_upstream(self._URL, range_header=None)

        assert self._TOKEN not in str(excinfo.value)
        # Same note as the 404 test above: the timeout path doesn't log
        # either, `caplog.records` is legitimately empty.
        for record in caplog.records:
            assert self._TOKEN not in record.getMessage()

    async def test_rejected_redirect_never_logs_the_url_or_token(self, xtream_mock, caplog):
        private_target = f"http://10.0.0.9/internal/{self._TOKEN}.mkv"
        xtream_mock.get(self._URL).mock(
            return_value=httpx.Response(302, headers={"Location": private_target})
        )
        xtream_mock.get(private_target).mock(return_value=httpx.Response(200, content=b"x"))

        with _capture_dav_logger(caplog):
            with pytest.raises(UpstreamError) as excinfo:
                await open_upstream(self._URL, range_header=None)

        assert self._TOKEN not in str(excinfo.value)

        # Positive proof the capture plumbing is actually wired (not just
        # "the loop below never ran"): `open_upstream` DOES log on this
        # path (`logger.warning("DAV relay: rejected redirect ...")`,
        # relay.py) — assert that record was really captured before trusting
        # the negative assertion that follows.
        messages = [record.getMessage() for record in caplog.records]
        assert any("rejected redirect" in message for message in messages), (
            f"expected the relay's own 'rejected redirect' warning to be "
            f"captured (proves _capture_dav_logger actually sees "
            f"plexhub.dav records) — got: {messages!r}"
        )

        for record in caplog.records:
            assert self._TOKEN not in record.getMessage()
