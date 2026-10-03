"""Public pairing-approval page GET /api/tv-auth/approve (Lot 10 sous-lot B, FR-BE-075).

The TV displays `verificationUri` (= `<base>api/tv-auth/approve`) and encodes a QR
`<verificationUri>?userCode=ABCD-EFGH`; a phone opening it used to get a 405
(POST-only route). The GET serves a self-contained HTML page that POSTs back to the
same path with `fetch`. Covers: public access, security headers (strict CSP with a
per-request nonce), reflected-XSS safety of the `userCode` prefill, the dedicated
rate limiter, the POST staying guarded, the full pairing cycle, independence from the
encryption config, and the (METHOD, path) route audit.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import Base

pytestmark = pytest.mark.asyncio

API_KEY = "secret-test-key"
AUTH = {"X-API-Key": API_KEY}
PAYLOAD = {"plexToken": "xyz-PLEX-TOKEN-123"}
PAGE = "/api/tv-auth/approve"
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "app" / "templates" / "pairing" / "approve.html"
CODE_FIELD_EMPTY = re.compile(r'placeholder="ABCD-EFGH"\s+value=""')
CODE_FIELD_FILLED = re.compile(r'placeholder="ABCD-EFGH"\s+value="ABCD-EFGH"')


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures (modelled on tests/test_tv_auth.py)
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def tv_engine(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'tv_auth_page_test.db'}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def tv_factory(tv_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(tv_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def tv_client(tv_factory, monkeypatch, tmp_path) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")

    from app.db import database as db_module
    from app.main import app

    monkeypatch.setattr(db_module, "async_session_factory", tv_factory)

    async def _override_get_db():
        async with tv_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[db_module.get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(db_module.get_db, None)


async def _start(client: AsyncClient) -> dict:
    resp = await client.post("/api/tv-auth/start", json={"deviceName": "Mi Box S test"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _nonce_of(csp: str) -> str:
    m = re.search(r"script-src 'nonce-([^']+)'", csp)
    assert m, csp
    return m.group(1)


# ──────────────────────────────────────────────────────────────────────────────
# Public access, content type, security headers
# ──────────────────────────────────────────────────────────────────────────────

async def test_get_without_key_is_public_html(tv_client):
    resp = await tv_client.get(PAGE)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert "<form" in resp.text


async def test_security_headers_present(tv_client):
    resp = await tv_client.get(PAGE)
    csp = resp.headers["content-security-policy"]
    nonce = _nonce_of(csp)
    assert csp == (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_csp_nonce_changes_per_request_and_matches_inline_blocks(tv_client):
    a = await tv_client.get(PAGE)
    b = await tv_client.get(PAGE)
    na = _nonce_of(a.headers["content-security-policy"])
    nb = _nonce_of(b.headers["content-security-policy"])
    assert na != nb and len(na) >= 16
    assert f'<script nonce="{na}">' in a.text and f'<style nonce="{na}">' in a.text
    assert nb not in a.text


async def test_every_inline_block_carries_the_nonce_and_no_inline_handlers(tv_client):
    html = (await tv_client.get(PAGE)).text
    assert all('nonce="' in tag for tag in re.findall(r"<script\b[^>]*>", html))
    assert all('nonce="' in tag for tag in re.findall(r"<style\b[^>]*>", html))
    assert not re.search(r"<script\b[^>]*\bsrc=", html)  # no external script
    assert not re.search(r"\sstyle\s*=", html)  # style attributes are blocked by the CSP
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)  # onclick=... etc.


async def test_no_external_url_in_rendered_html(tv_client):
    html = (await tv_client.get(f"{PAGE}?userCode=ABCD-EFGH")).text
    assert re.findall(r"https?://", html) == []
    assert "<link" not in html


async def test_template_never_disables_escaping():
    src = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\|\s*safe", src)
    assert "Markup" not in src
    assert "{% autoescape" not in src


async def test_page_secret_handling(tv_client):
    html = (await tv_client.get(PAGE)).text
    assert re.search(r'id="authkey"\s+type="password"\s+autocomplete="off"', html)
    # Relative same-origin fetch, key only in a header, nothing persisted.
    assert 'fetch("approve"' in html and '"X-API-Key": key' in html
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "location.search"):
        assert forbidden not in html
    assert "window.location.origin" in html  # backendUrl prefill


# ──────────────────────────────────────────────────────────────────────────────
# userCode prefill: validated, never reflected raw
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "evil",
    [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "'/><svg onload=alert(1)>",
        'ABCD-EFGH"><script>alert(1)</script>',
    ],
)
async def test_malicious_user_code_is_never_reflected(tv_client, evil):
    resp = await tv_client.get(PAGE, params={"userCode": evil})
    assert resp.status_code == 200
    html = resp.text
    assert "alert(1)" not in html
    assert "onerror" not in html and "onload" not in html
    assert "&lt;script&gt;" not in html  # not even escaped: simply dropped
    assert CODE_FIELD_EMPTY.search(html)


async def test_valid_user_code_is_prefilled(tv_client):
    html = (await tv_client.get(PAGE, params={"userCode": "ABCD-EFGH"})).text
    assert CODE_FIELD_FILLED.search(html)


@pytest.mark.parametrize("given", ["abcdefgh", "abcd-efgh", " ABCDEFGH "])
async def test_valid_variants_are_normalised_for_display(tv_client, given):
    html = (await tv_client.get(PAGE, params={"userCode": given})).text
    assert CODE_FIELD_FILLED.search(html)


@pytest.mark.parametrize("bad", ["ABCD-EFG0", "ABCD", "ABCD-EFGHJ", "ABCD EFGH", "ABCD--EFGH", ""])
async def test_invalid_user_code_is_ignored(tv_client, bad):
    html = (await tv_client.get(PAGE, params={"userCode": bad})).text
    assert CODE_FIELD_EMPTY.search(html)


# ──────────────────────────────────────────────────────────────────────────────
# Rate limiter
# ──────────────────────────────────────────────────────────────────────────────

async def test_rate_limit_returns_429_with_retry_after(tv_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_APPROVE_PAGE_RATE_LIMIT_MAX", 3)
    monkeypatch.setattr(settings, "TV_AUTH_APPROVE_PAGE_RATE_LIMIT_WINDOW_SECONDS", 60)
    for _ in range(3):
        assert (await tv_client.get(PAGE)).status_code == 200
    resp = await tv_client.get(PAGE)
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) >= 1
    assert "content-security-policy" not in resp.headers  # error body, not the page


async def test_rate_limit_is_per_client_ip(tv_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_APPROVE_PAGE_RATE_LIMIT_MAX", 1)
    one = {"CF-Connecting-IP": "203.0.113.1"}
    two = {"CF-Connecting-IP": "203.0.113.2"}
    assert (await tv_client.get(PAGE, headers=one)).status_code == 200
    assert (await tv_client.get(PAGE, headers=one)).status_code == 429
    assert (await tv_client.get(PAGE, headers=two)).status_code == 200


async def test_rate_limit_zero_disables_the_limiter(tv_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_APPROVE_PAGE_RATE_LIMIT_MAX", 0)
    for _ in range(5):
        assert (await tv_client.get(PAGE)).status_code == 200


async def test_page_limiter_is_independent_of_start_limiter(tv_client, monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_APPROVE_PAGE_RATE_LIMIT_MAX", 1)
    assert (await tv_client.get(PAGE)).status_code == 200
    assert (await tv_client.get(PAGE)).status_code == 429
    assert (await tv_client.post("/api/tv-auth/start", json={})).status_code == 201


async def test_new_settings_have_defaults_and_are_documented_in_env_example():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "TV_AUTH_APPROVE_PAGE_RATE_LIMIT_MAX" in env_example
    assert "TV_AUTH_APPROVE_PAGE_RATE_LIMIT_WINDOW_SECONDS" in env_example
    assert settings.TV_AUTH_APPROVE_PAGE_RATE_LIMIT_MAX == 30
    assert settings.TV_AUTH_APPROVE_PAGE_RATE_LIMIT_WINDOW_SECONDS == 60


# ──────────────────────────────────────────────────────────────────────────────
# POST stays guarded; full cycle; independence from the crypto config
# ──────────────────────────────────────────────────────────────────────────────

async def test_post_approve_without_key_is_still_401(tv_client):
    resp = await tv_client.post(PAGE, json={"userCode": "ABCD-EFGH", "payload": PAYLOAD})
    assert resp.status_code == 401


async def test_post_approve_with_wrong_key_is_401(tv_client):
    resp = await tv_client.post(
        PAGE, json={"userCode": "ABCD-EFGH", "payload": PAYLOAD}, headers={"X-API-Key": "wrong"}
    )
    assert resp.status_code == 401


async def test_get_page_does_not_unlock_the_post(tv_client):
    """Loading the public page first changes nothing for the POST's guard."""
    assert (await tv_client.get(PAGE)).status_code == 200
    resp = await tv_client.post(PAGE, json={"userCode": "ABCD-EFGH", "payload": PAYLOAD})
    assert resp.status_code == 401


