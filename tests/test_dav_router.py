"""DAV-2: `app/api/dav.py` — the WebDAV HTTP surface (OPTIONS/PROPFIND/HEAD/GET
on `/dav`), wired end-to-end through the real `app.main.app` (via the
`api_client` fixture from `tests/conftest.py` — GZipMiddleware/CORS included,
same as every other router test in this repo).

Tree resolution is stubbed out (`install_tree` monkeypatches
`app.dav.vfs.dav_tree_cache.get`) — this file is NOT re-testing
`build_dav_tree()`'s DB aggregation (`tests/test_dav_tree.py`'s job) nor
`render_multistatus()`'s XML shape (`tests/test_dav_propfind.py`'s job) nor
`open_upstream()`'s Range/redirect/SSRF handling (`tests/test_dav_relay.py`'s
job) — it only proves the ROUTER correctly walks an already-built tree,
enforces auth/Depth/method rules, and wires throttle+relay together for GET.
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.dav as dav_module
from app.config import settings
from app.dav import relay as relay_module
from app.db import database as db_module
from app.dav.throttle import ThrottleTimeout, account_throttle
from app.dav.vfs import DavEntry, DavTree, dav_tree_cache
from app.models.database import XtreamAccount

pytestmark = pytest.mark.asyncio

NS = "{DAV:}"

# Predicted by app.services.stream_service.build_stream_url for the account
# fixture below (base_url + port 80 -> default scheme, no ":80" suffix) and
# rating_key "vod_1.mkv" (movie, stream id "1", extension ".mkv") — mirrors
# tests/test_dav_relay.py's own URL constant.
STREAM_URL = "http://provider.example/movie/u/p/1.mkv"

FILE_PATH = "Films/Dune (2021)/Dune (2021).mkv"
DIR_PATH = "Films/Dune (2021)"


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _dav_enabled(monkeypatch):
    """Every test in this file starts from a fully-configured, enabled /dav
    (individual tests override DAV_ENABLED/DAV_PASSWORD to exercise the
    fail-closed gates). DAV_QUEUE_TIMEOUT_SECONDS kept short so a throttle
    bug (a leaked permit) fails the test fast instead of hanging ~30s."""
    monkeypatch.setattr(settings, "DAV_ENABLED", True)
    monkeypatch.setattr(settings, "DAV_USERNAME", "plexdav")
    monkeypatch.setattr(settings, "DAV_PASSWORD", "s3cret")
    monkeypatch.setattr(settings, "DAV_UPSTREAM_PER_ACCOUNT", 1)
    monkeypatch.setattr(settings, "DAV_QUEUE_TIMEOUT_SECONDS", 2)


@pytest_asyncio.fixture(autouse=True)
async def _wire_db(db_engine, monkeypatch) -> async_sessionmaker:
    """Point get_db()'s underlying async_session_factory at the isolated
    in-memory engine from conftest — mirrors tests/test_auth_guard.py's
    `_wire_test_db`. Autouse: dav_dispatch opens `Depends(get_db)` on EVERY
    request (OPTIONS/PROPFIND/HEAD included), so every test needs a valid,
    isolated DB even if it never seeds a row."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    return factory


@pytest_asyncio.fixture
async def seed_account(_wire_db):
    """`await seed_account(**overrides)` inserts one XtreamAccount (defaults
    below) and returns it. Defaults match STREAM_URL above exactly."""

    async def _seed(**overrides) -> XtreamAccount:
        defaults = dict(
            id="acct1", label="Test provider", base_url="http://provider.example",
            port=80, username="u", password="p", max_connections=1,
            is_active=True, created_at=0,
        )
        defaults.update(overrides)
        account = XtreamAccount(**defaults)
        async with _wire_db() as s:
            s.add(account)
            await s.commit()
        return account

    return _seed


def _make_tree(*, size: int | None = 1000, server_id: str = "xtream_acct1", rating_key: str = "vod_1.mkv") -> DavTree:
    entries = {
        "": DavEntry(name="", is_dir=True),
        "Films": DavEntry(name="Films", is_dir=True),
        DIR_PATH: DavEntry(name="Dune (2021)", is_dir=True),
        FILE_PATH: DavEntry(
            name="Dune (2021).mkv", is_dir=False, size=size,
            server_id=server_id, rating_key=rating_key,
        ),
    }
    children = {
        "": ["Films"],
        "Films": ["Dune (2021)"],
        DIR_PATH: ["Dune (2021).mkv"],
    }
    return DavTree(entries=entries, children=children)


