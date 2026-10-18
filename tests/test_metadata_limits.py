"""B2-11 — limites de l'API `/api/metadata/v1` (ADR 0010 § D2.4, contrat § 4.6) :
corps par préfixe dans UNE instance de middleware, deux limiteurs, réglages bornés.
"""
from __future__ import annotations

import pytest

from app.api.body_limit import ScrapeBodyLimitMiddleware
from app.config import settings
from app.services import metadata_limits

METADATA_PREFIX = "/api/metadata/v1/"


async def _run(middleware, path: str, chunks: list[bytes], headers=None):
    sent: list[dict] = []
    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
             for i, c in enumerate(chunks)] or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": path, "method": "POST", "headers": headers or []}
    await middleware(scope, receive, send)
    return sent


async def _inner(scope, receive, send):
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _mw():
    return ScrapeBodyLimitMiddleware(_inner, extra_limits={METADATA_PREFIX: 262_144})


async def test_scrape_prefix_keeps_its_64_kib_cap():
    sent = await _run(_mw(), "/api/scrape/v1/cache/purge", [b"x" * 65_537])
    assert _status(sent) == 413
    sent = await _run(_mw(), "/api/scrape/v1/cache/purge", [b"x" * 65_536])
    assert _status(sent) == 200


async def test_metadata_prefix_allows_up_to_256_kib():
    sent = await _run(_mw(), METADATA_PREFIX + "items/s/r", [b"x" * 100_000])
    assert _status(sent) == 200  # would be 413 under the 64 KiB scrape cap
    sent = await _run(_mw(), METADATA_PREFIX + "items/s/r", [b"x" * 262_145])
    assert _status(sent) == 413


async def test_metadata_content_length_over_the_cap_is_refused_without_reading():
    read = {"n": 0}

    async def inner(scope, receive, send):  # pragma: no cover - must not run
        read["n"] += 1

    mw = ScrapeBodyLimitMiddleware(inner, extra_limits={METADATA_PREFIX: 262_144})
    sent = await _run(mw, METADATA_PREFIX + "items/s/r", [b""], headers=[(b"content-length", b"262145")])
    assert _status(sent) == 413 and read["n"] == 0


async def test_chunked_metadata_body_is_cut_as_it_arrives():
    sent = await _run(_mw(), METADATA_PREFIX + "items/s/r", [b"x" * 200_000, b"x" * 100_000])
    assert _status(sent) == 413


async def test_413_body_is_the_gateway_error_shape():
    import json

    sent = await _run(_mw(), METADATA_PREFIX + "items/s/r", [b"x" * 262_145])
    body = next(m["body"] for m in sent if m["type"] == "http.response.body")
    assert json.loads(body) == {"detail": {"code": "invalid_parameter", "message": "Request body too large"}}


async def test_other_paths_are_untouched():
    sent = await _run(_mw(), "/api/accounts", [b"x" * 500_000])
    assert _status(sent) == 200


def test_main_registers_a_single_body_limit_instance_with_the_metadata_prefix():
    from app.main import app

    entries = [m for m in app.user_middleware if m.cls is ScrapeBodyLimitMiddleware]
    assert len(entries) == 1
    assert entries[0].kwargs["extra_limits"] == {METADATA_PREFIX: 262_144}


# ─── réglages bornés ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [(15, 15), (1, 5), (0, 5), (-3, 5), (99, 28), (28, 28), (5, 5)])
def test_request_timeout_is_clamped_to_5_28(monkeypatch, raw, expected):
    monkeypatch.setattr(settings, "METADATA_REQUEST_TIMEOUT_SECONDS", raw)
    assert metadata_limits.request_timeout_seconds() == expected


def test_rate_limits_default_and_disable(monkeypatch):
    assert settings.METADATA_RATE_LIMIT_PER_MINUTE == 300
    assert settings.METADATA_WRITE_RATE_LIMIT_PER_MINUTE == 90
    monkeypatch.setattr(settings, "METADATA_RATE_LIMIT_PER_MINUTE", -5)
    monkeypatch.setattr(settings, "METADATA_WRITE_RATE_LIMIT_PER_MINUTE", 0)
    assert metadata_limits.rate_limit_per_minute() == 0
    assert metadata_limits.write_rate_limit_per_minute() == 0


def test_two_independent_limiters_exist():
    from app.utils import rate_limit as rl

    assert rl.metadata_rate_limiter is not rl.metadata_write_rate_limiter
    assert rl.metadata_rate_limiter is not rl.scrape_gateway_rate_limiter
