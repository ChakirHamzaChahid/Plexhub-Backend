"""`download_service.download_to_disk` — the streaming GET -> `.part` ->
atomic-rename primitive (PH-DL-07 priority 3, Test IDs DL-050..05A).

All HTTP is mocked via `respx` (`xtream_mock` fixture, no base_url — this
file registers full URLs). First Range-aware/streaming respx usage in this
repo — see `docs/40-testplan-media-download.md` §6 risk #4.
"""
from __future__ import annotations

import httpx
import pytest

from app.services.download_service import (
    DownloadCanceled,
    DownloadPermanentError,
    DownloadResult,
    DownloadTransientError,
    download_to_disk,
)

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

URL = "http://provider.example/movie/u/p/1.mkv"
BODY = b"0123456789" * 10  # 100 bytes


# ─── DL-050: nominal transfer ───────────────────────────────────────────────


class TestNominalTransfer:
    async def test_writes_part_then_atomically_renames_to_dest(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "X" / "X.mkv"
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
        )
        progress_calls: list[tuple[int, int | None]] = []

        async def _on_progress(done, total):
            progress_calls.append((done, total))

        result = await download_to_disk(URL, dest, on_progress=_on_progress, chunk_bytes=10)

        assert dest.exists()
        assert dest.read_bytes() == BODY
        assert not dest.with_name(dest.name + ".part").exists()
        assert result == DownloadResult(
            bytes_downloaded=len(BODY), bytes_total=len(BODY),
            already_present=False, resumed=False,
        )
        assert progress_calls, "on_progress must be called at least once"
        seen = [c[0] for c in progress_calls]
        assert seen == sorted(seen), "bytes_done must be monotonically non-decreasing"
        assert seen[-1] == len(BODY)

    async def test_no_content_length_still_completes_with_total_none(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "Y" / "Y.mkv"

        def _chunked(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=BODY)  # no explicit Content-Length header override

        xtream_mock.get(URL).mock(side_effect=_chunked)

        result = await download_to_disk(URL, dest)
        assert dest.read_bytes() == BODY
        # httpx auto-computes Content-Length for a bytes body in test transport,
        # so force the "no header at all" case explicitly to prove the None path:
        assert result.bytes_downloaded == len(BODY)

    async def test_truly_missing_content_length_yields_bytes_total_none(self, tmp_path, xtream_mock):
        """A raw streamed response with NO content-length/content-range header
        at all (e.g. chunked transfer-encoding upstream) must leave
        `bytes_total=None` end to end."""
        dest = tmp_path / "Movies" / "Z" / "Z.mkv"

        def _no_length(request: httpx.Request) -> httpx.Response:
            resp = httpx.Response(200, content=BODY)
            del resp.headers["content-length"]
            return resp

        xtream_mock.get(URL).mock(side_effect=_no_length)

        result = await download_to_disk(URL, dest)
        assert result.bytes_total is None
        assert result.bytes_downloaded == len(BODY)
        assert dest.read_bytes() == BODY


# ─── DL-052/053/054: resume via Range ───────────────────────────────────────


class TestResume:
    async def test_206_range_response_appends_from_existing_part(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "R" / "R.mkv"
        dest.parent.mkdir(parents=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(BODY[:40])

        seen_range: dict = {}

        def _responder(request: httpx.Request) -> httpx.Response:
            seen_range["value"] = request.headers.get("range")
            remainder = BODY[40:]
            return httpx.Response(
                206, content=remainder,
                headers={"Content-Range": f"bytes 40-{len(BODY) - 1}/{len(BODY)}"},
            )

        xtream_mock.get(URL).mock(side_effect=_responder)

        result = await download_to_disk(URL, dest)

        assert seen_range["value"] == "bytes=40-"
        assert dest.read_bytes() == BODY, "the .part must be APPENDED to, not overwritten"
        assert result.resumed is True
        assert result.bytes_downloaded == len(BODY)
        assert result.bytes_total == len(BODY)

    async def test_200_ignoring_range_restarts_from_scratch(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "S" / "S.mkv"
        dest.parent.mkdir(parents=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(b"STALE-GARBAGE-DATA")

        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
        )

        result = await download_to_disk(URL, dest)

        assert dest.read_bytes() == BODY, ".part must be truncated and restarted, not appended to stale data"
        assert result.resumed is False
        assert result.bytes_downloaded == len(BODY)

    async def test_206_with_mismatched_content_range_start_restarts_instead_of_appending(
        self, tmp_path, xtream_mock,
    ):
        """CR-MIN-2 (review): a 206 response whose `Content-Range` start does
        NOT match the `Range: bytes=<resume_from>-` we asked for (a
        non-compliant/misbehaving provider) must be treated like "200, Range
        ignored" — truncate `.part` and restart — never blindly appended to,
        which would leave a gap or a duplicated byte range."""
        dest = tmp_path / "Movies" / "MR" / "MR.mkv"
        dest.parent.mkdir(parents=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(b"X" * 40)  # stale .part -> Range: bytes=40-

        def _responder(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("range") == "bytes=40-"
            # Server claims the resumed range starts at byte 5, not the 40
            # we requested -- simulating a non-compliant provider.
            return httpx.Response(
                206, content=BODY,
                headers={"Content-Range": f"bytes 5-{4 + len(BODY)}/999"},
            )

        xtream_mock.get(URL).mock(side_effect=_responder)

        result = await download_to_disk(URL, dest)

        assert dest.read_bytes() == BODY, (
            ".part must be truncated+restarted on a Content-Range offset "
            "mismatch, never appended to (would corrupt the file)"
        )
        assert result.resumed is False
        assert result.bytes_downloaded == len(BODY)

    async def test_206_with_missing_content_range_header_still_appends(
        self, tmp_path, xtream_mock,
    ):
        """No `Content-Range` header at all on a 206 -> can't prove a mismatch
        -> falls back to the pre-existing lenient append behaviour (no
        regression for a minimal/non-standard but genuinely-resuming server)."""
        dest = tmp_path / "Movies" / "MR2" / "MR2.mkv"
        dest.parent.mkdir(parents=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(BODY[:40])

        def _responder(request: httpx.Request) -> httpx.Response:
            # No `Content-Range` header at all on this 206 (minimal/non-
            # standard server) -> nothing to compare against `resume_from`.
            return httpx.Response(206, content=BODY[40:])

        xtream_mock.get(URL).mock(side_effect=_responder)

        result = await download_to_disk(URL, dest)

        assert dest.read_bytes() == BODY
        assert result.resumed is True

    async def test_416_promotes_part_as_is_without_further_download(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "T" / "T.mkv"
        dest.parent.mkdir(parents=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(BODY)

        call_count = {"n": 0}

        def _416(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(416)

        xtream_mock.get(URL).mock(side_effect=_416)

        result = await download_to_disk(URL, dest)

        assert call_count["n"] == 1
        assert dest.exists()
        assert dest.read_bytes() == BODY
        assert not part.exists()
        assert result.resumed is True
        assert result.bytes_downloaded == len(BODY)


# ─── DL-055: skip-if-exists ─────────────────────────────────────────────────


class TestSkipIfExists:
    async def test_existing_dest_with_no_part_skips_download(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "U" / "U.mkv"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(BODY)

        route = xtream_mock.get(URL).mock(return_value=httpx.Response(200, content=b"should not be fetched"))

        result = await download_to_disk(URL, dest)

        assert route.call_count == 0, "no GET must be issued when the final file already exists"
        assert result.already_present is True
        assert result.bytes_downloaded == len(BODY)
        assert dest.read_bytes() == BODY  # untouched


# ─── DL-05A: permanent errors -> DownloadPermanentError ────────────────────


class TestPermanentErrors:
    @pytest.mark.parametrize("status_code", [404, 403])
    async def test_404_403_raise_permanent_error(self, tmp_path, xtream_mock, status_code):
        dest = tmp_path / "Movies" / "P" / "P.mkv"
        xtream_mock.get(URL).mock(return_value=httpx.Response(status_code))

        with pytest.raises(DownloadPermanentError):
            await download_to_disk(URL, dest)
        assert not dest.exists()

    async def test_error_content_type_raises_permanent_error(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "Q" / "Q.mkv"
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=b"<html>oops</html>", headers={"Content-Type": "text/html"})
        )

        with pytest.raises(DownloadPermanentError):
            await download_to_disk(URL, dest)
        assert not dest.exists()

    async def test_unexpected_2xx_3xx_status_raises_permanent_error(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "W" / "W.mkv"
        xtream_mock.get(URL).mock(return_value=httpx.Response(201))

        with pytest.raises(DownloadPermanentError):
            await download_to_disk(URL, dest)


# ─── DL-01: redirects are never followed (SSRF hardening, review fix #2) ───


class TestRedirectNeverFollowed:
    """A malicious/MITM'd provider returning a 3xx must never have its
    `Location` target fetched or its body written to disk — a direct Xtream
    stream URL never legitimately redirects, and following one would let a
    hostile response point at an internal address (loopback/RFC1918/
    `169.254.169.254`)."""

    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
    async def test_redirect_status_raises_permanent_error_without_fetching_target(
        self, tmp_path, xtream_mock, status_code,
    ):
        dest = tmp_path / "Movies" / "RD" / "RD.mkv"
        redirect_target = "http://169.254.169.254/latest/meta-data/secret.mkv"

        origin_route = xtream_mock.get(URL).mock(
            return_value=httpx.Response(
                status_code, headers={"Location": redirect_target},
            )
        )
        target_route = xtream_mock.get(redirect_target).mock(
            return_value=httpx.Response(200, content=b"internal-secret-payload")
        )

        with pytest.raises(DownloadPermanentError):
            await download_to_disk(URL, dest)

        assert origin_route.call_count == 1
        assert target_route.call_count == 0, "the redirect target must NEVER be fetched"
        assert not dest.exists()
        part = dest.with_name(dest.name + ".part")
        assert not part.exists()

    async def test_302_leaves_no_bytes_written_to_disk(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "RD2" / "RD2.mkv"
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(302, headers={"Location": "http://10.0.0.1/evil.mkv"})
        )

        with pytest.raises(DownloadPermanentError) as excinfo:
            await download_to_disk(URL, dest)
        assert "302" in str(excinfo.value)
        assert not dest.parent.exists() or list(dest.parent.iterdir()) == []


# ─── DL-057: transient errors -> DownloadTransientError ────────────────────


class TestTransientErrors:
    @pytest.mark.parametrize("status_code", [500, 502, 503, 429])
    async def test_5xx_and_429_raise_transient_error(self, tmp_path, xtream_mock, status_code):
        dest = tmp_path / "Movies" / "V" / "V.mkv"
        xtream_mock.get(URL).mock(return_value=httpx.Response(status_code))

        with pytest.raises(DownloadTransientError):
            await download_to_disk(URL, dest)

    async def test_timeout_maps_to_transient_error_without_leaking_original_repr(
        self, tmp_path, xtream_mock,
    ):
        dest = tmp_path / "Movies" / "T2" / "T2.mkv"
        xtream_mock.get(URL).mock(side_effect=httpx.ConnectTimeout("connect timed out to secret-host"))

        with pytest.raises(DownloadTransientError) as excinfo:
            await download_to_disk(URL, dest)
        assert str(excinfo.value) == "network timeout"
        assert "secret-host" not in str(excinfo.value)

    async def test_transport_error_maps_to_transient_error(self, tmp_path, xtream_mock):
        dest = tmp_path / "Movies" / "T3" / "T3.mkv"
        xtream_mock.get(URL).mock(side_effect=httpx.ConnectError("connection refused to secret-host"))

        with pytest.raises(DownloadTransientError) as excinfo:
            await download_to_disk(URL, dest)
        assert str(excinfo.value) == "network error"
        assert "secret-host" not in str(excinfo.value)


# ─── Cancel mid-transfer ────────────────────────────────────────────────────


class TestCancelDuringTransfer:
    async def test_cancel_check_true_raises_downloadcanceled_and_leaves_part(
        self, tmp_path, xtream_mock,
    ):
        dest = tmp_path / "Movies" / "C" / "C.mkv"
        xtream_mock.get(URL).mock(
            return_value=httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
        )

        chunks_seen = {"n": 0}

        async def _cancel_after_first_chunk():
            chunks_seen["n"] += 1
            return chunks_seen["n"] > 1

        with pytest.raises(DownloadCanceled):
            await download_to_disk(URL, dest, cancel_check=_cancel_after_first_chunk, chunk_bytes=10)

        assert not dest.exists(), "a canceled transfer must never promote to the final file"
        part = dest.with_name(dest.name + ".part")
        assert part.exists(), "the .part must be left on disk for a later resume"
        assert part.stat().st_size > 0
