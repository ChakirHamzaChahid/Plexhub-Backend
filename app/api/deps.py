"""FastAPI dependencies for protected endpoints.

Authentication accepts EITHER:
  * the permanent master secret ``settings.AI_API_KEY`` (constant-time compare), or
  * any active (non-revoked, non-expired) per-user key from the ``api_keys`` table
    (see ``app.services.api_key_service``).

Dependencies:
  verify_backend_secret — guards the JSON API (accounts/media/stream/…). 401 on a
    missing/invalid key. No sqlite-vec dependency.
  verify_api_key — same auth, plus the AI-specific sqlite-vec check (503 when the
    vector extension failed to load). Used by the AI router.
  verify_master_key — accepts ONLY the master secret; guards key-management
    endpoints so a per-user key cannot mint or revoke other keys.
  verify_admin_basic_auth — HTTP Basic Auth for the browser /admin UI.
  verify_dav_basic_auth — HTTP Basic Auth for the read-only /dav WebDAV
    endpoint (rclone can only send Basic Auth, not a custom header).

Constant-time comparison on the master secret avoids timing oracles — plain `==`
is forbidden and gated by a grep-based acceptance test.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.db.database import _VEC_LOADED
from app.services import api_key_service


def _client_ip(request: Request | None) -> str | None:
    """Best-effort real client IP (behind the Cloudflare tunnel)."""
    if request is None:
        return None
    xff = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _is_master(x_api_key: str | None) -> bool:
    master = settings.AI_API_KEY
    if not master or not x_api_key:
        return False
    return secrets.compare_digest(x_api_key.encode("utf-8"), master.encode("utf-8"))


async def _authenticate(x_api_key: str | None, request: Request | None) -> bool:
    """True when x_api_key is the master secret OR an active per-user key."""
    if not x_api_key:
        return False
    if _is_master(x_api_key):
        return True
    row = await api_key_service.resolve(x_api_key, client_ip=_client_ip(request))
    return row is not None


async def verify_backend_secret(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Guard the JSON API. Master secret or any active per-user key. Fail-closed."""
    if not await _authenticate(x_api_key, request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


async def verify_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Guard the AI router: same auth as the JSON API + sqlite-vec availability."""
    if not _VEC_LOADED.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI vector storage unavailable",
        )
    if not await _authenticate(x_api_key, request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


async def verify_master_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Guard key-management endpoints — ONLY the master secret is accepted, so a
    per-user key cannot create or revoke other keys."""
    if not settings.AI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend secret not configured",
        )
    if not _is_master(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master key required",
        )


_admin_basic = HTTPBasic(auto_error=False)


async def verify_admin_basic_auth(
    credentials: HTTPBasicCredentials | None = Depends(_admin_basic),
) -> None:
    """HTTP Basic Auth guard for the /admin browser UI (fail-closed).

    Browser-friendly: a navigation can't carry an X-API-Key header, so the admin
    UI uses Basic Auth (ADMIN_USERNAME / ADMIN_PASSWORD) instead of the backend
    secret. Both fields are compared constant-time. 401 with WWW-Authenticate so
    the browser shows the native login prompt.
    """
    if not settings.ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin UI not configured",
        )
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid admin credentials",
        headers={"WWW-Authenticate": "Basic"},
    )
    if credentials is None:
        raise unauthorized
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.ADMIN_USERNAME.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.ADMIN_PASSWORD.encode("utf-8"),
    )
    # Evaluate both before branching to avoid a username-timing oracle.
    if not (user_ok and pass_ok):
        raise unauthorized


_dav_basic = HTTPBasic(auto_error=False)


async def verify_dav_basic_auth(
    credentials: HTTPBasicCredentials | None = Depends(_dav_basic),
) -> None:
    """HTTP Basic Auth guard for the read-only /dav WebDAV endpoint (fail-closed).

    Mirrors verify_admin_basic_auth: rclone (the only WebDAV client this
    endpoint targets) speaks HTTP Basic Auth, not a custom X-API-Key header.
    DAV_USERNAME / DAV_PASSWORD are a secret dedicated to this endpoint —
    separate from the backend's X-API-Key and from ADMIN_PASSWORD. Both
    fields are compared constant-time. 401 with WWW-Authenticate so a
    Basic-Auth-capable client can retry with credentials.
    """
    if not settings.DAV_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DAV not configured",
        )
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid DAV credentials",
        headers={"WWW-Authenticate": "Basic"},
    )
    if credentials is None:
        raise unauthorized
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.DAV_USERNAME.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.DAV_PASSWORD.encode("utf-8"),
    )
    # Evaluate both before branching to avoid a username-timing oracle.
    if not (user_ok and pass_ok):
        raise unauthorized
