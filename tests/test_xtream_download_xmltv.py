"""`XtreamService.download_xmltv` (Lot 10 sub-lot D, FR-BE-560): the streaming
replacement for the removed `get_xmltv`. All HTTP is mocked with respx — no
network."""
from __future__ import annotations

import gzip

import httpx
import pytest
import respx

from app.services import xtream_service as xs_module
from app.services.xtream_service import XmltvTooLargeError, XtreamService
from tests._records import Records

SECRET = "S3CR3T-PASSWORD"
URL = "http://prov.example:8080/xmltv.php"


class _Acc:
    base_url = "http://prov.example"
    port = 8080
    username = "user"
    password = SECRET


class _ChunkedStream(httpx.AsyncByteStream):
    """A body with NO Content-Length (chunked), to exercise the running byte
    counter instead of the declared-length shortcut."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c


@pytest.fixture
def svc():
    return XtreamService()  # fresh client: no state shared with the singleton


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr(xs_module.asyncio, "sleep", _instant)


async def test_streams_body_to_file_and_sends_credentials_as_params(svc, tmp_path):
    dest = tmp_path / "out.xml"
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"<tv>hello</tv>"))
        written = await svc.download_xmltv(_Acc(), dest, max_bytes=1_000)
    await svc.close()

    assert written == len(b"<tv>hello</tv>")
    assert dest.read_bytes() == b"<tv>hello</tv>"
    sent = route.calls.last.request.url.params
    assert sent["username"] == "user" and sent["password"] == SECRET


async def test_declared_content_length_over_cap_fails_before_reading(svc, tmp_path):
    dest = tmp_path / "out.xml"
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(
            200, content=b"x" * 50, headers={"Content-Length": "999999"},
        ))
        with pytest.raises(XmltvTooLargeError):
            await svc.download_xmltv(_Acc(), dest, max_bytes=1_000)
    await svc.close()


async def test_running_counter_cuts_a_chunked_body_over_the_cap(svc, tmp_path):
    dest = tmp_path / "out.xml"
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(
            200, stream=_ChunkedStream([b"a" * 600, b"b" * 600, b"c" * 600]),
        ))
        with pytest.raises(XmltvTooLargeError):
            await svc.download_xmltv(_Acc(), dest, max_bytes=1_000)
    await svc.close()


async def test_gzip_bomb_is_cut_on_the_decompressed_size(svc, tmp_path):
    bomb = gzip.compress(b"\0" * 5_000_000)  # a few KB on the wire, 5 MB inflated
    assert len(bomb) < 100_000
    dest = tmp_path / "out.xml"
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(
            200, content=bomb, headers={"Content-Encoding": "gzip"},
        ))
        with pytest.raises(XmltvTooLargeError):
            await svc.download_xmltv(_Acc(), dest, max_bytes=1_000_000)
    await svc.close()


async def test_503_is_retried_and_the_file_restarts_clean(svc, tmp_path):
    dest = tmp_path / "out.xml"
    with respx.mock:
        route = respx.get(URL).mock(side_effect=[
            httpx.Response(503, headers={"Retry-After": "1"}),
            httpx.Response(200, content=b"<tv/>"),
        ])
        with Records("plexhub") as logs:
            written = await svc.download_xmltv(_Acc(), dest, max_bytes=1_000)
    await svc.close()

    assert route.call_count == 2
    assert dest.read_bytes() == b"<tv/>" and written == 5
    assert "got HTTP 503" in logs.text, "the retry must have been logged"
    assert SECRET not in logs.text, "the credentialed URL must never be logged"


async def test_connect_error_is_retried_then_raised_without_leaking(svc, tmp_path):
    dest = tmp_path / "out.xml"
    with respx.mock:
        route = respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
        with Records("plexhub") as logs:
            with pytest.raises(httpx.ConnectError):
                await svc.download_xmltv(_Acc(), dest, max_bytes=1_000)
    await svc.close()

    assert route.call_count == 4  # 1 attempt + 3 retries (1s, 2s, 4s backoff)
    assert "attempt 1 failed (ConnectError)" in logs.text
    assert SECRET not in logs.text


async def test_404_is_not_retried(svc, tmp_path):
    dest = tmp_path / "out.xml"
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError):
            await svc.download_xmltv(_Acc(), dest, max_bytes=1_000)
    await svc.close()
    assert route.call_count == 1


async def test_get_xmltv_no_longer_exists():
    """FR-BE-560: the whole-document-in-a-str variant was removed, not kept as
    a second, memory-hungry path."""
    assert not hasattr(XtreamService, "get_xmltv")
