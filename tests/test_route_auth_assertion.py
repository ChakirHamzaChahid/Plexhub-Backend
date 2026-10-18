"""Boot-time route-auth audit (AUDIT-P4-001 / CR-A04, `app/api/route_audit.py`).

Covers exactly the DoD called out in the plan (`docs/plans/2026-07-26-refacto-
audit-v1-plan.md` §S1.2):
  1. the real app boots (i.e. `app.main` imports cleanly — the assertion runs
     at import time and does not raise);
  2. a toy app with an unguarded `/api/oops` route makes the assertion bite
     (`RuntimeError`, naming the offending path);
  3. the allow-list is *exactly* the 5 documented public (METHOD, path) pairs —
     a test that fails the moment someone widens it without a matching test
     change. (Lot 10 sous-lot B: it reasons on (METHOD, path), so the public
     `GET /api/tv-auth/approve` page can never make the guarded POST public.)

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
    """A dependency that ISN'T one of the 6 recognised guards doesn't count —
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


def test_recognised_guard_names_are_exactly_the_six_known_guards():
    """Fige l'ensemble EXACT des gardes reconnus (ADR 0008 § D2.4, B1-03) :
    l'élargir sans toucher ce test doit échouer."""
    from app.api.route_audit import _AUTH_DEPENDENCY_NAMES

    assert _AUTH_DEPENDENCY_NAMES == frozenset(
        {
            "verify_backend_secret",
            "verify_api_key",
            "verify_master_key",
            "verify_admin_basic_auth",
            "verify_scrape_scope",
            "verify_scrape_admin_scope",
        }
    )


def test_every_recognised_guard_name_is_a_real_function_of_deps():
    from app.api import deps
    from app.api.route_audit import _AUTH_DEPENDENCY_NAMES

    for name in _AUTH_DEPENDENCY_NAMES:
        fn = getattr(deps, name)
        assert fn.__name__ == name


@pytest.mark.parametrize("guard_name", ["verify_scrape_scope", "verify_scrape_admin_scope"])
def test_scope_guards_satisfy_the_audit_per_route(guard_name):
    """Les deux gardes de la passerelle, posés PAR ROUTE (sans dépendance de
    router), satisfont l'audit ; retirer le garde le fait mordre."""
    from app.api import deps

    guard = getattr(deps, guard_name)

    guarded = FastAPI()
    router = APIRouter(prefix="/api/scrape-test")

    @router.get("/x", dependencies=[Depends(guard)])
    async def x():
        return {}

    guarded.include_router(router)
    assert_api_routes_authenticated(guarded)  # must not raise

    bare = FastAPI()
    bare_router = APIRouter(prefix="/api/scrape-test")

    @bare_router.get("/x")
    async def y():
        return {}

    bare.include_router(bare_router)
    with pytest.raises(RuntimeError):
        assert_api_routes_authenticated(bare)


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


# ─── 3. The allow-list is frozen and exactly the 5 documented (method, path) ──


def test_allowlist_is_exactly_the_five_documented_public_routes():
    assert PUBLIC_API_ALLOWLIST == frozenset(
        {
            ("GET", "/api/health"),
            ("POST", "/api/tv-auth/start"),
            ("GET", "/api/tv-auth/status"),
            ("POST", "/api/tv-auth/complete"),
            ("GET", "/api/tv-auth/approve"),
        }
    )


def test_metrics_is_not_in_the_allowlist():
    """S4.1 (AUDIT-P2-001/CR-S02): `/metrics` now carries its own
    `verify_metrics_basic_auth` guard, wired at the `Instrumentator.expose`
    call site in `main.py` — never via this allow-list (it never carries the
    `/api/` prefix, so it was never reachable by the route-walk either way).
    This is the explicit non-regression: the allow-list must not grow to
    "cover" it as a workaround."""
    assert not any(path == "/metrics" for _method, path in PUBLIC_API_ALLOWLIST)


