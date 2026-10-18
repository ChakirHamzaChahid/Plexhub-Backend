"""B1-02 — scopes des clés d'API (ADR 0008 § D2.1/D2.5, PRD FR-001/FR-002).

Service : `api_key_service.create_key(..., scopes=...)` valide contre l'ensemble
fermé `KNOWN_SCOPES` (tri, dédoublonnage, rejet de toute valeur inconnue ou
contenant une espace ⇒ `InvalidScopeError`, sous-classe de `ValueError`) et
stocke `" ".join(sorted)`. `parse_scopes(raw)` relit la colonne et ignore les
valeurs inconnues (journalisées une fois).

API : `POST /api/admin/keys` accepte `scopes` (liste de `Literal`), `GET` les
rend (`[]` pour une clé sans scope), toujours sous `verify_master_key`.
"""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.db import database as db_module
from app.models.database import ApiKey
from app.services import api_key_service

MASTER = "test-master-key-scopes"
MASTER_HEADERS = {"X-API-Key": MASTER}


@pytest_asyncio.fixture(autouse=True)
async def _wire(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER)
    return db_factory


# ─── Constante unique ───────────────────────────────────────────────────────


def test_known_scopes_is_the_closed_set():
    assert api_key_service.KNOWN_SCOPES == frozenset(
        {"scrape", "scrape.admin", "media.read", "media.write"}
    )
    assert isinstance(api_key_service.KNOWN_SCOPES, frozenset)


def test_invalid_scope_error_is_a_value_error():
    assert issubclass(api_key_service.InvalidScopeError, ValueError)


# ─── create_key : validation et stockage ────────────────────────────────────


async def test_create_key_without_scopes_stores_empty_string(db_session):
    row, _ = await api_key_service.create_key(db_session, label="Sans scope")
    assert row.scopes == ""
    persisted = (
        await db_session.execute(select(ApiKey).where(ApiKey.id == row.id))
    ).scalars().one()
    assert persisted.scopes == ""


async def test_create_key_sorts_and_deduplicates(db_session):
    row, _ = await api_key_service.create_key(
        db_session, label="TMM", scopes=["scrape.admin", "scrape", "scrape"],
    )
    assert row.scopes == "scrape scrape.admin"


async def test_create_key_single_scope(db_session):
    row, _ = await api_key_service.create_key(db_session, label="TMM", scopes=("scrape",))
    assert row.scopes == "scrape"


@pytest.mark.parametrize(
    "bad",
    [
        ["admin"],
        ["scrape", "nope"],
        ["scrape scrape.admin"],   # deux scopes dans une seule valeur
        [" scrape"],
        ["scrape\t"],
        [""],
        ["SCRAPE"],
        [123],
    ],
)
async def test_create_key_rejects_unknown_or_spaced_values(db_session, bad):
    with pytest.raises(api_key_service.InvalidScopeError):
        await api_key_service.create_key(db_session, label="X", scopes=bad)
    # Aucune clé créée.
    assert await api_key_service.list_keys(db_session) == []


async def test_create_key_rejects_a_bare_string(db_session):
    """Une chaîne nue serait itérée caractère par caractère : refus explicite."""
    with pytest.raises(api_key_service.InvalidScopeError):
        await api_key_service.create_key(db_session, label="X", scopes="scrape")
    assert await api_key_service.list_keys(db_session) == []


async def test_resolve_returns_the_row_with_its_scopes(db_session):
    _row, plaintext = await api_key_service.create_key(
        db_session, label="TMM", scopes=["scrape"],
    )
    resolved = await api_key_service.resolve(plaintext)
    assert resolved is not None
    assert resolved.scopes == "scrape"


# ─── parse_scopes ───────────────────────────────────────────────────────────


def test_parse_scopes_empty():
    assert api_key_service.parse_scopes("") == frozenset()
    assert api_key_service.parse_scopes(None) == frozenset()


def test_parse_scopes_known_values():
    assert api_key_service.parse_scopes("scrape scrape.admin") == frozenset(
        {"scrape", "scrape.admin"}
    )


