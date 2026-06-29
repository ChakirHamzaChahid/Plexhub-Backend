"""FastAPI dependencies for protected endpoints.

verify_api_key:
  503 'AI service not configured' when settings.AI_API_KEY is empty.
  503 'AI vector storage unavailable' when sqlite-vec failed to load.
  401 'Invalid API key' when X-API-Key header is missing or wrong.

Comparison uses a constant-time digest check to avoid timing oracles — see
critical correction C2 from the orchestrator. Plain `==` on the configured
secret is forbidden and gated by a grep-based acceptance test.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.db.database import _VEC_LOADED


async def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Validate the X-API-Key header against settings.AI_API_KEY.

    Order matters:
      1. AI configuration absent -> 503 (service-wide unavailable).
      2. Vector extension missing -> 503 (storage-wide unavailable).
      3. Header missing or mismatched -> 401.
    """
    if not settings.AI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service not configured",
        )
    if not _VEC_LOADED.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI vector storage unavailable",
        )
    if x_api_key is None or not secrets.compare_digest(
        x_api_key.encode("utf-8"),
        settings.AI_API_KEY.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


async def verify_backend_secret(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Generic backend-secret guard for the JSON API (no sqlite-vec check).

    Same shared secret (settings.AI_API_KEY) as verify_api_key, but without the
    AI-specific vector-storage dependency — protecting accounts/media/stream/etc.
    must not 503 just because the AI vector extension failed to load. Fail-closed.
    """
    if not settings.AI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend secret not configured",
        )
    if x_api_key is None or not secrets.compare_digest(
        x_api_key.encode("utf-8"),
        settings.AI_API_KEY.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
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
