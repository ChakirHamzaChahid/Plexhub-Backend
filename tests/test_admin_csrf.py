"""tests/test_admin_csrf.py — CSRF guard on POST /admin* (S4.3, AUDIT-P2-005 /
CR-S07, docs/plans/2026-07-26-refacto-audit-v1-plan.md §S4.3).

`app/api/csrf.py::AdminCsrfMiddleware` rejects (403) any POST/PUT/PATCH/DELETE
whose path starts with `/admin` when the browser-set `Sec-Fetch-Site` header
says `cross-site` — the exact shape of a CSRF attack against the Basic-Auth
-gated admin UI (a browser replays cached credentials automatically on a
cross-site request; there is no synchronizer token). `same-origin` and an
ABSENT header (non-browser clients: curl, scripts, this test suite's own
client) are accepted by design — see that module's docstring for the honest
trade-off. GET is never touched, and the `X-API-Key`-guarded JSON mirrors
under `/api/admin/*` are structurally outside the `/admin` path prefix this
middleware matches, so they are unaffected regardless of `Sec-Fetch-Site`.

Uses `POST /admin/keys` (the admin API-key-creation form) as the concrete
mutating endpoint under test — plain `Depends(get_db)` request-scoped
session, easy to wire to the isolated in-memory `db_factory` (no real
filesystem/DB touched, per this repo's test isolation convention).
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.db import database as db_module


pytestmark = pytest.mark.asyncio

ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-csrf-pass"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)
MASTER_KEY = "test-master-csrf-key"


@pytest.fixture(autouse=True)
def _configure_secrets(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER_KEY)


@pytest.fixture(autouse=True)
def _wire_db(monkeypatch, db_factory):
    from app.api import admin as admin_module

    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    # The manual-scrape routes hold `admin.py`'s own import-time reference to
    # `async_session_factory`, which patching the module attribute above does
    # not reach (same reason as tests/test_admin_manual_scrape.py).
    monkeypatch.setattr(admin_module, "async_session_factory", db_factory)


class TestAdminPostBlockedCrossSite:
    async def test_cross_site_post_rejected_with_403(self, api_client):
        resp = await api_client.post(
            "/admin/keys",
            data={"label": "attacker-forged"},
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cross-site request rejected"

    async def test_cross_site_post_does_not_create_the_key(self, api_client):
        await api_client.post(
            "/admin/keys",
            data={"label": "attacker-forged"},
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        table = await api_client.get("/admin/keys/table", auth=ADMIN_AUTH)
        assert table.status_code == 200
        assert "attacker-forged" not in table.text


class TestAdminPostAllowedSameOrigin:
    async def test_same_origin_post_creates_the_key(self, api_client):
        resp = await api_client.post(
            "/admin/keys",
            data={"label": "operator-same-origin"},
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 200
        assert "operator-same-origin" in resp.text

    async def test_same_site_post_creates_the_key(self, api_client):
        """`same-site` (e.g. a same-registrable-domain subdomain) is also
        accepted — only the `cross-site` value is rejected."""
        resp = await api_client.post(
            "/admin/keys",
            data={"label": "operator-same-site"},
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "same-site"},
        )
        assert resp.status_code == 200
        assert "operator-same-site" in resp.text


class TestAdminPostAllowedHeaderAbsent:
    async def test_missing_header_post_creates_the_key(self, api_client):
        """Non-browser clients never send Sec-Fetch-Site at all (curl,
        scripts, this very test client) — accepted by design, see
        app/api/csrf.py's module docstring for the trade-off."""
        resp = await api_client.post(
            "/admin/keys",
            data={"label": "operator-no-header"},
            auth=ADMIN_AUTH,
        )
        assert resp.status_code == 200
        assert "operator-no-header" in resp.text


