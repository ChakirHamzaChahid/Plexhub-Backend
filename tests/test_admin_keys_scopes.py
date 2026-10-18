"""B1-04 — scopes dans l'admin HTML `/admin/keys` (ADR 0008 § D2.5, FR-006, US1-AC5).

Deux cases à cocher (`scrape`, `scrape.admin`) dans le formulaire, colonne
« Scopes » dans le tableau, scopes affichés sur l'écran de clé créée. La
validation vit dans `api_key_service.create_key` : une valeur inconnue ou
« scrape scrape.admin » dans un seul champ ⇒ fragment d'erreur (422, swappé
par l'opt-in `htmx:beforeSwap` de base.html), aucune clé créée.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.db import database as db_module
from app.services import api_key_service

ADMIN_AUTH = ("admin", "test-admin-keys-scopes-pass")


@pytest.fixture(autouse=True)
def _configure(monkeypatch, db_factory):
    from app.api import admin as admin_module

    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_AUTH[0])
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_AUTH[1])
    monkeypatch.setattr(settings, "AI_API_KEY", "test-master-admin-keys-scopes")
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(admin_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)


async def _keys(db_factory):
    async with db_factory() as db:
        return await api_key_service.list_keys(db)


async def test_form_offers_both_scope_checkboxes(api_client):
    resp = await api_client.get("/admin/keys", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    html = resp.text
    for value in ("scrape", "scrape.admin"):
        assert f'type="checkbox" name="scopes" value="{value}"' in html
    assert "Scopes" in html


async def test_create_with_both_scopes(api_client, db_factory):
    resp = await api_client.post(
        "/admin/keys",
        data={"label": "plexhubTMM", "scopes": ["scrape.admin", "scrape"]},
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("HX-Trigger") == "refresh-keys"
    assert "phk_" in resp.text
    assert "scrape.admin" in resp.text
    rows = await _keys(db_factory)
    assert [r.scopes for r in rows] == ["scrape scrape.admin"]

    table = await api_client.get("/admin/keys/table", auth=ADMIN_AUTH)
    assert "Scopes" in table.text
    assert "scrape.admin" in table.text


async def test_create_without_scope_is_an_unscoped_key(api_client, db_factory):
    resp = await api_client.post("/admin/keys", data={"label": "Salon TV"}, auth=ADMIN_AUTH)
    assert resp.status_code == 200
    rows = await _keys(db_factory)
    assert [r.scopes for r in rows] == [""]
    table = await api_client.get("/admin/keys/table", auth=ADMIN_AUTH)
    assert "aucun" in table.text


@pytest.mark.parametrize("bad", ["scrape scrape.admin", "bogus", "SCRAPE", ""])
async def test_invalid_scope_value_shows_error_and_creates_nothing(api_client, db_factory, bad):
    resp = await api_client.post(
        "/admin/keys", data={"label": "bad", "scopes": [bad]}, auth=ADMIN_AUTH,
    )
    assert resp.status_code == 422
    assert "phk_" not in resp.text
    assert "Scope inconnu" in resp.text
    assert "HX-Trigger" not in resp.headers
    assert await _keys(db_factory) == []


async def test_error_message_is_html_escaped(api_client, db_factory):
    resp = await api_client.post(
        "/admin/keys", data={"label": "x", "scopes": ["<script>x</script>"]}, auth=ADMIN_AUTH,
    )
    assert resp.status_code == 422
    assert "<script>x</script>" not in resp.text
    assert await _keys(db_factory) == []


async def test_cross_site_post_with_scopes_still_rejected(api_client, db_factory):
    resp = await api_client.post(
        "/admin/keys",
        data={"label": "forged", "scopes": ["scrape"]},
        auth=ADMIN_AUTH,
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403
    assert await _keys(db_factory) == []


async def test_post_requires_basic_auth(api_client, db_factory):
    resp = await api_client.post("/admin/keys", data={"label": "x", "scopes": ["scrape"]})
    assert resp.status_code == 401
    assert await _keys(db_factory) == []
