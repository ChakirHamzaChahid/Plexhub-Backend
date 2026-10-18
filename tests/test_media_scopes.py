"""B2-02 — scopes `media.read` / `media.write`, gardes nommés et `route_audit`
par préfixe (ADR 0010 § D2.1–D2.3/D4, PRD FR-001).

- `KNOWN_SCOPES` à 4 valeurs ; `Literal` de l'API aligné ; `scrape media.read`
  est une combinaison valide, normalisée (triée).
- Matrice des gardes `verify_media_read_scope` / `verify_media_write_scope`.
- La clé combinée `scrape media.read` est confinée : refusée par les gardes
  historiques (`verify_backend_secret`, `verify_api_key`), admise par les gardes
  `scrape` ET `media.read`, refusée par `media.write`.
- `route_audit` : un garde de scope ne vaut QUE sous son propre préfixe.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import deps
from app.api.deps import (
    verify_backend_secret,
    verify_media_read_scope,
    verify_media_write_scope,
    verify_scrape_admin_scope,
    verify_scrape_scope,
)
from app.api.route_audit import (
    _AUTH_DEPENDENCY_NAMES,
    assert_api_routes_authenticated,
)
from app.config import settings
from app.db import database as db_module
from app.db.database import _VEC_LOADED
from app.models.database import ApiKey
from app.services import api_key_service
from app.utils.time import now_ms

MASTER = "test-master-key-media-scopes"


@pytest_asyncio.fixture(autouse=True)
async def _wire(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER)
    monkeypatch.setitem(_VEC_LOADED, "ok", True)
    return db_factory


async def _key(db_factory, *, scopes: str = "", revoked=False, expired=False) -> str:
    plaintext = api_key_service.generate_token()
    now = now_ms()
    async with db_factory() as db:
        db.add(ApiKey(
            id=uuid.uuid4().hex,
            key_hash=api_key_service._hash(plaintext),
            key_prefix=plaintext[:10],
            label="t",
            created_at=now - 10_000,
            expires_at=(now - 1_000) if expired else None,
            revoked_at=(now - 1_000) if revoked else None,
            scopes=scopes,
        ))
        await db.commit()
    return plaintext


@pytest_asyncio.fixture
async def keys(db_factory):
    return {
        "master": MASTER,
        "none": await _key(db_factory),
        "scrape": await _key(db_factory, scopes="scrape"),
        "read": await _key(db_factory, scopes="media.read"),
        "write": await _key(db_factory, scopes="media.write"),
        "scrape+read": await _key(db_factory, scopes="media.read scrape"),
        "read+write": await _key(db_factory, scopes="media.read media.write"),
        "unknown": await _key(db_factory, scopes="zz-unknown"),
        "revoked": await _key(db_factory, scopes="media.read media.write", revoked=True),
        "expired": await _key(db_factory, scopes="media.read media.write", expired=True),
        "bogus": "phk_not-a-real-key",
        "missing": None,
    }


# ─── Ensemble des scopes ────────────────────────────────────────────────────


def test_known_scopes_has_the_four_scopes():
    assert api_key_service.KNOWN_SCOPES == frozenset(
        {"scrape", "scrape.admin", "media.read", "media.write"}
    )


def test_api_literal_matches_known_scopes():
    import typing

    from app.api.api_keys import ApiKeyCreate

    annotation = ApiKeyCreate.model_fields["scopes"].annotation
    (item,) = typing.get_args(annotation)
    assert frozenset(typing.get_args(item)) == api_key_service.KNOWN_SCOPES


def test_normalize_scopes_accepts_the_combined_read_key_and_sorts():
    assert api_key_service.normalize_scopes(["scrape", "media.read"]) == "media.read scrape"
    assert (
        api_key_service.normalize_scopes(["media.write", "media.read", "media.read"])
        == "media.read media.write"
    )


@pytest.mark.parametrize("bad", [["media"], ["media.read media.write"], ["media.admin"], ["MEDIA.READ"]])
def test_normalize_scopes_still_rejects_unknown_values(bad):
    with pytest.raises(ValueError):
        api_key_service.normalize_scopes(bad)


# ─── Matrice des gardes (app de test) ───────────────────────────────────────


@pytest.fixture
def scope_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/api/_media_scope_test")

    @router.get("/read", dependencies=[Depends(verify_media_read_scope)])
    async def read():
        return {}

    @router.post("/write", dependencies=[Depends(verify_media_write_scope)])
    async def write():
        return {}

    @router.get("/scrape", dependencies=[Depends(verify_scrape_scope)])
    async def scrape():
        return {}

    @router.get("/legacy", dependencies=[Depends(verify_backend_secret)])
    async def legacy():
        return {}

    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def scope_client(scope_app):
    async with AsyncClient(transport=ASGITransport(app=scope_app), base_url="http://t") as c:
        yield c


READ_EXPECTED = {
    "master": 200, "none": 403, "scrape": 403, "read": 200, "write": 403,
    "scrape+read": 200, "read+write": 200, "unknown": 403,
    "revoked": 401, "expired": 401, "bogus": 401, "missing": 401,
}
WRITE_EXPECTED = {
    "master": 200, "none": 403, "scrape": 403, "read": 403, "write": 200,
    "scrape+read": 403, "read+write": 200, "unknown": 403,
    "revoked": 401, "expired": 401, "bogus": 401, "missing": 401,
}
SCRAPE_EXPECTED = {
    "master": 200, "none": 403, "scrape": 200, "read": 403, "write": 403,
    "scrape+read": 200, "read+write": 403, "unknown": 403,
    "revoked": 401, "expired": 401, "bogus": 401, "missing": 401,
}
LEGACY_EXPECTED = {
    "master": 200, "none": 200, "scrape": 403, "read": 403, "write": 403,
    "scrape+read": 403, "read+write": 403, "unknown": 403,
    "revoked": 401, "expired": 401, "bogus": 401, "missing": 401,
}


def _headers(key):
    return {} if key is None else {"X-API-Key": key}


@pytest.mark.parametrize("who", sorted(READ_EXPECTED))
async def test_verify_media_read_scope_matrix(scope_client, keys, who):
    resp = await scope_client.get("/api/_media_scope_test/read", headers=_headers(keys[who]))
    assert resp.status_code == READ_EXPECTED[who], (who, resp.text)
    if resp.status_code in (401, 403):
        code = {401: "invalid_api_key", 403: "insufficient_scope"}[resp.status_code]
        assert resp.json()["detail"]["code"] == code


@pytest.mark.parametrize("who", sorted(WRITE_EXPECTED))
async def test_verify_media_write_scope_matrix(scope_client, keys, who):
    resp = await scope_client.post("/api/_media_scope_test/write", headers=_headers(keys[who]))
    assert resp.status_code == WRITE_EXPECTED[who], (who, resp.text)
    if resp.status_code in (401, 403):
        code = {401: "invalid_api_key", 403: "insufficient_scope"}[resp.status_code]
        assert resp.json()["detail"]["code"] == code


@pytest.mark.parametrize("who", sorted(SCRAPE_EXPECTED))
async def test_scrape_guard_matrix_with_media_keys(scope_client, keys, who):
    resp = await scope_client.get("/api/_media_scope_test/scrape", headers=_headers(keys[who]))
    assert resp.status_code == SCRAPE_EXPECTED[who], (who, resp.text)


@pytest.mark.parametrize("who", sorted(LEGACY_EXPECTED))
async def test_legacy_guard_refuses_every_scoped_key(scope_client, keys, who):
    resp = await scope_client.get("/api/_media_scope_test/legacy", headers=_headers(keys[who]))
    assert resp.status_code == LEGACY_EXPECTED[who], (who, resp.text)


async def test_write_scope_does_not_include_read_scope(scope_client, keys):
    assert (
        await scope_client.get("/api/_media_scope_test/read", headers=_headers(keys["write"]))
    ).status_code == 403


# ─── route_audit : garde de scope ⇒ son propre préfixe seulement ────────────


def test_recognised_guard_names_are_exactly_the_eight_known_guards():
    assert _AUTH_DEPENDENCY_NAMES == frozenset(
        {
            "verify_backend_secret",
            "verify_api_key",
            "verify_master_key",
            "verify_admin_basic_auth",
            "verify_scrape_scope",
            "verify_scrape_admin_scope",
            "verify_media_read_scope",
            "verify_media_write_scope",
        }
    )


def _app_with(guard, path: str) -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.get(path, dependencies=[Depends(guard)])
    async def handler():
        return {}

    app.include_router(router)
    return app


@pytest.mark.parametrize("guard", [verify_media_read_scope, verify_media_write_scope])
def test_media_guards_satisfy_the_audit_under_metadata_prefix(guard):
    assert_api_routes_authenticated(_app_with(guard, "/api/metadata/v1/x"))  # no raise


@pytest.mark.parametrize("guard", [verify_scrape_scope, verify_scrape_admin_scope])
def test_scrape_guards_satisfy_the_audit_under_scrape_prefix(guard):
    assert_api_routes_authenticated(_app_with(guard, "/api/scrape/v1/x"))  # no raise


@pytest.mark.parametrize(
    "guard,path",
    [
        (verify_media_read_scope, "/api/media/oops"),
        (verify_media_write_scope, "/api/media/oops"),
        (verify_media_read_scope, "/api/scrape/v1/oops"),
        (verify_media_write_scope, "/api/scrape/v1/oops"),
        (verify_scrape_scope, "/api/metadata/v1/oops"),
        (verify_scrape_admin_scope, "/api/metadata/v1/oops"),
    ],
)
def test_scope_guard_outside_its_own_prefix_counts_as_unguarded(guard, path):
    with pytest.raises(RuntimeError) as exc_info:
        assert_api_routes_authenticated(_app_with(guard, path))
    assert path in str(exc_info.value)


def test_metadata_route_without_any_guard_refuses_boot():
    app = FastAPI()
    router = APIRouter()

    @router.get("/api/metadata/v1/bare")
    async def bare():
        return {}

    app.include_router(router)
    with pytest.raises(RuntimeError):
        assert_api_routes_authenticated(app)


def test_media_guard_is_a_real_deps_function():
    assert deps.verify_media_read_scope.__name__ == "verify_media_read_scope"
    assert deps.verify_media_write_scope.__name__ == "verify_media_write_scope"
