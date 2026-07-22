"""WebDAV router — the HTTP surface rclone/Plex actually talk to (DAV-2).

Wires together the three DAV-1 building blocks (`app/dav/vfs.py`, `app/dav/
propfind.py`, `app/dav/throttle.py`, `app/dav/relay.py`) into ONE self-
prefixed, self-guarded router mounted at `/dav` (deliberately NOT under
`/api` — rclone speaks HTTP Basic Auth, not `X-API-Key`; see `app/main.py`'s
"Pattern C" mounting block).

A single dispatch function (`dav_dispatch`) handles every verb this feature
supports, registered on TWO routes — `""` (bare `/dav`) and `/{path:path}`
(everything under it) — because Starlette's `path` converter never matches
an empty remainder on its own. Both `DavTree.lookup`/`list_dir` (see `app/
dav/vfs.py`) normalize slashes internally, so the raw path segment captured
by the route is passed straight through.

Verb summary:
  OPTIONS  -> 204, advertises `DAV: 1` (rclone's WebDAV capability probe).
  PROPFIND -> 207 Multi-Status body (`app/dav/propfind.render_multistatus`).
              Depth 0 = the resource itself; Depth 1 = + its direct children
              (directories only); any other value (including the RFC default
              of "infinity", and a missing header) -> 403, this in-memory
              tree is never walked unbounded.
  HEAD     -> answered ENTIRELY from the cached tree — zero upstream call,
              on purpose: HEAD is what rclone's directory listing / dir-cache
              refresh hits constantly, and every one of those must NOT spend
              an Xtream upstream connection out of the account's tiny cap
              (`app/dav/throttle.py`).
  GET      -> resolves the file's owning `XtreamAccount` (mirrors `app/api/
              stream.py`'s account lookup), acquires one throttle permit for
              that account, opens the upstream (`app/dav/relay.open_upstream`)
              and streams its body back. The throttle permit AND the open
              upstream connection are released exactly once via `_cleanup_
              once` (`_get_response`), reachable from up to two independent
              call sites — the streamed body's own `finally` (normal
              end-of-stream / mid-stream disconnect) and a `StreamingResponse
              (background=...)` safety net (covers a failure while assembling
              the response itself, or a body that's constructed but never
              actually driven) — idempotent so whichever fires first wins.

Every response this router returns for a file also carries
`Content-Encoding: identity` — that's what stops `app/main.py`'s global
`GZipMiddleware` from touching it (Starlette's GZip responder skips any
response that already declares a `Content-Encoding`). Without it, gzip would
silently corrupt `Content-Length`/`Content-Range` for rclone.

Secrets invariant (same as `app/dav/relay.py`): the resolved Xtream stream
URL is never put in a log line, an exception message, or an HTTP response
body — only status codes and the local tree's own metadata (file name/size)
are ever surfaced to the DAV client or the logs.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_dav_basic_auth
from app.config import settings
from app.db.database import get_db
from app.dav import relay
from app.dav.propfind import content_type_for, http_date, render_multistatus
from app.dav.throttle import ThrottleTimeout, account_throttle, upstream_limit
from app.dav.vfs import DavEntry, DavTree, dav_tree_cache
from app.models.database import XtreamAccount
from app.services.stream_service import build_stream_url
from app.utils.server_id import parse_server_id

logger = logging.getLogger("plexhub.api.dav")

_BASE_HREF = "/dav"
_ALLOWED_METHODS = ["OPTIONS", "PROPFIND", "HEAD", "GET"]


router = APIRouter(prefix="/dav", tags=["dav"], dependencies=[Depends(verify_dav_basic_auth)])


# ─── PROPFIND ────────────────────────────────────────────────────────────


def _child_rel_path(parent_norm: str, child_name: str) -> str:
    return f"{parent_norm}/{child_name}" if parent_norm else child_name


def _propfind_response(request: Request, tree: DavTree, path: str, entry: DavEntry | None) -> Response:
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # RFC 4918 defaults an absent Depth to "infinity" — deliberately rejected
    # here (along with an explicit "infinity"): this tree is served from an
    # in-memory snapshot, not a filesystem, but a client asking to recurse
    # the whole catalogue in one PROPFIND is still not something this
    # minimal server (built for rclone's 0/1 usage only) answers.
    depth = request.headers.get("depth")
    if depth not in ("0", "1"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unsupported Depth (only 0 and 1)")

    norm_path = path.strip("/")
    items: list[tuple[str, DavEntry]] = [(norm_path, entry)]
    # On a FILE, Depth 0 and 1 render identically (self only) — there are no
    # children to add regardless of what Depth asked for.
    if depth == "1" and entry.is_dir:
        for child_name, child_entry in tree.list_dir(path) or []:
            items.append((_child_rel_path(norm_path, child_name), child_entry))

    body = render_multistatus(_BASE_HREF, items)
    return Response(
        content=body,
        status_code=status.HTTP_207_MULTI_STATUS,
        media_type="application/xml; charset=utf-8",
    )


# ─── HEAD ────────────────────────────────────────────────────────────────


def _head_response(entry: DavEntry) -> Response:
    """Answered purely from the cached tree — see module docstring for why
    this must never touch the upstream/throttle."""
    headers = {
        "Content-Length": str(entry.size or 0),
        "Content-Type": content_type_for(entry.name),
        "Last-Modified": http_date(entry.mtime),
        "Accept-Ranges": "bytes",
    }
    return Response(content=b"", status_code=status.HTTP_200_OK, headers=headers)


# ─── GET ─────────────────────────────────────────────────────────────────


async def _resolve_account(db: AsyncSession, entry: DavEntry) -> XtreamAccount | None:
    account_id = parse_server_id(entry.server_id) if entry.server_id else None
    if not account_id:
        return None
    result = await db.execute(
        select(XtreamAccount).where(
            XtreamAccount.id == account_id,
            XtreamAccount.is_active == True,  # noqa: E712 - SQLAlchemy filter, not a truthiness check
        )
    )
    return result.scalars().first()


def _warn_on_size_mismatch(entry: DavEntry, upstream: relay.UpstreamStream) -> None:
    """Best-effort drift detector: the tree's cached `file_size` (from the
    last `health_check_worker` HEAD) can go stale if the provider re-encoded
    the file since. Logged (item identity + both sizes) never with the
    upstream URL — reading continues regardless, this is purely informational
    (see docs/30-ops-plex-webdav.md's "risques actés")."""
    if entry.size is None or upstream.status_code != 200:
        return  # a 206/416's Content-Length is a slice, not the full size
    content_length = upstream.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) != entry.size:
        logger.warning(
            "DAV GET size mismatch for %r: tree=%d upstream=%s",
            entry.name, entry.size, content_length,
        )