def test_tv_auth_post_approve_is_not_public():
    """`POST /api/tv-auth/approve` is guarded (verify_backend_secret) — it must
    NOT be in the allow-list, whatever the method spelling: only the GET page
    shares that path (Lot 10 sous-lot B). Listing the POST would mask a real
    regression if its guard were ever dropped."""
    assert ("POST", "/api/tv-auth/approve") not in PUBLIC_API_ALLOWLIST
    approve_entries = {m for m, path in PUBLIC_API_ALLOWLIST if path == "/api/tv-auth/approve"}
    assert approve_entries == {"GET"}


def test_allowlisted_routes_are_reachable_on_the_real_app_without_a_guard():
    """Sanity: the 5 allow-listed (method, path) pairs genuinely exist on the
    real app and genuinely carry no recognised auth dependency today — i.e. the
    allow-list entries are load-bearing, not dead weight."""
    from app.api.route_audit import _dependency_names, _AUTH_DEPENDENCY_NAMES
    from app.main import app
    from fastapi.routing import APIRoute

    real = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            if (method, route.path) in PUBLIC_API_ALLOWLIST:
                real[(method, route.path)] = route
    assert real.keys() == PUBLIC_API_ALLOWLIST
    for key, route in real.items():
        assert not (_dependency_names(route) & _AUTH_DEPENDENCY_NAMES), (
            f"{key} unexpectedly carries a recognised auth dependency — "
            "remove it from PUBLIC_API_ALLOWLIST, it's no longer needed"
        )


# ─── 4. (METHOD, path) reasoning — the public GET never makes the POST public ─


def _toy_app_same_path(*, post_guarded: bool) -> FastAPI:
    """Mirror of tv_auth's shape: a public GET and a POST on the SAME path."""
    from app.api.deps import verify_backend_secret

    app = FastAPI()
    router = APIRouter()

    @router.get("/tv-auth/approve")
    async def page():
        return {}

    deps = [Depends(verify_backend_secret)] if post_guarded else []

    @router.post("/tv-auth/approve", dependencies=deps)
    async def approve():
        return {}

    app.include_router(router, prefix="/api")
    return app


def test_public_get_does_not_whitelist_an_unguarded_post_on_the_same_path():
    """The invariant of the (METHOD, path) design: with a path-only allow-list
    the unguarded POST below would have slipped through."""
    assert_api_routes_authenticated(_toy_app_same_path(post_guarded=True))
    with pytest.raises(RuntimeError) as exc_info:
        assert_api_routes_authenticated(_toy_app_same_path(post_guarded=False))
    assert "POST /api/tv-auth/approve" in str(exc_info.value)
    assert "GET /api/tv-auth/approve" not in str(exc_info.value)


def test_a_single_route_is_skipped_only_if_every_method_is_allowlisted():
    app = FastAPI()
    router = APIRouter()

    @router.api_route("/tv-auth/approve", methods=["GET", "POST"])
    async def both():
        return {}

    app.include_router(router, prefix="/api")
    with pytest.raises(RuntimeError):
        assert_api_routes_authenticated(app)


def test_real_tv_auth_router_raises_if_post_approve_loses_its_guard():
    """Take the REAL tv_auth router, mount it on a throwaway app: green. Then
    strip the guard from the POST /approve route of that throwaway app (as an
    accidental removal of `dependencies=` would): the audit must bite and name
    the POST — while the public GET page stays accepted."""
    from app.api import tv_auth
    from fastapi.routing import APIRoute

    app = FastAPI()
    app.include_router(tv_auth.router, prefix="/api")
    assert_api_routes_authenticated(app)  # green with the guard

    post = next(
        r for r in app.routes
        if isinstance(r, APIRoute) and r.path == "/api/tv-auth/approve" and "POST" in r.methods
    )
    post.dependant.dependencies = [
        d for d in post.dependant.dependencies
        if getattr(d.call, "__name__", "") != "verify_backend_secret"
    ]
    with pytest.raises(RuntimeError) as exc_info:
        assert_api_routes_authenticated(app)
    assert "POST /api/tv-auth/approve" in str(exc_info.value)
    assert "GET /api/tv-auth/approve" not in str(exc_info.value)
