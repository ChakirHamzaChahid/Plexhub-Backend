"""SSRF protection tests for XtreamService, /api/accounts, and download_to_disk (CR-S03, CR-S04)."""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import XtreamAccount
from app.services.download_service import DownloadPermanentError, download_to_disk
from app.services.xtream_service import XtreamService
from app.utils import ssrf
from app.utils.ssrf import SsrfBlockedError

API_KEY = "test-master-key"
API_HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def _reset_ssrf_cache(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    ssrf.clear_cache()
    yield
    ssrf.clear_cache()


@pytest_asyncio.fixture
async def seeded_account(db_engine, monkeypatch):
    from app.db import database as db_module

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session_factory", factory)
    async with factory() as s:
        s.add(
            XtreamAccount(
                id="acc_ssrf",
                label="Public",
                base_url="http://public.example",
                port=80,
                username="u",
                password="p",
                is_active=True,
                created_at=0,
            )
        )
        await s.commit()
    return factory


async def test_xtream_service_blocks_private_ip_via_ssrf_hook():
    """CR-S03: XtreamService client must reject loopback/metadata targets before sending."""
    svc = XtreamService()
    try:
        with respx.mock(assert_all_called=False) as router:
            route = router.get("http://169.254.169.254/player_api.php").mock(
                return_value=httpx.Response(200, json={"user_info": {}})
            )
            with pytest.raises(SsrfBlockedError):
                await svc._get("http://169.254.169.254", 80, "u", "p")
            assert not route.called
    finally:
        await svc.close()


async def test_accounts_create_rejects_private_host(api_client, seeded_account):
    """CR-S03: POST /api/accounts must reject loopback/RFC1918/metadata hosts with HTTP 400."""
    resp = await api_client.post(
        "/api/accounts",
        headers=API_HEADERS,
        json={
            "label": "Internal",
            "baseUrl": "http://127.0.0.1",
            "port": 8080,
            "username": "u",
            "password": "p",
        },
    )
    assert resp.status_code == 400
    assert "SSRF guard" in resp.json()["detail"]


async def test_accounts_update_rejects_private_host(api_client, seeded_account):
    """CR-S03: PUT /api/accounts/{id} must reject changing baseUrl to a private host."""
    resp = await api_client.put(
        "/api/accounts/acc_ssrf",
        headers=API_HEADERS,
        json={"baseUrl": "http://169.254.169.254"},
    )
    assert resp.status_code == 400
    assert "SSRF guard" in resp.json()["detail"]


async def test_download_to_disk_rejects_private_initial_url(tmp_path):
    """CR-S04: download_to_disk must vet the initial URL before issuing the first GET."""
    dest = tmp_path / "movie.mp4"
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://127.0.0.1:8080/movie.mp4").mock(
            return_value=httpx.Response(200, content=b"should-not-fetch")
        )
        with pytest.raises(DownloadPermanentError, match="unsafe redirect"):
            await download_to_disk("http://127.0.0.1:8080/movie.mp4", dest)
        assert not route.called