async def test_post_approve_with_valid_key_still_works(tv_client):
    started = await _start(tv_client)
    resp = await tv_client.post(
        PAGE, json={"userCode": started["userCode"], "payload": PAYLOAD}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "approved"}


async def test_full_cycle_start_page_approve_status(tv_client):
    started = await _start(tv_client)

    # QR target: verificationUri + ?userCode=...
    assert started["verificationUri"].endswith("/api/tv-auth/approve")
    page = await tv_client.get(PAGE, params={"userCode": started["userCode"]})
    assert page.status_code == 200
    assert f'value="{started["userCode"]}"' in page.text

    # What the page's fetch sends (relative POST on the same path, key in a header).
    resp = await tv_client.post(
        PAGE,
        json={"userCode": started["userCode"], "payload": PAYLOAD},
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text

    params = {"deviceCode": started["deviceCode"]}
    first = await tv_client.get("/api/tv-auth/status", params=params)
    assert first.status_code == 200
    assert first.json()["status"] == "approved"
    assert first.json()["payload"] == PAYLOAD
    second = await tv_client.get("/api/tv-auth/status", params=params)
    assert second.json()["payload"] is None  # delivered exactly once


@pytest.mark.parametrize("status_code,detail", [(404, "unknown"), (409, "already"), (410, "expired")])
async def test_post_error_codes_the_page_maps(tv_client, tv_factory, status_code, detail):
    """404 / 409 / 410 are the business codes the page turns into messages."""
    started = await _start(tv_client)
    body = {"userCode": started["userCode"], "payload": PAYLOAD}
    if status_code == 404:
        body["userCode"] = "ZZZZ-ZZZZ"
    elif status_code == 409:
        assert (await tv_client.post(PAGE, json=body, headers=AUTH)).status_code == 200
    else:
        async with tv_factory() as s:
            await s.execute(
                text("UPDATE tv_auth_sessions SET expires_at = 1000 WHERE device_code = :dc"),
                {"dc": started["deviceCode"]},
            )
            await s.commit()
    resp = await tv_client.post(PAGE, json=body, headers=AUTH)
    assert resp.status_code == status_code
    assert detail in resp.json()["detail"].lower()


async def test_post_empty_payload_is_422(tv_client):
    started = await _start(tv_client)
    resp = await tv_client.post(
        PAGE, json={"userCode": started["userCode"], "payload": {}}, headers=AUTH
    )
    assert resp.status_code == 422


async def test_get_page_is_served_when_encryption_is_not_configured(tv_client, monkeypatch):
    """The page depends on neither the Fernet key nor the DB."""
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")
    resp = await tv_client.get(PAGE)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


async def test_page_is_not_in_the_openapi_schema(tv_client):
    from app.main import app

    schema_paths = app.openapi().get("paths", {})
    assert "get" not in schema_paths.get(PAGE, {})
    assert "post" in schema_paths.get(PAGE, {})  # the POST stays documented


# ──────────────────────────────────────────────────────────────────────────────
# Route audit: (METHOD, path), POST never public
# ──────────────────────────────────────────────────────────────────────────────

async def test_real_app_boots_and_audit_accepts_get_page_only():
    from fastapi.routing import APIRoute

    from app.api.route_audit import (
        _AUTH_DEPENDENCY_NAMES,
        PUBLIC_API_ALLOWLIST,
        _dependency_names,
        assert_api_routes_authenticated,
    )
    from app.main import app  # importing app.main already ran the boot audit once

    assert_api_routes_authenticated(app)
    by_method = {
        m: r
        for r in app.routes
        if isinstance(r, APIRoute) and r.path == PAGE
        for m in (r.methods or ())
    }
    assert set(by_method) == {"GET", "POST"}
    assert ("GET", PAGE) in PUBLIC_API_ALLOWLIST and ("POST", PAGE) not in PUBLIC_API_ALLOWLIST
    assert not (_dependency_names(by_method["GET"]) & _AUTH_DEPENDENCY_NAMES)
    assert _dependency_names(by_method["POST"]) & _AUTH_DEPENDENCY_NAMES
    assert by_method["GET"].include_in_schema is False


async def test_audit_raises_if_the_real_post_loses_its_guard():
    """On the live app object (in memory only, restored in `finally`)."""
    from fastapi.routing import APIRoute

    from app.api.route_audit import assert_api_routes_authenticated
    from app.main import app

    post = next(
        r for r in app.routes
        if isinstance(r, APIRoute) and r.path == PAGE and "POST" in (r.methods or ())
    )
    original = list(post.dependant.dependencies)
    try:
        post.dependant.dependencies = [
            d for d in original if getattr(d.call, "__name__", "") != "verify_backend_secret"
        ]
        with pytest.raises(RuntimeError) as exc_info:
            assert_api_routes_authenticated(app)
        assert "POST /api/tv-auth/approve" in str(exc_info.value)
    finally:
        post.dependant.dependencies = original
    assert_api_routes_authenticated(app)