def test_parse_scopes_ignores_unknown_and_logs_once(caplog):
    api_key_service._LOGGED_UNKNOWN_SCOPES.discard("zz-unknown-scope")
    # Le logger `plexhub` ne propage plus vers la racine une fois `app.main`
    # importé (config de logging) : on attache le handler de capture directement.
    svc_logger = api_key_service.logger
    svc_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="plexhub.apikey"):
            assert api_key_service.parse_scopes("scrape zz-unknown-scope") == frozenset({"scrape"})
            assert api_key_service.parse_scopes("zz-unknown-scope") == frozenset()
    finally:
        svc_logger.removeHandler(caplog.handler)
    # Selon la config de logging, le même enregistrement peut atteindre caplog par
    # deux handlers (le nôtre et celui de la racine) : on compte les enregistrements
    # distincts, pas les captures.
    hits = list({id(r): r for r in caplog.records if "zz-unknown-scope" in r.getMessage()}.values())
    assert len(hits) == 1


# ─── API /api/admin/keys ────────────────────────────────────────────────────


async def test_api_create_with_scopes_returns_them(api_client):
    resp = await api_client.post(
        "/api/admin/keys",
        json={"label": "plexhubTMM", "expiresInDays": 365, "scopes": ["scrape"]},
        headers=MASTER_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["scopes"] == ["scrape"]
    assert body["key"].startswith("phk_")


async def test_api_create_deduplicates(api_client):
    resp = await api_client.post(
        "/api/admin/keys",
        json={"label": "dup", "scopes": ["scrape.admin", "scrape", "scrape.admin"]},
        headers=MASTER_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scopes"] == ["scrape", "scrape.admin"]


async def test_api_create_without_scopes_field_gives_empty_list(api_client):
    resp = await api_client.post(
        "/api/admin/keys", json={"label": "legacy"}, headers=MASTER_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["scopes"] == []


@pytest.mark.parametrize("bad", [["admin"], ["scrape scrape.admin"], "scrape", [None]])
async def test_api_create_unknown_scope_is_422_and_creates_nothing(api_client, bad):
    resp = await api_client.post(
        "/api/admin/keys", json={"label": "bad", "scopes": bad}, headers=MASTER_HEADERS,
    )
    assert resp.status_code == 422
    listing = await api_client.get("/api/admin/keys", headers=MASTER_HEADERS)
    assert listing.json() == []


async def test_api_service_value_error_maps_to_422(api_client, monkeypatch):
    """Le `Literal` n'est qu'une première barrière : une `ValueError` du
    service (seconde barrière) doit devenir un 422, jamais un 500."""

    async def _boom(db, **kwargs):
        raise api_key_service.InvalidScopeError("scope inconnu")

    monkeypatch.setattr(api_key_service, "create_key", _boom)
    resp = await api_client.post(
        "/api/admin/keys", json={"label": "x", "scopes": ["scrape"]}, headers=MASTER_HEADERS,
    )
    assert resp.status_code == 422
    assert "scope" in resp.text


async def test_api_list_returns_scopes(api_client):
    await api_client.post(
        "/api/admin/keys", json={"label": "a", "scopes": ["scrape"]}, headers=MASTER_HEADERS,
    )
    await api_client.post("/api/admin/keys", json={"label": "b"}, headers=MASTER_HEADERS)
    listing = await api_client.get("/api/admin/keys", headers=MASTER_HEADERS)
    assert listing.status_code == 200
    by_label = {k["label"]: k["scopes"] for k in listing.json()}
    assert by_label == {"a": ["scrape"], "b": []}


async def test_api_create_with_scoped_key_is_still_401_master_required(api_client, db_factory):
    async with db_factory() as db:
        _row, plaintext = await api_key_service.create_key(
            db, label="TMM", scopes=["scrape", "scrape.admin"],
        )
    resp = await api_client.post(
        "/api/admin/keys", json={"label": "no"}, headers={"X-API-Key": plaintext},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Master key required"


def test_api_literal_matches_known_scopes():
    """Le `Literal` de l'API reflète exactement la constante du service."""
    import typing

    from app.api.api_keys import ApiKeyCreate

    ann = ApiKeyCreate.model_fields["scopes"].annotation
    (lit,) = typing.get_args(ann)
    assert frozenset(typing.get_args(lit)) == api_key_service.KNOWN_SCOPES
