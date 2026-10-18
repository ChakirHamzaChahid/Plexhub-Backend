"""Request body size limit of the scrape gateway (lot B1, security review M2).

The routes of `/api/scrape/v1` take at most a small JSON body (`/cache/invalidate`
carries up to 500 keys of 1 024 characters at the very most, `/cache/purge` a
single integer), yet Starlette reads a body of any size into memory before the
route runs. This middleware caps it at 64 KiB, for `/api/scrape/v1/` ONLY:

* a `Content-Length` above the cap is refused with 413 WITHOUT reading the
  body;
* a body without `Content-Length` (`Transfer-Encoding: chunked`) is counted as
  it arrives and cut as soon as it crosses the cap.

It is a pure ASGI middleware, not a `BaseHTTPMiddleware`: the latter buffers
the request through a task group and cannot cut a stream cleanly.

Why the `send` interception: when `receive` raises, FastAPI turns the failure
of its body parsing into a 422 (`There was an error parsing the body`). Once
the cap is crossed the client must see 413 whatever the inner app answers, so
the first `http.response.start` after the cut is replaced by the 413.

Mounted in `app/main.py` without changing the relative order of the other
middlewares; nothing outside `/api/scrape/v1/` is touched.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("plexhub.scrape_v1")

GUARDED_PREFIX = "/api/scrape/v1/"
MAX_BODY_BYTES = 65_536

_BODY = json.dumps(
    {"detail": {"code": "invalid_parameter", "message": "Request body too large"}},
    separators=(",", ":"),
).encode()
_HEADERS = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_BODY)).encode()),
]


class _BodyTooLarge(Exception):
    """Raised by the wrapped `receive` once the cap is crossed."""


class ScrapeBodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES, prefix: str = GUARDED_PREFIX) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.prefix = prefix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            logger.info("scrape gateway: body refused (Content-Length over the limit)")
            await _send_413(send)
            return

        state = {"received": 0, "exceeded": False, "started": False, "swallow": False}

        async def limited_receive():
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body", b""))
                if state["received"] > self.max_bytes:
                    state["exceeded"] = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message) -> None:
            if state["swallow"]:
                return
            if state["exceeded"] and not state["started"]:
                # Whatever the inner app answers after the cut, the client sees 413.
                state["swallow"] = True
                state["started"] = True
                await _send_413(send)
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        except Exception:
            if not (state["exceeded"] and not state["started"]):
                raise
        if state["exceeded"] and not state["started"]:
            logger.info("scrape gateway: body refused (chunked body over the limit)")
            state["started"] = True
            await _send_413(send)


def _content_length(scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None  # malformed: the server layer rejects it, not us
    return None


async def _send_413(send) -> None:
    await send({"type": "http.response.start", "status": 413, "headers": list(_HEADERS)})
    await send({"type": "http.response.body", "body": _BODY})
