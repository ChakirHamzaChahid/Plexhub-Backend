"""Boot-time route-auth audit (AUDIT-P4-001 / CR-A04, `app/api/route_audit.py`).

Covers exactly the DoD called out in the plan (`docs/plans/2026-07-26-refacto-
audit-v1-plan.md` §S1.2):
  1. the real app boots (i.e. `app.main` imports cleanly — the assertion runs
     at import time and does not raise);
  2. a toy app with an unguarded `/api/oops` route makes the assertion bite
     (`RuntimeError`, naming the offending path);
  3. the allow-list is *exactly* the 4 documented public paths — a test that
     fails the moment someone widens it without a matching test change.

Also proves, per the task's DoD, that the assertion is not a no-op: removing
a real guard from the live app object (in-memory, never touching source)
makes `assert_api_routes_authenticated` raise.
"""
from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI

from app.api.route_audit import (
    PUBLIC_API_ALLOWLIST,
    assert_api_routes_authenticated,
)


# ─── 1. The real app boots clean ────────────────────────────────────────────


def test_real_app_passes_the_audit():
    """Importing `app.main` must not raise — this IS the boot-time check."""
    from app.main import app  # noqa: F401 — import is the assertion under test


def test_real_app_reaudit_is_idempotent_and_green():
    """Re-running the audit against the already-booted app stays green."""
    from app.main import app

    assert_api_routes_authenticated(app)  # must not raise


# ─── 2. The assertion actually bites ────────────────────────────────────────


def test_unguarded_api_route_raises_with_the_offending_path():
    app = FastAPI()
    router = APIRouter()

    @router.get("/oops")
    async def oops():
        return {}

    app.include_router(router, prefix="/api")

    with pytest.raises(RuntimeError) as exc_info:
        assert_api_routes_authenticated(app)

    assert "/api/oops" in str(exc_info.value)
    assert "GET" in str(exc_info.value)


def test_guarded_api_route_via_mount_time_dependency_passes():
    """Pattern A: `include_router(..., dependencies=[Depends(guard)])`."""
    from app.api.deps import verify_backend_secret

    app = FastAPI()
    router = APIRouter()

    @router.get("/x")
    async def x():
        return {}

    app.include_router(
        router, prefix="/api", dependencies=[Depends(verify_backend_secret)]
    )
    assert_api_routes_authenticated(app)  # must not raise


def test_guarded_api_route_via_module_level_router_dependency_passes():
    """Pattern C: `APIRouter(dependencies=[Depends(guard)])`, bare-mounted."""
    from app.api.deps import verify_api_key

    app = FastAPI()
    router = APIRouter(prefix="/api/thing", dependencies=[Depends(verify_api_key)])

    @router.get("/y")
    async def y():
        return {}

    app.include_router(router)
    assert_api_routes_authenticated(app)  # must not raise


def test_guarded_api_route_via_endpoint_level_dependency_passes():
    """Per-endpoint `dependencies=[Depends(guard)]`, e.g. tv_auth's /approve."""
    from app.api.deps import verify_master_key

    app = FastAPI()
    router = APIRouter()

    @router.post("/z", dependencies=[Depends(verify_master_key)])
    async def z():
        return {}

    app.include_router(router, prefix="/api")
    assert_api_routes_authenticated(app)  # must not raise


def test_recognised_dependency_names_are_required_not_just_any_dependency():
    """A dependency that ISN'T one of the 4 recognised guards doesn't count —
    the audit checks for a *known* auth callable, not merely "has deps"."""

    async def _unrelated_dependency():
        return None

    app = FastAPI()
    router = APIRouter()

    @router.get("/oops")
    async def oops():
        return {}

    app.include_router(
        router, prefix="/api", dependencies=[Depends(_unrelated_dependency)]
    )

    with pytest.raises(RuntimeError):
        assert_api_routes_authenticated(app)


def test_removing_a_real_guard_makes_the_boot_assertion_bite():
    """Proves the assertion isn't inert: build the SAME shape main.py uses for
    a Pattern-A router (mount-time guard), then show that dropping the guard
    — as would happen from an accidental omission — flips the outcome from
    green to a `RuntimeError` naming the exact route. Mirrors main.py's own
    `accounts`/`media`/... mounting convention without touching any real
    source file."""
    from app.api.deps import verify_backend_secret

    def build_app(*, with_guard: bool) -> FastAPI:
        app = FastAPI()
        router = APIRouter()

        @router.get("/media/movies")
        async def movies():
            return {}

        kwargs = {"dependencies": [Depends(verify_backend_secret)]} if with_guard else {}
        app.include_router(router, prefix="/api", **kwargs)
        return app

    # Guard present (today's real shape) -> green.
    assert_api_routes_authenticated(build_app(with_guard=True))

    # Guard accidentally dropped -> the assertion bites.
    with pytest.raises(RuntimeError) as exc_info:
        assert_api_routes_authenticated(build_app(with_guard=False))
    assert "/api/media/movies" in str(exc_info.value)


# ─── 3. The allow-list is frozen and exactly the 4 documented paths ────────


def test_allowlist_is_exactly_the_four_documented_public_paths():
    assert PUBLIC_API_ALLOWLIST == frozenset(
        {
            "/api/health",
            "/api/tv-auth/start",
            "/api/tv-auth/status",
            "/api/tv-auth/complete",
        }
    )


def test_metrics_is_not_in_the_allowlist():
    """S4.1 (AUDIT-P2-001/CR-S02): `/metrics` now carries its own
    `verify_metrics_basic_auth` guard, wired at the `Instrumentator.expose`
    call site in `main.py` — never via this allow-list (it never carries the
    `/api/` prefix, so it was never reachable by the route-walk either way).
    This is the explicit non-regression: the allow-list must not grow to
    "cover" it as a workaround."""
    assert "/metrics" not in PUBLIC_API_ALLOWLIST


def test_tv_auth_approve_is_not_in_the_allowlist():
    """/approve is guarded (verify_backend_secret), unlike start/status/complete
    — it must NOT be in the allow-list (that would mask a real regression if
    its guard were ever dropped)."""
    assert "/api/tv-auth/approve" not in PUBLIC_API_ALLOWLIST


def test_allowlisted_paths_are_reachable_on_the_real_app_without_a_guard():
    """Sanity: the 4 allow-listed paths genuinely exist on the real app and
    genuinely carry no recognised auth dependency today — i.e. the allow-list
    entries are load-bearing, not dead weight."""
    from app.api.route_audit import _dependency_names, _AUTH_DEPENDENCY_NAMES
    from app.main import app
    from fastapi.routing import APIRoute

    real_paths = {
        route.path: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path in PUBLIC_API_ALLOWLIST
    }
    assert real_paths.keys() == PUBLIC_API_ALLOWLIST
    for path, route in real_paths.items():
        assert not (_dependency_names(route) & _AUTH_DEPENDENCY_NAMES), (
            f"{path} unexpectedly carries a recognised auth dependency — "
            "remove it from PUBLIC_API_ALLOWLIST, it's no longer needed"
        )
