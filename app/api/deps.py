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

from fastapi import Header, HTTPException, status

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