async def _get_response(request: Request, db: AsyncSession, entry: DavEntry) -> Response:
    account = await _resolve_account(db, entry)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    url = build_stream_url(account, entry.rating_key or "")
    if not url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    try:
        release = await account_throttle.acquire(
            account.id, upstream_limit(account), settings.DAV_QUEUE_TIMEOUT_SECONDS,
        )
    except ThrottleTimeout:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "10"},
        ) from None

    try:
        upstream = await relay.open_upstream(url, request.headers.get("range"))
    except relay.UpstreamNotFound:
        release()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except relay.UpstreamTimeout:
        # Must be caught BEFORE UpstreamError — it's a subclass.
        release()
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT) from None
    except relay.UpstreamError:
        release()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY) from None

    # From here on `upstream` holds a live upstream connection AND the
    # throttle permit is held — both need exactly one cleanup pass no
    # matter which of the following unwinds the request:
    #   1. normal end-of-stream (body() drains upstream.body to exhaustion)
    #   2. a mid-stream client (rclone) disconnect — Starlette's
    #      `StreamingResponse.__call__` runs `stream_response` (draining
    #      body()) and `listen_for_disconnect` concurrently in one task
    #      group and cancels the former when the latter fires; that
    #      cancellation is delivered as a `CancelledError` at whatever
    #      `await` body() is suspended on, unwinding through its own
    #      `finally` below (verified against the pinned starlette==0.52.1
    #      source — this repo has no ASGI server advertising
    #      `spec_version >= "2.4"`, so `StreamingResponse` always takes
    #      this task-group branch, never the raw-OSError one).
    #   3. anything raised between here and `return StreamingResponse(...)`
    #      (header assembly, the constructor itself, ...) — body() is
    #      NEVER entered in that case, so its `finally` never runs; caught
    #      by the `try/except` around the whole handoff below.
    #   4. a response that's constructed but whose body() generator is
    #      somehow never driven a single step (0 `__anext__` calls) before
    #      the ASGI app is torn down — an async generator's `finally` only
    #      executes once its function body has actually started, so this
    #      case is invisible to body()'s own `try/finally` no matter what;
    #      covered by the `background=` safety net on `StreamingResponse`
    #      below, which Starlette always awaits once `stream_response`
    #      returns/is cancelled (paths 1 and 2), independent of whether
    #      body() itself ever ran.
    # `_cleanup_once` runs from up to two independent call sites (body()'s
    # finally AND the background task) — guarded so a permit already
    # released/upstream already closed by one is a no-op for the other
    # (mirrors `AccountThrottle.acquire`'s own idempotent `release`).
    cleaned_up = {"done": False}

    async def _cleanup_once() -> None:
        if cleaned_up["done"]:
            return
        cleaned_up["done"] = True
        await upstream.aclose()
        release()

    try:
        _warn_on_size_mismatch(entry, upstream)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.body:
                    yield chunk
            finally:
                await _cleanup_once()

        # Single source of truth for every response header: the pass-through
        # dict from the relay (Content-Length/Content-Range/Content-Type/
        # Accept-Ranges, whichever the upstream actually sent), plus the three
        # this router adds itself. `Content-Type` is always guaranteed HERE
        # (never also via `StreamingResponse(media_type=...)`) — that would
        # duplicate the header on Starlette's `Response.init_headers`; setting
        # it once in `headers` is the only path used.
        headers = dict(upstream.headers)
        headers["Content-Type"] = upstream.headers.get("Content-Type") or content_type_for(entry.name)
        headers["Content-Encoding"] = "identity"  # neutralizes app.main's global GZipMiddleware
        headers["Last-Modified"] = http_date(entry.mtime)

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            headers=headers,
            background=BackgroundTask(_cleanup_once),
        )
    except Exception:
        # Anything above (including `StreamingResponse.__init__` itself)
        # failing before the response object is even handed back to
        # Starlette — release what we're still holding, then let the
        # original exception surface as-is (never swallowed/rewrapped).
        await _cleanup_once()
        raise


