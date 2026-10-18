"""Bounded settings of the metadata API (lot B2, ADR 0010 D2.4, ADR 0011 D2.4).

Pure functions over `settings`: the config reads raw values, this module clamps
them (same split as `scrape_keys` for the scrape gateway).
"""
from __future__ import annotations

from app.config import settings

REQUEST_TIMEOUT_SECONDS_RANGE = (5, 28)  # a client cuts at 30 s


def request_timeout_seconds() -> int:
    """Deadline of one request BEFORE its COMMIT (503 `database_busy` past it)."""
    low, high = REQUEST_TIMEOUT_SECONDS_RANGE
    return max(low, min(high, int(settings.METADATA_REQUEST_TIMEOUT_SECONDS)))


def rate_limit_per_minute() -> int:
    """Requests per minute per API key, all routes; 0 means disabled."""
    return max(0, int(settings.METADATA_RATE_LIMIT_PER_MINUTE))


def write_rate_limit_per_minute() -> int:
    """Requests per minute per API key on the routes that validate a write; 0 = disabled."""
    return max(0, int(settings.METADATA_WRITE_RATE_LIMIT_PER_MINUTE))