@pytest.fixture
def install_tree(monkeypatch):
    """`install_tree(tree)` makes `dav_tree_cache.get()` return `tree`
    without going through the real build_dav_tree()/DB aggregation — the
    object is patched (not the name), so it's visible from every module that
    imported the `dav_tree_cache` singleton (app/api/dav.py included)."""

    def _install(tree: DavTree) -> DavTree:
        async def _get() -> DavTree:
            return tree

        monkeypatch.setattr(dav_tree_cache, "get", _get)
        return tree

    return _install


@pytest.fixture
def default_tree(install_tree) -> DavTree:
    return install_tree(_make_tree())


def _auth() -> tuple[str, str]:
    return ("plexdav", "s3cret")


def _url(path: str) -> str:
    return f"/dav/{quote(path, safe='/')}"


# ─── Auth ────────────────────────────────────────────────────────────────


class TestAuth:
    async def test_no_credentials_is_401_with_www_authenticate(self, api_client, default_tree):
        resp = await api_client.request("PROPFIND", _url(FILE_PATH), headers={"Depth": "0"})
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Basic"

    async def test_wrong_credentials_is_401(self, api_client, default_tree):
        resp = await api_client.request(
            "PROPFIND", _url(FILE_PATH), headers={"Depth": "0"}, auth=("plexdav", "wrong"),
        )
        assert resp.status_code == 401

    async def test_empty_dav_password_is_503_even_with_credentials(self, api_client, monkeypatch, default_tree):
        monkeypatch.setattr(settings, "DAV_PASSWORD", "")
        resp = await api_client.request(
            "PROPFIND", _url(FILE_PATH), headers={"Depth": "0"}, auth=_auth(),
        )
        assert resp.status_code == 503

    async def test_correct_credentials_pass_auth(self, api_client, default_tree):
        resp = await api_client.request(
            "PROPFIND", _url(FILE_PATH), headers={"Depth": "0"}, auth=_auth(),
        )
        assert resp.status_code == 207


