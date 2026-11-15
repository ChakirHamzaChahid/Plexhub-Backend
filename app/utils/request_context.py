"""Request-scoped context (request_id) for log correlation."""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Default "-" so logs emitted outside an HTTP request (workers, startup) stay readable.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_HEADER_BYTES = b"x-request-id"


class RequestIdMiddleware:
    """Pure ASGI middleware assigning a request_id and exposing it via contextvar."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid: str | None = None
        for name, value in scope.get("headers", ()):
            if name.lower() == _REQUEST_ID_HEADER_BYTES:
                rid = value.decode("latin-1") or None
                break
        rid = rid or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)


class RequestIdLogFilter(logging.Filter):
    """Injects request_id into every LogRecord so the formatter can render it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