# ─── Dispatch ────────────────────────────────────────────────────────────


async def dav_dispatch(
    request: Request,
    path: str = "",
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Single entry point for OPTIONS/PROPFIND/HEAD/GET on `/dav` and every
    path under it (see module docstring). Every other HTTP verb (PUT/DELETE/
    MKCOL/...) never reaches this function at all — they aren't in this
    router's registered `methods=`, so Starlette's own routing answers 405
    before any dependency (including auth) runs.
    """
    if not settings.DAV_ENABLED:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="WebDAV disabled")

    method = request.method

    if method == "OPTIONS":
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"Allow": ", ".join(_ALLOWED_METHODS), "DAV": "1"},
        )

    tree = await dav_tree_cache.get()
    entry = tree.lookup(path)

    if method == "PROPFIND":
        return _propfind_response(request, tree, path, entry)

    # HEAD / GET share the same "resolve the file entry" preamble.
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if entry.is_dir:
        # Neither HEAD nor GET is meaningful on a directory in this
        # read-only, file-serving-only WebDAV surface.
        raise HTTPException(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)

    if method == "HEAD":
        return _head_response(entry)

    return await _get_response(request, db, entry)


router.add_api_route("/{path:path}", dav_dispatch, methods=_ALLOWED_METHODS, include_in_schema=False)
router.add_api_route("", dav_dispatch, methods=_ALLOWED_METHODS, include_in_schema=False)
