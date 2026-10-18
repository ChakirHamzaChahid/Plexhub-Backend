"""Test structurel route par route de `/api/metadata/v1` (ADR 0010 § D2.3/D4) : `route_audit` ne
distingue pas `verify_media_read_scope` de `verify_media_write_scope`, ce test fige donc le garde
EXACT de chaque route — une route d'écriture ne porte jamais le garde de lecture, et inversement —
et l'ordre garde → limiteurs (le limiteur lit le `Principal` posé par le garde)."""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.api import metadata_v1
from app.api.deps import verify_media_read_scope, verify_media_write_scope
from app.api.metadata_v1 import enforce_metadata_rate_limit, enforce_metadata_write_rate_limit

P = "/api/metadata/v1"
READ, WRITE = verify_media_read_scope, verify_media_write_scope

EXPECTED = {
    ("GET", f"{P}/lookup"): READ,
    ("GET", f"{P}/items"): READ,
    ("GET", f"{P}/items/{{serverId}}/{{ratingKey}}"): READ,
    ("GET", f"{P}/items/{{serverId}}/{{ratingKey}}/episodes"): READ,
    ("POST", f"{P}/items/{{serverId}}/{{ratingKey}}/preview"): READ,           # simulation => read
    ("PATCH", f"{P}/items/{{serverId}}/{{ratingKey}}"): WRITE,
    ("GET", f"{P}/items/{{serverId}}/{{ratingKey}}/history"): READ,
    ("GET", f"{P}/edits/{{editId}}"): READ,
    ("POST", f"{P}/edits/{{editId}}/revert/preview"): READ,                      # simulation => read
    ("POST", f"{P}/edits/{{editId}}/revert"): WRITE,
    ("POST", f"{P}/items/{{serverId}}/{{ratingKey}}/lock/preview"): READ,        # simulation => read
    ("POST", f"{P}/items/{{serverId}}/{{ratingKey}}/lock"): WRITE,
    ("GET", f"{P}/backup/status"): READ,
    ("GET", f"{P}/fields"): READ,
}


def _routes() -> dict[tuple[str, str], APIRoute]:
    out: dict[tuple[str, str], APIRoute] = {}
    for route in metadata_v1.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods or ():
            out[(method, route.path)] = route
    return out


def _chain(route: APIRoute) -> list:
    return [d.call for d in route.dependant.dependencies]


def test_the_router_exposes_exactly_the_fourteen_routes():
    assert set(_routes()) == set(EXPECTED)


def test_the_router_has_no_router_level_dependency():
    assert metadata_v1.router.dependencies == []


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_each_route_carries_exactly_its_guard_then_its_rate_limits(key):
    route = _routes()[key]
    expected_guard = EXPECTED[key]
    other_guard = WRITE if expected_guard is READ else READ
    chain = _chain(route)
    assert expected_guard in chain, key
    assert other_guard not in chain, f"{key} must not carry the other scope guard"
    assert chain.index(expected_guard) < chain.index(enforce_metadata_rate_limit)
    assert (enforce_metadata_write_rate_limit in chain) is (expected_guard is WRITE), key
    if expected_guard is WRITE:
        assert chain.index(enforce_metadata_rate_limit) < chain.index(enforce_metadata_write_rate_limit)


def test_exactly_three_routes_validate_a_write():
    writers = {k for k, g in EXPECTED.items() if g is WRITE}
    assert writers == {
        ("PATCH", f"{P}/items/{{serverId}}/{{ratingKey}}"),
        ("POST", f"{P}/edits/{{editId}}/revert"),
        ("POST", f"{P}/items/{{serverId}}/{{ratingKey}}/lock"),
    }
    # every simulation is a distinct `/preview` route under the READ guard (never a query flag)
    assert all(EXPECTED[k] is READ for k in EXPECTED if k[1].endswith("/preview"))


def test_no_metadata_route_accepts_a_dry_run_query_parameter():
    for route in _routes().values():
        names = {p.name.lower() for p in route.dependant.query_params}
        assert not names & {"dryrun", "dry_run", "preview", "simulate"}, route.path