class TestDavEnabledGate:
    async def test_disabled_is_503_even_authenticated(self, api_client, monkeypatch, default_tree):
        monkeypatch.setattr(settings, "DAV_ENABLED", False)
        resp = await api_client.request(
            "PROPFIND", _url(FILE_PATH), headers={"Depth": "0"}, auth=_auth(),
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "WebDAV disabled"

    async def test_disabled_blocks_options_too(self, api_client, monkeypatch, default_tree):
        monkeypatch.setattr(settings, "DAV_ENABLED", False)
        resp = await api_client.options(_url(""), auth=_auth())
        assert resp.status_code == 503


# ─── OPTIONS ─────────────────────────────────────────────────────────────


class TestOptions:
    async def test_options_204_with_dav_and_allow_headers(self, api_client, default_tree):
        resp = await api_client.options(_url(""), auth=_auth())
        assert resp.status_code == 204
        assert resp.headers.get("dav") == "1"
        allow = resp.headers.get("allow", "")
        for method in ("OPTIONS", "PROPFIND", "HEAD", "GET"):
            assert method in allow


# ─── PROPFIND ────────────────────────────────────────────────────────────


class TestPropfind:
    async def test_depth_0_on_file_returns_self_only(self, api_client, default_tree):
        resp = await api_client.request(
            "PROPFIND", _url(FILE_PATH), headers={"Depth": "0"}, auth=_auth(),
        )
        assert resp.status_code == 207
        assert resp.headers["content-type"].startswith("application/xml")
        root = ET.fromstring(resp.content)
        responses = root.findall(f"{NS}response")
        assert len(responses) == 1
        prop = responses[0].find(f"{NS}propstat/{NS}prop")
        assert prop.find(f"{NS}getcontentlength").text == "1000"
        resourcetype = prop.find(f"{NS}resourcetype")
        assert list(resourcetype) == []  # empty -> not a collection

    async def test_depth_1_on_directory_returns_self_and_children(self, api_client, default_tree):
        resp = await api_client.request(
            "PROPFIND", _url(DIR_PATH), headers={"Depth": "1"}, auth=_auth(),
        )
        assert resp.status_code == 207
        root = ET.fromstring(resp.content)
        hrefs = [r.find(f"{NS}href").text for r in root.findall(f"{NS}response")]
        assert len(hrefs) == 2
        assert any(h.endswith("/Dune%20%282021%29/") for h in hrefs)  # self
        assert any(h.endswith("Dune%20%282021%29.mkv") for h in hrefs)  # child

    async def test_depth_1_on_file_is_self_only(self, api_client, default_tree):
        """No children exist on a file — Depth 0 and 1 render identically."""
        resp = await api_client.request(
            "PROPFIND", _url(FILE_PATH), headers={"Depth": "1"}, auth=_auth(),
        )
        root = ET.fromstring(resp.content)
        assert len(root.findall(f"{NS}response")) == 1

    @pytest.mark.parametrize("depth", [None, "infinity"])
    async def test_unsupported_depth_is_403(self, api_client, default_tree, depth):
        headers = {"Depth": depth} if depth is not None else {}
        resp = await api_client.request("PROPFIND", _url(FILE_PATH), headers=headers, auth=_auth())
        assert resp.status_code == 403

    async def test_missing_entry_is_404(self, api_client, default_tree):
        resp = await api_client.request(
            "PROPFIND", _url("Films/Nope.mkv"), headers={"Depth": "0"}, auth=_auth(),
        )
        assert resp.status_code == 404

    async def test_root_depth_1_lists_top_level(self, api_client, default_tree):
        resp = await api_client.request("PROPFIND", _url(""), headers={"Depth": "1"}, auth=_auth())
        assert resp.status_code == 207
        root = ET.fromstring(resp.content)
        assert len(root.findall(f"{NS}response")) == 2  # root itself + "Films"


# ─── HEAD ────────────────────────────────────────────────────────────────


class TestHead:
    async def test_head_on_file_is_answered_from_tree_with_zero_upstream_calls(
        self, api_client, default_tree, xtream_mock,
    ):
        # xtream_mock wraps this test in `respx.mock(assert_all_called=False)`
        # with NO route registered: `assert_all_called=False` only relaxes the
        # "every registered route must be hit" check — respx still raises on
        # any outbound call it doesn't have a route for (its default
        # `assert_all_mocked=True`), so an actual HTTP call here would still
        # fail this test — that IS the "zero calls" proof.
        resp = await api_client.head(_url(FILE_PATH), auth=_auth())
        assert resp.status_code == 200
        assert resp.headers["content-length"] == "1000"
        assert resp.headers["accept-ranges"] == "bytes"
        assert resp.headers["content-type"] == "video/x-matroska"
        assert resp.content == b""

    async def test_head_on_missing_entry_is_404(self, api_client, default_tree):
        resp = await api_client.head(_url("Films/Nope.mkv"), auth=_auth())
        assert resp.status_code == 404

    async def test_head_on_directory_is_405(self, api_client, default_tree):
        resp = await api_client.head(_url(DIR_PATH), auth=_auth())
        assert resp.status_code == 405


# ─── GET — nominal ───────────────────────────────────────────────────────


class TestGetNominal:
    async def test_200_full_body_streamed_verbatim_no_gzip(self, api_client, default_tree, seed_account, xtream_mock):
        await seed_account()
        body = b"0123456789" * 200  # 2000 bytes, well above GZipMiddleware's minimum_size
        xtream_mock.get(STREAM_URL).mock(
            return_value=httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})
        )
        resp = await api_client.get(
            _url(FILE_PATH), auth=_auth(), headers={"Accept-Encoding": "gzip"},
        )
        assert resp.status_code == 200
        assert resp.content == body
        assert resp.headers.get("content-encoding") == "identity"
        assert resp.headers.get("accept-ranges") == "bytes"
        assert resp.headers.get("last-modified") is not None

    async def test_range_request_is_passed_through_as_206(self, api_client, default_tree, seed_account, xtream_mock):
        await seed_account()
        full = b"0123456789" * 200
        xtream_mock.get(STREAM_URL).mock(
            return_value=httpx.Response(
                206, content=full[0:100],
                headers={"Content-Range": f"bytes 0-99/{len(full)}", "Content-Length": "100"},
            )
        )
        resp = await api_client.get(_url(FILE_PATH), auth=_auth(), headers={"Range": "bytes=0-99"})
        assert resp.status_code == 206
        assert resp.content == full[0:100]
        assert resp.headers.get("content-range") == f"bytes 0-99/{len(full)}"
        assert resp.headers.get("content-encoding") == "identity"

    async def test_second_get_reuses_the_released_permit(self, api_client, default_tree, seed_account, xtream_mock):
        """Two sequential requests against a max_connections=1 account must
        both succeed — proves the throttle permit from the first GET was
        actually released once its body finished draining."""
        await seed_account()
        body = b"abc"
        xtream_mock.get(STREAM_URL).mock(
            return_value=httpx.Response(200, content=body, headers={"Content-Length": "3"})
        )
        first = await api_client.get(_url(FILE_PATH), auth=_auth())
        second = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert first.status_code == 200
        assert second.status_code == 200


