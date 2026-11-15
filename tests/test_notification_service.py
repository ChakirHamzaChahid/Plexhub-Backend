"""Tests for notification_service (T8.1 - Lot 8).

Verifies:
- Config switches (NOTIFY_ENABLED, empty URL) -> no HTTP calls made.
- Event filtering (NOTIFY_EVENTS comma-separated and '*' wildcard).
- Payload formats (Discord embeds vs generic structured JSON).
- SSRF blocking (loopback, cloud metadata, invalid schemes, private hosts).
- Anti-spam deduplication and cooldown mechanism.
- Resilience: HTTP errors, timeouts, connection failures return False without raising,
  and tokens/secrets NEVER leak into caplog.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from app.config import settings
from app.services import notification_service
from app.utils import ssrf


@pytest.fixture(autouse=True)
def _propagate_plexhub_logs(monkeypatch):
    """app.main sets plexhub.propagate = False; enable propagation for caplog."""
    monkeypatch.setattr(logging.getLogger("plexhub"), "propagate", True)
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.WARNING)


@pytest.fixture(autouse=True)
def _clean_notification_state():
    """Ensure clean cooldown cache, SSRF cache, and closed HTTP client between tests."""
    notification_service.clear_cooldown_cache()
    ssrf.clear_cache()
    yield
    notification_service.clear_cooldown_cache()
    ssrf.clear_cache()


@pytest.fixture(autouse=True)
async def _close_client_after():
    yield
    await notification_service.close_client()


@pytest.fixture
def mock_public_dns(monkeypatch):
    """Mock SSRF DNS resolution so that test public domains resolve safely without network."""
    def _fake_resolve(host: str) -> bool:
        private_hosts = {"127.0.0.1", "169.254.169.254", "localhost", "10.0.0.1", "192.168.1.1"}
        return host not in private_hosts

    monkeypatch.setattr(ssrf, "_resolve_is_public", _fake_resolve)


# ─── 1. Disabled & Configuration Guard Tests ─────────────────────────────────


async def test_notify_disabled_does_not_call_http(respx_mock, monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", False)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "https://hooks.example.com/test")
    route = respx_mock.post("https://hooks.example.com/test").respond(200)

    success = await notification_service.send_notification("outage", "Panne", "Serveur down")
    assert success is False
    assert route.call_count == 0


async def test_notify_empty_url_does_not_call_http(respx_mock, monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "")

    success = await notification_service.send_notification("outage", "Panne", "Serveur down")
    assert success is False
    assert len(respx_mock.calls) == 0


# ─── 2. Event Filtering Tests ────────────────────────────────────────────────


async def test_notify_event_filtering_blocks_unlisted_event(respx_mock, monkeypatch, mock_public_dns):
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "https://hooks.example.com/test")
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "outage,backup")
    route = respx_mock.post("https://hooks.example.com/test").respond(200)

    # 'download' is not in 'outage,backup'
    success = await notification_service.send_notification("download", "Download done", "Movie downloaded")
    assert success is False
    assert route.call_count == 0

    # 'outage' is enabled
    success_outage = await notification_service.send_notification("outage", "Server down", "Outage strike")
    assert success_outage is True
    assert route.call_count == 1


async def test_notify_event_wildcard_allows_all(respx_mock, monkeypatch, mock_public_dns):
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "https://hooks.example.com/test")
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")
    route = respx_mock.post("https://hooks.example.com/test").respond(200)

    success = await notification_service.send_notification("any_custom_event", "Custom", "Msg")
    assert success is True
    assert route.call_count == 1


# ─── 3. SSRF Protection Tests ────────────────────────────────────────────────


async def test_notify_blocks_loopback_ssrf(respx_mock, monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "http://127.0.0.1:8080/webhook")
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")

    success = await notification_service.send_notification("outage", "Alert", "Loopback target")
    assert success is False
    assert len(respx_mock.calls) == 0


async def test_notify_blocks_cloud_metadata_ssrf(respx_mock, monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "http://169.254.169.254/latest/meta-data")
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")

    success = await notification_service.send_notification("outage", "Alert", "Metadata target")
    assert success is False
    assert len(respx_mock.calls) == 0


async def test_notify_blocks_unsupported_scheme(respx_mock, monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "file:///etc/passwd")
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")

    success = await notification_service.send_notification("outage", "Alert", "File scheme")
    assert success is False
    assert len(respx_mock.calls) == 0


# ─── 4. Payload Formats: Generic Webhook vs Discord ──────────────────────────


async def test_generic_webhook_payload_format(respx_mock, monkeypatch, mock_public_dns):
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "https://hooks.example.com/generic")
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")

    captured_payload = {}

    def _handler(request):
        import json
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    respx_mock.post("https://hooks.example.com/generic").mock(side_effect=_handler)

    details = {"job_id": 42, "account": "provider_1"}
    success = await notification_service.send_notification(
        "download",
        "Batch terminé",
        "10 médias téléchargés avec succès",
        severity="success",
        details=details,
    )
    assert success is True
    assert captured_payload["event"] == "download"
    assert captured_payload["title"] == "Batch terminé"
    assert captured_payload["message"] == "10 médias téléchargés avec succès"
    assert captured_payload["severity"] == "success"
    assert captured_payload["details"] == details
    assert "timestamp" in captured_payload


async def test_discord_webhook_payload_format(respx_mock, monkeypatch, mock_public_dns):
    webhook_url = "https://discord.com/api/webhooks/123456789/my_secret_token"
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", webhook_url)
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")

    captured_payload = {}

    def _handler(request):
        import json
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(204)

    respx_mock.post(webhook_url).mock(side_effect=_handler)

    details = {"Provider": "Xtream Server 1", "Strikes": 3}
    success = await notification_service.send_notification(
        "outage",
        "Panne détectée",
        "Le fournisseur ne répond plus",
        severity="error",
        details=details,
    )
    assert success is True
    assert "embeds" in captured_payload
    embed = captured_payload["embeds"][0]
    assert embed["title"] == "[OUTAGE] Panne détectée"
    assert embed["description"] == "Le fournisseur ne répond plus"
    # Error severity color is 0xE74C3C (15158332)
    assert embed["color"] == 0xE74C3C
    assert "timestamp" in embed
    fields = embed["fields"]
    assert len(fields) == 2
    assert fields[0] == {"name": "Provider", "value": "Xtream Server 1", "inline": True}
    assert fields[1] == {"name": "Strikes", "value": "3", "inline": True}


# ─── 5. Anti-spam Deduplication / Cooldown Tests ──────────────────────────────


async def test_deduplication_cooldown_throttles_duplicate(respx_mock, monkeypatch, mock_public_dns):
    webhook_url = "https://hooks.example.com/alerts"
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", webhook_url)
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")
    monkeypatch.setattr(settings, "NOTIFY_COOLDOWN_SECONDS", 300)

    route = respx_mock.post(webhook_url).respond(200)

    # First send with key="server_1" succeeds
    s1 = await notification_service.send_notification("outage", "Down", "Server 1 down", key="server_1")
    assert s1 is True
    assert route.call_count == 1

    # Second send with same (event, key) within cooldown is throttled
    s2 = await notification_service.send_notification("outage", "Down again", "Still down", key="server_1")
    assert s2 is False
    assert route.call_count == 1

    # Send with a different key succeeds
    s3 = await notification_service.send_notification("outage", "Down", "Server 2 down", key="server_2")
    assert s3 is True
    assert route.call_count == 2


async def test_deduplication_cooldown_disabled_when_zero(respx_mock, monkeypatch, mock_public_dns):
    webhook_url = "https://hooks.example.com/alerts"
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", webhook_url)
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")
    monkeypatch.setattr(settings, "NOTIFY_COOLDOWN_SECONDS", 0)

    route = respx_mock.post(webhook_url).respond(200)

    s1 = await notification_service.send_notification("outage", "Down", "Msg", key="same_key")
    s2 = await notification_service.send_notification("outage", "Down", "Msg", key="same_key")
    assert s1 is True
    assert s2 is True
    assert route.call_count == 2


# ─── 6. Resilience & Secret Redaction Tests ───────────────────────────────────


async def test_http_error_returns_false_and_does_not_leak_secret(
    respx_mock, monkeypatch, caplog, mock_public_dns
):
    token = "super_secret_discord_token_xyz123"
    webhook_url = f"https://discord.com/api/webhooks/987654321/{token}"
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", webhook_url)
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")

    respx_mock.post(webhook_url).respond(500, text="Internal Discord Error")

    with caplog.at_level(logging.DEBUG, logger="plexhub.notify"):
        success = await notification_service.send_notification("outage", "Down", "Msg")

    assert success is False
    # Verify the secret token is NEVER present in logs
    assert token not in caplog.text
    # Verify masked path appears instead
    assert "https://discord.com/api/webhooks/987654321/***" in caplog.text


async def test_network_timeout_returns_false_and_does_not_leak_secret(
    respx_mock, monkeypatch, caplog, mock_public_dns
):
    token = "secret_query_param_token_999"
    webhook_url = f"https://hooks.example.com/webhook?token={token}"
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", webhook_url)
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")

    respx_mock.post(webhook_url).mock(side_effect=httpx.ConnectTimeout("Connection timed out"))

    with caplog.at_level(logging.DEBUG, logger="plexhub.notify"):
        success = await notification_service.send_notification("outage", "Down", "Msg")

    assert success is False
    # Secret query param must not appear in logs
    assert token not in caplog.text
    assert "https://hooks.example.com/webhook?***" in caplog.text


# ─── 7. URL Masking Unit Tests ───────────────────────────────────────────────


def test_mask_webhook_url_discord():
    raw = "https://discord.com/api/webhooks/123456789/abcdef_token"
    masked = notification_service.mask_webhook_url(raw)
    assert masked == "https://discord.com/api/webhooks/123456789/***"
    assert "abcdef_token" not in masked


def test_mask_webhook_url_query_params():
    raw = "https://webhook.site/abc-def?api_key=secret_123&user=admin"
    masked = notification_service.mask_webhook_url(raw)
    assert masked == "https://webhook.site/abc-def?***"
    assert "secret_123" not in masked


def test_mask_webhook_url_basic_auth():
    raw = "https://user:password123@example.com/notify"
    masked = notification_service.mask_webhook_url(raw)
    assert masked == "https://user:***@example.com/notify"
    assert "password123" not in masked


# ─── 8. Fire-and-Forget Helper Tests ──────────────────────────────────────────


async def test_notify_fire_and_forget(respx_mock, monkeypatch, mock_public_dns):
    webhook_url = "https://hooks.example.com/fast"
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", webhook_url)
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")

    route = respx_mock.post(webhook_url).respond(200)

    task = notification_service.notify_fire_and_forget("download", "Done", "Media finished")
    assert isinstance(task, asyncio.Task)

    # Await the task to ensure completion in the test
    result = await task
    assert result is True
    assert route.call_count == 1
