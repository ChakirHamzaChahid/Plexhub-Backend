"""Scrape gateway cache keys and bounded settings (lot B1, ADR 0009 D2.2/D2.3).

Pure module: no DB, no network, no route. This first slice only carries the
bounded readers of the gateway settings; the canonical cache keys are added by
the next ticket.
"""
from __future__ import annotations

from app.config import settings

_DAY_MS = 86_400_000

TTL_SUCCESS_DAYS_RANGE = (1, 365)
TTL_NEGATIVE_DAYS_RANGE = (1, 30)
REQUEST_TIMEOUT_SECONDS_RANGE = (5, 28)


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    return max(low, min(high, int(value)))


def ttl_success_ms() -> int:
    """TTL of a success entry (matched/found), in ms, bounded to 1..365 days."""
    return _clamp(settings.SCRAPE_TTL_SUCCESS_DAYS, TTL_SUCCESS_DAYS_RANGE) * _DAY_MS


def ttl_negative_ms() -> int:
    """TTL of a negative entry (ambiguous/nomatch/not_found), in ms, 1..30 days."""
    return _clamp(settings.SCRAPE_TTL_NEGATIVE_DAYS, TTL_NEGATIVE_DAYS_RANGE) * _DAY_MS


def request_timeout_seconds() -> int:
    """Overall deadline of one gateway request, bounded to 5..28 seconds."""
    return _clamp(settings.SCRAPE_REQUEST_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS_RANGE)


def rate_limit_per_minute() -> int:
    """Requests per minute per API key; 0 means disabled (any value <= 0)."""
    return max(0, int(settings.SCRAPE_RATE_LIMIT_PER_MINUTE))