# ─── GET — permit/upstream cleanup robustness ───────────────────────────
#
# `_get_response` (app/api/dav.py) holds BOTH the throttle permit and the
# live upstream connection from a successful `open_upstream()` call until
# `_cleanup_once()` runs — normally from the streamed `body()` generator's
# own `finally`. The two tests below exercise the two paths that DON'T go
# through that `finally` at all: (1) something raising while assembling/
# returning the `StreamingResponse` itself, and (2) the DAV client
# disconnecting (task cancellation) while `body()` is mid-iteration, which
# both instead rely on the `try/except` wrapper and the `background=`
# safety net added alongside it. `default_tree`/`seed_account` are the same
# `max_connections=1` fixtures the two `test_*_releases_permit_for_next_
# request` tests above already use to prove a released permit.


class TestGetPermitCleanupRobustness:
    async def test_streaming_response_construction_failure_releases_the_permit(
        self, api_client, default_tree, seed_account, xtream_mock, monkeypatch,
    ):
        """Simulates the exact scenario B2 describes: something between a
        successful `open_upstream()` and the `return StreamingResponse(...)`
        blows up. `dav_module.StreamingResponse` — the name `_get_response`
        actually calls — is replaced with a stand-in that raises ONCE (first
        GET) then delegates to the real `StreamingResponse` (second GET), so
        the first request fails BEFORE the router ever returns a response
        object, while the account's single upstream permit + connection were
        already acquired/opened. `ASGITransport`'s default
        `raise_app_exceptions=True` re-raises the server-side exception into
        the test client, matching Starlette's own `ServerErrorMiddleware`
        behaviour (it always sends a 500 THEN re-raises)."""
        await seed_account()
        body = b"abc"
        xtream_mock.get(STREAM_URL).mock(
            return_value=httpx.Response(200, content=body, headers={"Content-Length": "3"})
        )

        real_streaming_response = dav_module.StreamingResponse
        calls = {"n": 0}

        def _boom_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated failure assembling the response")
            return real_streaming_response(*args, **kwargs)

        monkeypatch.setattr(dav_module, "StreamingResponse", _boom_once)

        with pytest.raises(RuntimeError, match="simulated failure"):
            await api_client.get(_url(FILE_PATH), auth=_auth())
        assert calls["n"] == 1

        # Second GET, same max_connections=1 account, StreamingResponse
        # working normally now (calls["n"] becomes 2 -> real constructor):
        # if the first request's permit/upstream connection had leaked
        # (no `_cleanup_once()` ever ran), this would 503 (throttle timeout)
        # instead of 200 — same proof pattern as
        # `TestGetNominal.test_second_get_reuses_the_released_permit`.
        second = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert second.status_code == 200
        assert second.content == body
        assert calls["n"] == 2

    async def test_client_disconnect_mid_stream_releases_the_permit(
        self, api_client, default_tree, seed_account, monkeypatch,
    ):
        """Simulates a DAV client (rclone) dropping the connection PART-WAY
        through a GET — the risk the `background=BackgroundTask(...)` safety
        net (and the streamed body's own `finally`) exists for. `relay.
        open_upstream` is monkeypatched to return a fake `UpstreamStream`
        whose body yields one chunk and then blocks FOREVER (an `asyncio.
        Event` that's never set) — deterministic, not a sleep/timing race:
        the request can only ever end via cancellation, never on its own.
        Wrapping the whole client call in `asyncio.wait_for(..., timeout=)`
        cancels the task running it once the timeout elapses; since
        `ASGITransport` drives the ASGI app in that SAME task (no separate
        task per request), the cancellation reaches all the way down into
        Starlette's `StreamingResponse.__call__` — which (verified against
        the pinned starlette==0.52.1 source, see `app/api/dav.py`'s
        docstring) runs `stream_response` (draining `body()`) inside an
        `anyio` task group and cancels it the same way a real ASGI
        `http.disconnect` would — unwinding `body()`'s `finally` and
        releasing the permit + closing the fake upstream."""
        await seed_account()

        aclose_calls = {"n": 0}
        never_resumes = asyncio.Event()

        async def _slow_body():
            yield b"first-chunk-"
            await never_resumes.wait()  # pragma: no cover - never reached

        async def _fake_aclose() -> None:
            aclose_calls["n"] += 1

        real_open_upstream = relay_module.open_upstream

        async def _fake_open_upstream(url, range_header):
            return relay_module.UpstreamStream(
                200, {"Content-Length": "9999"}, _slow_body(), _fake_aclose,
            )

        monkeypatch.setattr(relay_module, "open_upstream", _fake_open_upstream)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(api_client.get(_url(FILE_PATH), auth=_auth()), timeout=1.0)

        assert aclose_calls["n"] == 1, "the fake upstream must have been closed exactly once"

        # Same account, permit-released proof: a normal second GET must not
        # 503. Re-point `relay.open_upstream` at the REAL implementation via
        # `monkeypatch.setattr` (NOT `monkeypatch.undo()` — this fixture
        # instance is shared with the autouse `_dav_enabled` fixture above,
        # which ALSO patches through it; `.undo()` would revert THOSE
        # settings too — e.g. `DAV_ENABLED` back to its off-by-default value
        # — and the second GET would then 503 with "WebDAV disabled" instead
        # of proving anything about the permit).
        monkeypatch.setattr(relay_module, "open_upstream", real_open_upstream)
        import respx

        with respx.mock(assert_all_called=False) as xtream_mock2:
            xtream_mock2.get(STREAM_URL).mock(
                return_value=httpx.Response(200, content=b"ok", headers={"Content-Length": "2"})
            )
            second = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert second.status_code == 200, "permit from the disconnected request must have been released"


