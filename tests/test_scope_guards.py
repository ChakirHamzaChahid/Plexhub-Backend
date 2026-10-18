"""B1-03 — `Principal`, gardes à scopes et confinement des clés à scopes
(ADR 0008 § D2.2–D2.4/D4, PRD FR-003→FR-005, US1-AC2/AC3/AC6/AC7).

- Matrice des deux gardes nommés `verify_scrape_scope` / `verify_scrape_admin_scope`
  sur une petite app de test (la passerelle réelle arrive avec B1-11).
- `verify_backend_secret` / `verify_api_key` : 403 `insufficient_scope` pour toute
  clé dont la colonne `scopes` n'est pas vide ; maître et clé sans scope inchangés.
- Parcours de `app.main.app.routes` : une clé `scrape` reçoit 403 sur CHAQUE route
  `/api/*` gardée par `verify_backend_secret`/`verify_api_key` (y compris
  `POST /api/tv-auth/approve`), 401 `Master key required` sur `/api/admin/*`.
  Les requêtes sont réelles : le garde refuse AVANT le handler, aucun effet de bord.
"""
from __future__ import annotations

import re
import uuid

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.api import deps
from app.api.deps import (
    Principal,
    verify_api_key,
    verify_backend_secret,
    verify_scrape_admin_scope,
    verify_scrape_scope,
)
from app.api.route_audit import _dependency_names
from app.config import settings
from app.db import database as db_module
from app.db.database import _VEC_LOADED
from app.models.database import ApiKey
from app.services import api_key_service
from app.utils.time import now_ms

MASTER = "test-master-key-scope-guards"


@pytest_asyncio.fixture(autouse=True)
async def _wire(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER)
    monkeypatch.setitem(_VEC_LOADED, "ok", True)
    return db_factory


async def _key(db_factory, *, scopes: str = "", revoked=False, expired=False) -> str:
    """Insère une clé brute (permet des scopes inconnus) et rend son clair."""
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
        "admin": await _key(db_factory, scopes="scrape.admin"),
        "both": await _key(db_factory, scopes="scrape scrape.admin"),
        "unknown": await _key(db_factory, scopes="zz-unknown"),
        "revoked": await _key(db_factory, scopes="scrape scrape.admin", revoked=True),
        "expired": await _key(db_factory, scopes="scrape scrape.admin", expired=True),
        "bogus": "phk_not-a-real-key",
        "missing": None,
    }


# ─── Principal ──────────────────────────────────────────────────────────────


def test_principal_is_frozen():
    p = Principal(is_master=True, key_id=None, scopes=frozenset())
    with pytest.raises(Exception):
        p.is_master = False  # type: ignore[misc]


async def test_resolve_principal_master_and_keys(keys):
    master = await deps._resolve_principal(MASTER, None)
    assert master is not None and master.is_master and master.key_id is None

    scoped = await deps._resolve_principal(keys["both"], None)
    assert scoped is not None and not scoped.is_master
    assert scoped.key_id is not None
    assert scoped.scopes == frozenset({"scrape", "scrape.admin"})

    unknown = await deps._resolve_principal(keys["unknown"], None)
    assert unknown is not None
    assert unknown.scopes == frozenset()     # aucun droit…
    assert unknown.confined is True           # …mais clé confinée

    plain = await deps._resolve_principal(keys["none"], None)
    assert plain is not None and plain.confined is False

    assert await deps._resolve_principal(None, None) is None
    assert await deps._resolve_principal(keys["revoked"], None) is None
    assert await deps._resolve_principal(keys["bogus"], None) is None


async def test_authenticate_stays_a_boolean_wrapper(keys):
    assert await deps._authenticate(MASTER, None) is True
    assert await deps._authenticate(keys["scrape"], None) is True
    assert await deps._authenticate(keys["revoked"], None) is False
    assert await deps._authenticate(None, None) is False


# ─── Matrice des deux nouveaux gardes (app de test) ────────────────────────


@pytest.fixture
def scope_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/api/_scope_test")

    def _who(request: Request) -> dict:
        p = request.state.principal
        return {"master": p.is_master, "keyId": p.key_id, "scopes": sorted(p.scopes)}

    @router.get("/read", dependencies=[Depends(verify_scrape_scope)])
    async def read(request: Request):
        return _who(request)

    @router.post("/admin", dependencies=[Depends(verify_scrape_admin_scope)])
    async def admin(request: Request):
        return _who(request)

    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def scope_client(scope_app):
    async with AsyncClient(transport=ASGITransport(app=scope_app), base_url="http://t") as c:
        yield c


READ_EXPECTED = {
    "master": 200, "none": 403, "scrape": 200, "admin": 403, "both": 200,
    "unknown": 403, "revoked": 401, "expired": 401, "bogus": 401, "missing": 401,
}
ADMIN_EXPECTED = {
    "master": 200, "none": 403, "scrape": 403, "admin": 200, "both": 200,
    "unknown": 403, "revoked": 401, "expired": 401, "bogus": 401, "missing": 401,
}


def _headers(key: str | None) -> dict:
    return {} if key is None else {"X-API-Key": key}


def _assert_error(resp, status: int) -> None:
    body = resp.json()
    code = {401: "invalid_api_key", 403: "insufficient_scope"}[status]
    assert body["detail"]["code"] == code, body
    assert isinstance(body["detail"]["message"], str) and body["detail"]["message"]


@pytest.mark.parametrize("who", sorted(READ_EXPECTED))
async def test_verify_scrape_scope_matrix(scope_client, keys, who):
    resp = await scope_client.get("/api/_scope_test/read", headers=_headers(keys[who]))
    assert resp.status_code == READ_EXPECTED[who], (who, resp.text)
    if resp.status_code != 200:
        _assert_error(resp, resp.status_code)