class TestAdminGetNeverBlocked:
    async def test_cross_site_get_on_keys_page_not_blocked(self, api_client):
        resp = await api_client.get(
            "/admin/keys",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 200

    async def test_cross_site_get_on_downloads_tab_not_blocked(self, api_client):
        resp = await api_client.get(
            "/admin/downloads",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 200

    async def test_cross_site_get_on_plex_downloads_tab_not_blocked(self, api_client):
        resp = await api_client.get(
            "/admin/plex-downloads",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 200

    async def test_cross_site_get_on_unified_downloads_tab_not_blocked(self, api_client):
        resp = await api_client.get(
            "/admin/unified-downloads",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 200


MANUAL_SCRAPE_POSTS = [
    ("/admin/media/xtream_a/vod_1.mp4/apply", {"tmdb_id": "603", "type": "movie"}),
    ("/admin/media/xtream_a/vod_1.mp4/unlock", {}),
    ("/admin/media/xtream_a/vod_1.mp4/clear", {}),
    ("/admin/media/xtream_a/vod_1.mp4/rescrape", {}),
    ("/admin/media/xtream_a/vod_1.mp4/lookup", {"raw_id": "603", "type": "movie"}),
    ("/admin/movies/vod_1.mp4/ids", {"server_id": "xtream_a", "imdb_id": "tt1"}),
    ("/admin/movies/vod_1.mp4/rescrape", {"server_id": "xtream_a"}),
    # W5 (ADR 0005 D8/D10)
    ("/admin/review/xtream_a/vod_1.mp4/apply", {"candidate_index": "0"}),
    ("/admin/review/xtream_a/vod_1.mp4/dismiss", {}),
    ("/admin/scrape-batch/scrape_batch_unknown/cancel", {}),
]


class TestManualScrapePostsCsrfGuarded:
    """ADR 0005 D10 (W2/W4): every manual-scraper write route is a POST under
    `/admin`, so it inherits the same CSRF guard — asserted route by route
    rather than assumed, since a future route mounted under a different
    prefix would silently lose it."""

    @pytest.mark.parametrize("path,data", MANUAL_SCRAPE_POSTS)
    async def test_cross_site_post_rejected_with_403(self, api_client, path, data):
        resp = await api_client.post(
            path, data=data, auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403, path
        assert resp.json()["detail"] == "Cross-site request rejected"

    @pytest.mark.parametrize("path,data", MANUAL_SCRAPE_POSTS)
    async def test_same_origin_post_reaches_the_handler(self, api_client, path, data):
        """Not CSRF-blocked: the route runs and answers 404 for this unknown
        media row (proving the 403 above came from the guard, not from the
        route itself)."""
        resp = await api_client.post(
            path, data=data, auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 404, path


class TestScrapeBatchStartCsrfGuarded:
    """`POST /admin/scrape-batch` can't join the parametrized list above (a
    same-origin call would actually LAUNCH a batch), so its cross-site
    rejection is asserted on its own."""

    async def test_cross_site_post_rejected_with_403(self, api_client):
        resp = await api_client.post(
            "/admin/scrape-batch", data={"type": "movie", "dry_run": "1"},
            auth=ADMIN_AUTH, headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403
        from app.workers import manual_scrape_batch_worker as worker

        assert worker.is_running() is False


class TestJsonMirrorUnaffected:
    """`/api/admin/*` (keys/downloads/plex-downloads/enrichment) is guarded by
    `X-API-Key`, a header a browser never attaches ambiently — never in the
    CSRF threat model — and structurally outside the `/admin` path prefix
    this middleware matches. Prove it end to end: a cross-site POST to the
    JSON mirror still succeeds (not intercepted by this middleware)."""

    async def test_cross_site_post_to_json_key_mirror_is_not_csrf_blocked(self, api_client):
        resp = await api_client.post(
            "/api/admin/keys",
            json={"label": "json-mirror-key"},
            headers={"X-API-Key": MASTER_KEY, "Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 201
        assert resp.json()["label"] == "json-mirror-key"
