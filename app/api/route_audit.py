"""Boot-time assertion that every `/api/*` route carries a known auth guard.

Context (AUDIT-P4-001 / CR-A04, `docs/audit/v1/40-architecture.md`): the app
mounts its routers via **three different conventions** — a guard injected at
`include_router(..., dependencies=[...])` (Pattern A: accounts/categories/
live/media/stream/sync/plex), a guard declared module-level on the router
itself (Pattern C: ai/api_keys/downloads/plex_downloads/enrichment), or a
guard attached per-endpoint (e.g. `tv_auth.router`'s `/approve`). There is no
structural guarantee that a *new* `/api/*` route carries one of these — a
forgotten `dependencies=` on a freshly-added Pattern-C router (the most
recently adopted convention) would ship an unauthenticated route in silence,
undetected by any test targeting a route that doesn't exist yet.

This module closes that gap with a single boot-time check: walk `app.routes`
and require every `/api/*` path, **unless explicitly allow-listed below**, to
carry at least one dependency whose callable is a recognised auth guard from
`app.api.deps`. Anything else raises `RuntimeError` — the app refuses to
start. This mirrors, at the route level, the same fail-closed posture the API
already has at the request level (house law piège 9/10).

Detection covers all three mounting conventions because FastAPI flattens
every `Depends(...)` supplied at include_router-time, router-definition-time,
and endpoint-decorator-time into `route.dependant.dependencies` (verified
empirically for all three shapes — see `tests/test_route_auth_assertion.py`).
We additionally walk nested sub-dependencies defensively, in case a guard is
ever required transitively through another dependency.

Not covered by this walk, by construction (no `/api` prefix at all, so they
never enter the loop below — see `docs/plans/2026-07-26-refacto-audit-v1-plan.md`
§S1.2):
  - `/admin*` (HTML UI, HTTP Basic Auth, guarded at the `include_router` mount
    site — see `main.py`'s "Routes / auth-per-router" block).
  - `/dav` (WebDAV relay, dedicated HTTP Basic Auth, `verify_dav_basic_auth`).
  - `/docs` / `/openapi.json` (re-exposed behind `verify_admin_basic_auth`,
    `main.py`).
  - `/metrics` — **S4.1 done** (AUDIT-P2-001/CR-S02, ADR 0004 §Décision 3):
    guarded by its own `verify_metrics_basic_auth` HTTP Basic Auth
    (`METRICS_USERNAME`/`METRICS_PASSWORD`, fail-closed if the password is
    empty, escape hatch `METRICS_PUBLIC=true` — see `app/api/deps.py`),
    wired at the `Instrumentator.expose(..., dependencies=[...])` call site
    in `main.py`, not at `include_router`. It never carries the `/api/`
    prefix in the first place, so it was never in scope for the route-walk
    below or `PUBLIC_API_ALLOWLIST` — this entry only records that the guard
    exists, since this module used to be the one place noting it as an open
    gap.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

# Recognised auth-guard callables (from app/api/deps.py). A route is
# considered guarded if ANY dependency in its chain (any nesting depth) is
# one of these. `verify_dav_basic_auth` is deliberately excluded: `/dav`
# never carries the `/api/` prefix, so it can't reach this set anyway, and
# listing it here would invite confusion about scope.
_AUTH_DEPENDENCY_NAMES: frozenset[str] = frozenset(
    {
        "verify_backend_secret",
        "verify_api_key",
        "verify_master_key",
        "verify_admin_basic_auth",
    }
)

# Explicit, reviewed allow-list of `/api/*` paths that are legitimately
# public. Each entry MUST be justified here — adding a path is a deliberate
# security decision, not something a future refactor should be able to widen
# by accident. Kept as a frozenset so `tests/test_route_auth_assertion.py`
# can assert it stays EXACTLY this shape (a growing allow-list is itself a
# signal worth a human looking at it).
PUBLIC_API_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Monitoring probe. No credential to present (used by uptime checks /
        # container healthchecks before any client is configured).
        "/api/health",
        # TV device-flow bootstrap (RFC 8628-like, `app/api/tv_auth.py`): the
        # TV has no key yet by design — it's the thing being paired. `/approve`
        # (the mobile/web side, which DOES already hold a key) is guarded by
        # `verify_backend_secret` (aliased `verify_pairing_api_key`) and is
        # therefore NOT in this allow-list — it's caught by the dependency
        # walk below like any other guarded route.
        "/api/tv-auth/start",
        "/api/tv-auth/status",
        "/api/tv-auth/complete",
    }
)


def _dependency_names(route: APIRoute) -> set[str]:
    """All dependency callable names attached to `route`, at any nesting depth."""
    names: set[str] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dependant = stack.pop()
        name = getattr(dependant.call, "__name__", None)
        if name:
            names.add(name)
        stack.extend(dependant.dependencies)
    return names


def assert_api_routes_authenticated(app: FastAPI) -> None:
    """Raise `RuntimeError` if any `/api/*` route lacks a recognised auth guard.

    Call once, after every `include_router(...)` call that can add a
    `/api/*` path (see `app/main.py`). Runs at import time of `app.main` —
    i.e. on every process boot, master or worker alike (piège 7): this check
    has nothing to do with the `fcntl` master election.
    """
    unguarded: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api/"):
            continue
        if route.path in PUBLIC_API_ALLOWLIST:
            continue
        if _dependency_names(route) & _AUTH_DEPENDENCY_NAMES:
            continue
        methods = ",".join(sorted(route.methods or ())) or "?"
        unguarded.append(f"{methods} {route.path}")

    if unguarded:
        raise RuntimeError(
            "Route auth audit failed (AUDIT-P4-001 / CR-A04): the following "
            "/api/* route(s) are reachable WITHOUT a recognised auth "
            "dependency (one of "
            f"{sorted(_AUTH_DEPENDENCY_NAMES)}): "
            + "; ".join(unguarded)
            + ". Either attach a guard (dependencies= at mount site, "
            "module-level on the router, or per-endpoint), or — only if the "
            "route is genuinely meant to be public — add its exact path to "
            "PUBLIC_API_ALLOWLIST in app/api/route_audit.py with a written "
            "justification."
        )