# ─── GET — account resolution failures ──────────────────────────────────


class TestGetAccountResolution:
    async def test_unknown_account_is_404(self, api_client, install_tree, xtream_mock):
        install_tree(_make_tree(server_id="xtream_ghost"))
        resp = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert resp.status_code == 404

    async def test_inactive_account_is_404(self, api_client, default_tree, seed_account, xtream_mock):
        await seed_account(is_active=False)
        resp = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert resp.status_code == 404


# ─── GET — upstream failures ─────────────────────────────────────────────


class TestGetUpstreamErrors:
    async def test_upstream_500_is_502(self, api_client, default_tree, seed_account, xtream_mock):
        await seed_account()
        xtream_mock.get(STREAM_URL).mock(return_value=httpx.Response(500))
        resp = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert resp.status_code == 502

    async def test_upstream_404_is_404(self, api_client, default_tree, seed_account, xtream_mock):
        await seed_account()
        xtream_mock.get(STREAM_URL).mock(return_value=httpx.Response(404))
        resp = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert resp.status_code == 404

    async def test_upstream_timeout_is_504(self, api_client, default_tree, seed_account, monkeypatch):
        """`relay.UpstreamTimeout` must be caught BEFORE its own parent
        `UpstreamError` (`_get_response`'s except-clause ordering) and
        mapped to 504, not 502 — no router-level test exercised this branch
        (`xtream_mock` has no clean way to raise a real httpx timeout mid-
        streaming-response setup here without racing the mock router), so
        `relay.open_upstream` is monkeypatched directly to raise it."""
        await seed_account()

        async def _raise_timeout(url, range_header):
            raise relay_module.UpstreamTimeout("simulated upstream timeout")

        monkeypatch.setattr(relay_module, "open_upstream", _raise_timeout)
        resp = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert resp.status_code == 504

    async def test_upstream_500_releases_permit_for_next_request(
        self, api_client, default_tree, seed_account, xtream_mock,
    ):
        """A max_connections=1 account must not stay locked out after a
        failed upstream call — proves `release()` runs in the exception path
        too, not just the happy path."""
        await seed_account()
        xtream_mock.get(STREAM_URL).mock(return_value=httpx.Response(500))
        first = await api_client.get(_url(FILE_PATH), auth=_auth())
        second = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert first.status_code == 502
        assert second.status_code == 502  # not 503 - would mean the permit leaked

    async def test_throttle_timeout_is_503_with_retry_after(
        self, api_client, default_tree, seed_account, monkeypatch,
    ):
        await seed_account()

        async def _timeout(*args, **kwargs):
            raise ThrottleTimeout("no upstream permit available")

        monkeypatch.setattr(account_throttle, "acquire", _timeout)
        resp = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert resp.status_code == 503
        assert resp.headers.get("retry-after") == "10"


