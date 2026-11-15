"""Generic notification service supporting Discord webhooks and structured JSON webhooks.

T8.1 (Lot 8) — Outbound notifications for operational events (provider outage,
download completion, backup failure, etc.).

Security and reliability invariants:
- Strict SSRF protection: http/https scheme required, public routable host only
  (app.utils.ssrf.assert_public_host + vet_request hook), redirects disallowed.
- Zero secret / token leaks: webhook URLs with tokens/queries are masked in logs;
  raw exceptions containing target URLs are stripped or replaced with error types.
- Anti-spam deduplication: in-memory bounded cooldown cache per (event, key).
- Fail-safe: send_notification never raises, returns boolean success.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from app.config import settings
from app.utils.ssrf import SsrfBlockedError, assert_public_host, vet_request

logger = logging.getLogger("plexhub.notify")
# Pin httpx to WARNING to prevent logging request URLs containing webhook secrets/tokens
logging.getLogger("httpx").setLevel(logging.WARNING)

_DEFAULT_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 3.0
_COOLDOWN_CACHE_MAX_ENTRIES = 1000

# Severity to Discord embed color mapping
_DISCORD_COLORS: dict[str, int] = {
    "info": 0x3498DB,      # Blue
    "success": 0x2ECC71,   # Green
    "warning": 0xF1C40F,   # Yellow / Gold
    "error": 0xE74C3C,     # Red
}

# (event, key) -> timestamp_monotonic
_cooldown_cache: dict[tuple[str, str], float] = {}
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return a pooled, keep-alive AsyncClient with SSRF guard and redirect disabled."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT, connect=_CONNECT_TIMEOUT),
            follow_redirects=False,
            event_hooks={"request": [vet_request]},
        )
    return _client


async def close_client() -> None:
    """Close the shared notification HTTP client if open (for shutdown/tests)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def mask_webhook_url(url: str | None) -> str:
    """Mask credentials, tokens, and query strings in a webhook URL for safe logging."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # 1. Mask basic auth user:password if present
        netloc = parsed.netloc
        if "@" in netloc:
            user_part, host_part = netloc.split("@", 1)
            if ":" in user_part:
                u, _ = user_part.split(":", 1)
                netloc = f"{u}:***@{host_part}"
            else:
                netloc = f"***@{host_part}"

        # 2. Mask Discord path: /api/webhooks/<id>/<token>
        path = parsed.path
        parts = path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "webhooks":
            parts[4] = "***"
            path = "/".join(parts[:5])

        # 3. Mask query params if any
        query = "***" if parsed.query else ""

        return urlunparse((parsed.scheme, netloc, path, parsed.params, query, parsed.fragment))
    except Exception:
        return "***"


def is_discord_webhook(url: str) -> bool:
    """Return True if URL points to Discord webhook endpoint."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        is_discord_host = (
            host in ("discord.com", "discordapp.com")
            or host.endswith(".discord.com")
            or host.endswith(".discordapp.com")
        )
        return is_discord_host and "/api/webhooks/" in parsed.path
    except Exception:
        return False


def is_event_enabled(event: str, events_config: str | None = None) -> bool:
    """Return True if event is allowed by the configured comma-separated events list or '*'."""
    raw = events_config if events_config is not None else settings.NOTIFY_EVENTS
    raw = (raw or "").strip()
    if raw == "*":
        return True
    allowed = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return event.strip().lower() in allowed


def clear_cooldown_cache() -> None:
    """Reset the in-memory cooldown cache (used by tests)."""
    global _cooldown_cache
    _cooldown_cache.clear()


def _is_in_cooldown(event: str, key: str | None, cooldown_seconds: int) -> bool:
    if cooldown_seconds <= 0:
        return False
    dedup_key = (event.strip().lower(), (key or "").strip())
    last_sent = _cooldown_cache.get(dedup_key)
    if last_sent is not None and (time.monotonic() - last_sent) < cooldown_seconds:
        return True
    return False


def _record_sent(event: str, key: str | None) -> None:
    global _cooldown_cache
    now = time.monotonic()
    dedup_key = (event.strip().lower(), (key or "").strip())

    # Bounded cache eviction when size exceeds limit
    if len(_cooldown_cache) >= _COOLDOWN_CACHE_MAX_ENTRIES:
        cutoff = now - max(3600.0, float(settings.NOTIFY_COOLDOWN_SECONDS * 2))
        _cooldown_cache = {k: v for k, v in _cooldown_cache.items() if v > cutoff}
        if len(_cooldown_cache) >= _COOLDOWN_CACHE_MAX_ENTRIES:
            oldest_key = min(_cooldown_cache, key=_cooldown_cache.get)
            _cooldown_cache.pop(oldest_key, None)

    _cooldown_cache[dedup_key] = now