@pytest.mark.parametrize("who", sorted(ADMIN_EXPECTED))
async def test_verify_scrape_admin_scope_matrix(scope_client, keys, who):
    resp = await scope_client.post("/api/_scope_test/admin", headers=_headers(keys[who]))
    assert resp.status_code == ADMIN_EXPECTED[who], (who, resp.text)
    if resp.status_code != 200:
        _assert_error(resp, resp.status_code)


async def test_principal_is_set_on_request_state(scope_client, keys):
    resp = await scope_client.get("/api/_scope_test/read", headers=_headers(keys["both"]))
    body = resp.json()
    assert body["master"] is False and body["keyId"]
    assert body["scopes"] == ["scrape", "scrape.admin"]
    resp = await scope_client.get("/api/_scope_test/read", headers=_headers(MASTER))
    assert resp.json() == {"master": True, "keyId": None, "scopes": []}


async def test_error_bodies_never_echo_the_key(scope_client, keys):
    for who in ("scrape", "bogus", "revoked"):
        resp = await scope_client.post("/api/_scope_test/admin", headers=_headers(keys[who]))
        assert keys[who] not in resp.text


async def test_new_guards_accept_request_none(keys):
    """Signatures compatibles `request=None` (comme les tests existants)."""
    await verify_scrape_scope(request=None, x_api_key=MASTER)
    await verify_scrape_admin_scope(request=None, x_api_key=keys["admin"])
    with pytest.raises(HTTPException) as exc:
        await verify_scrape_scope(request=None, x_api_key=keys["admin"])
    assert exc.value.status_code == 403


# ─── Gardes existants : confinement des clés à scopes ──────────────────────


@pytest.mark.parametrize("who", ["scrape", "admin", "both", "unknown"])
@pytest.mark.parametrize("guard", [verify_backend_secret, verify_api_key])
async def test_existing_guards_refuse_scoped_keys_with_403(keys, guard, who):
    with pytest.raises(HTTPException) as exc:
        await guard(request=None, x_api_key=keys[who])
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "insufficient_scope"


@pytest.mark.parametrize("who", ["master", "none"])
@pytest.mark.parametrize("guard", [verify_backend_secret, verify_api_key])
async def test_existing_guards_unchanged_for_master_and_unscoped(keys, guard, who):
    assert await guard(request=None, x_api_key=keys[who]) is None


@pytest.mark.parametrize("who", ["revoked", "expired", "bogus", "missing"])
@pytest.mark.parametrize("guard", [verify_backend_secret, verify_api_key])
async def test_existing_guards_keep_their_401_text(keys, guard, who):
    with pytest.raises(HTTPException) as exc:
        await guard(request=None, x_api_key=keys[who])
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid API key"


async def test_verify_api_key_still_checks_vec_first(keys, monkeypatch):
    monkeypatch.setitem(_VEC_LOADED, "ok", False)
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(request=None, x_api_key=keys["scrape"])
    assert exc.value.status_code == 503


# ─── Parcours des routes réelles de l'app ──────────────────────────────────


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _api_routes():
    from app.main import app

    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            yield route


def _guarded_by_json_guards() -> list[tuple[str, str]]:
    out = []
    for route in _api_routes():
        names = _dependency_names(route)
        if "verify_master_key" in names:
            continue
        if names & {"verify_backend_secret", "verify_api_key"}:
            for m in sorted(route.methods or ()):
                if m != "HEAD":
                    out.append((m, route.path))
    return out


def _guarded_by_master() -> list[tuple[str, str]]:
    out = []
    for route in _api_routes():
        if "verify_master_key" in _dependency_names(route):
            for m in sorted(route.methods or ()):
                if m != "HEAD":
                    out.append((m, route.path))
    return out


def test_route_walk_sees_the_expected_families():
    walked = _guarded_by_json_guards()
    paths = {p for _m, p in walked}
    assert ("POST", "/api/tv-auth/approve") in walked
    assert any(p.startswith("/api/ai/") for p in paths)
    assert any(p.startswith("/api/media") for p in paths)
    assert any(p.startswith("/api/accounts") for p in paths)
    assert any(p.startswith("/api/sync") for p in paths)
    assert any(p.startswith("/api/stream") for p in paths)
    assert len(walked) > 30


async def test_scrape_key_gets_403_on_every_json_api_route(api_client, keys):
    failures = []
    for method, path in _guarded_by_json_guards():
        resp = await api_client.request(
            method, _concrete(path), headers={"X-API-Key": keys["scrape"]},
        )
        ok = resp.status_code == 403
        if ok:
            try:
                ok = resp.json()["detail"]["code"] == "insufficient_scope"
            except Exception:
                ok = False
        if not ok:
            failures.append((method, path, resp.status_code, resp.text[:120]))
    assert not failures, failures


async def test_scoped_key_on_admin_api_is_still_401_master_required(api_client, keys):
    routes = _guarded_by_master()
    assert routes
    failures = []
    for method, path in routes:
        resp = await api_client.request(
            method, _concrete(path), headers={"X-API-Key": keys["both"]},
        )
        if resp.status_code != 401 or resp.json().get("detail") != "Master key required":
            failures.append((method, path, resp.status_code, resp.text[:120]))
    assert not failures, failures


@pytest.mark.parametrize("path", ["/api/accounts", "/api/media/movies"])
async def test_unscoped_key_still_reaches_sample_routes(api_client, keys, path):
    resp = await api_client.get(path, headers={"X-API-Key": keys["none"]})
    assert 200 <= resp.status_code < 300, resp.text
    resp = await api_client.get(path, headers={"X-API-Key": keys["scrape"]})
    assert resp.status_code == 403