# ─── Method / resource-type guards ──────────────────────────────────────


class TestMethodGuards:
    async def test_get_on_directory_is_405(self, api_client, default_tree):
        resp = await api_client.get(_url(DIR_PATH), auth=_auth())
        assert resp.status_code == 405

    @pytest.mark.parametrize("method", ["PUT", "DELETE", "MKCOL", "PATCH"])
    async def test_write_verbs_are_405(self, api_client, default_tree, method):
        # No credentials on purpose: Starlette's route matching rejects an
        # unregistered method with 405 BEFORE any dependency (including
        # auth) runs, so this also proves that ordering.
        resp = await api_client.request(method, _url(FILE_PATH))
        assert resp.status_code == 405


# ─── Secrets hygiene ─────────────────────────────────────────────────────


class TestNoUrlLeak:
    async def test_error_body_never_contains_the_upstream_url(
        self, api_client, default_tree, seed_account, xtream_mock,
    ):
        await seed_account()
        xtream_mock.get(STREAM_URL).mock(return_value=httpx.Response(500))
        resp = await api_client.get(_url(FILE_PATH), auth=_auth())
        assert resp.status_code == 502
        body_text = resp.text
        for needle in ("provider.example", "/u/p/"):
            assert needle not in body_text

    async def test_size_mismatch_warning_never_logs_the_url(
        self, api_client, default_tree, seed_account, xtream_mock, caplog,
    ):
        await seed_account()
        # Tree says 1000 bytes, upstream disagrees -> triggers the size
        # mismatch warning (`_warn_on_size_mismatch`).
        xtream_mock.get(STREAM_URL).mock(
            return_value=httpx.Response(200, content=b"short", headers={"Content-Length": "5"})
        )
        # `app/main.py` sets `plexhub` (this router's own "plexhub.api.dav"
        # logger's parent) to `propagate=False`, so pytest's root-attached
        # caplog handler never sees its records through `caplog.at_level`
        # alone — attach the handler directly onto this router's logger for
        # the duration of the request instead (propagate=False only stops a
        # record climbing PAST "plexhub", it doesn't stop a handler attached
        # directly on/under it from firing). Scoped to this one logger
        # (mirrors tests/test_download_transfer.py's `logger="plexhub.
        # download"` convention) so httpx's own per-request INFO log line —
        # which legitimately DOES contain the upstream URL, that's httpx's
        # transport logging, not this feature's code — is never captured.
        dav_logger = logging.getLogger("plexhub.api.dav")
        dav_logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.DEBUG, logger="plexhub.api.dav"):
                resp = await api_client.get(_url(FILE_PATH), auth=_auth())
        finally:
            dav_logger.removeHandler(caplog.handler)
        assert resp.status_code == 200
        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert "provider.example" not in log_text
        assert "/u/p/" not in log_text
        # ... but the mismatch itself IS surfaced (identity, no secret).
        assert "size mismatch" in log_text
        assert "Dune (2021).mkv" in log_text