def build_discord_payload(
    event: str,
    title: str,
    message: str,
    *,
    severity: str = "info",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Format payload according to Discord webhook Embed specification."""
    color = _DISCORD_COLORS.get(severity.lower(), _DISCORD_COLORS["info"])
    now_iso = datetime.now(timezone.utc).isoformat()
    embed: dict[str, Any] = {
        "title": f"[{event.upper()}] {title}",
        "description": message,
        "color": color,
        "timestamp": now_iso,
    }
    if details and isinstance(details, dict):
        fields = []
        for k, v in details.items():
            fields.append({
                "name": str(k)[:256],
                "value": str(v)[:1024],
                "inline": True,
            })
        if fields:
            embed["fields"] = fields[:25]
    return {"embeds": [embed]}


def build_generic_payload(
    event: str,
    title: str,
    message: str,
    *,
    severity: str = "info",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Format standard structured JSON webhook payload."""
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "event": event,
        "title": title,
        "message": message,
        "severity": severity,
        "details": details if isinstance(details, dict) else (details or {}),
        "timestamp": now_iso,
    }


async def send_notification(
    event: str,
    title: str,
    message: str,
    *,
    severity: str = "info",
    key: str | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """Send an async notification to the configured webhook (Discord or generic).

    Never raises an exception; returns True on HTTP success (2xx), False otherwise.
    Safe against SSRF, credential leakage, and notification storms.
    """
    if not settings.NOTIFY_ENABLED:
        return False

    url = (settings.NOTIFY_WEBHOOK_URL or "").strip()
    if not url:
        return False

    if not is_event_enabled(event):
        logger.debug("Notification skipped for event %r (not in NOTIFY_EVENTS)", event)
        return False

    cooldown = settings.NOTIFY_COOLDOWN_SECONDS
    if _is_in_cooldown(event, key, cooldown):
        logger.debug("Notification for (%r, %r) throttled by cooldown (%ds)", event, key, cooldown)
        return False

    # 1. URL Scheme & Host validation
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            logger.warning("Notification rejected: unsupported scheme %r", parsed.scheme)
            return False
        host = parsed.hostname
        if not host:
            logger.warning("Notification rejected: missing host in webhook URL")
            return False
    except Exception as exc:
        logger.warning("Notification rejected: invalid webhook URL (%s)", type(exc).__name__)
        return False

    # 2. SSRF check on hostname
    try:
        await assert_public_host(host)
    except SsrfBlockedError:
        logger.warning("Notification blocked by SSRF guard: unsafe host %r", host)
        return False
    except Exception as exc:
        logger.warning("Notification SSRF resolution failed for host %r: %s", host, type(exc).__name__)
        return False

    # 3. Payload preparation
    if is_discord_webhook(url):
        payload = build_discord_payload(
            event, title, message, severity=severity, details=details
        )
    else:
        payload = build_generic_payload(
            event, title, message, severity=severity, details=details
        )

    masked_url = mask_webhook_url(url)
    client = _get_client()

    # 4. Outbound HTTP dispatch
    try:
        resp = await client.post(url, json=payload)
        if 200 <= resp.status_code < 300:
            _record_sent(event, key)
            logger.info("Notification sent [%s] (%s): %s", event, severity, title)
            return True
        else:
            logger.warning(
                "Notification webhook returned HTTP %d for [%s] to %s",
                resp.status_code,
                event,
                masked_url,
            )
            return False
    except SsrfBlockedError:
        # Caught if vet_request hook triggers during connect/redirect
        logger.warning("Notification blocked by SSRF hook: %s", masked_url)
        return False
    except Exception as exc:
        # Strip raw exception message if it contains the unmasked url
        err_name = type(exc).__name__
        logger.warning("Failed to send notification [%s] to %s: %s", event, masked_url, err_name)
        return False


def notify_fire_and_forget(
    event: str,
    title: str,
    message: str,
    *,
    severity: str = "info",
    key: str | None = None,
    details: dict[str, Any] | None = None,
) -> asyncio.Task | None:
    """Helper to dispatch send_notification as a background asyncio task.

    Returns the Task if an event loop is running, or None if no loop is available.
    Exceptions inside the task are logged and do not crash the caller.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("notify_fire_and_forget called without running event loop")
        return None

    from app.utils.tasks import create_background_task

    coro = send_notification(
        event,
        title,
        message,
        severity=severity,
        key=key,
        details=details,
    )
    return create_background_task(coro, name=f"notify_{event}")
