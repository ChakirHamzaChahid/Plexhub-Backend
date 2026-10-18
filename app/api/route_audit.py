"""Boot-time assertion that every `/api/*` route carries a known auth guard.

Context (AUDIT-P4-001 / CR-A04, `docs/audit/v1/40-architecture.md`): the app
mounts its routers via **three different conventions** — a guard injected at
`include_router(..., dependencies=[...])` (Pattern A: accounts/categories/
live/media/stream/sync/plex), a guard declared module-level on the router
itself (Pattern C: ai/api_keys/downloads/plex_downloads/enrichment), or a
guard attached per-endpoint (e.g. `tv_auth.router`'s `POST /approve`). There is no
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

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api import deps

# Recognised auth guards (from app/api/deps.py), matched by FUNCTION IDENTITY,
# not by name: a local function that merely happens to be called
# `verify_backend_secret` must not satisfy the audit. A route is considered
# guarded if ANY dependency in its chain (any nesting depth) is one of these.
# Aliases (`tv_auth.verify_pairing_api_key`) are the same function object and
# are therefore accepted. `verify_dav_basic_auth` is deliberately excluded:
# `/dav` never carries the `/api/` prefix, so it can't reach this set anyway.
_GENERAL_GUARDS: frozenset[Callable[..., Any]] = frozenset(
    {
        deps.verify_backend_secret,
        deps.verify_api_key,
        deps.verify_master_key,
        deps.verify_admin_basic_auth,
    }
)

# Scrape gateway scope guards (ADR 0008 § D2.4, lot B1). They are ONLY
# accepted on routes under SCRAPE_GATEWAY_PREFIX: elsewhere they would let an
# unscoped key through, so the route counts as unguarded (boot refused).
# route_audit does not tell the two apart: a structural test checks the
# expected guard per gateway route (B1-11).
_SCOPE_GUARDS: frozenset[Callable[..., Any]] = frozenset(
    {
        deps.verify_scrape_scope,
        deps.verify_scrape_admin_scope,
    }
)
SCRAPE_GATEWAY_PREFIX = "/api/scrape/v1/"

# Names of all recognised guards (error message, tests); NOT used for matching.
_AUTH_DEPENDENCY_NAMES: frozenset[str] = frozenset(
    fn.__name__ for fn in _GENERAL_GUARDS | _SCOPE_GUARDS
)

# Explicit, reviewed allow-list of `/api/*` (METHOD, path) pairs that are
# legitimately public. Each entry MUST be justified here — adding one is a
# deliberate security decision, not something a future refactor should be able
# to widen by accident. Kept as a frozenset so `tests/test_route_auth_assertion.py`
# can assert it stays EXACTLY this shape (a growing allow-list is itself a
# signal worth a human looking at it).
#
# Lot 10 sous-lot B — the allow-list reasons on (METHOD, path), NOT on the path
# alone. `GET /api/tv-auth/approve` (the public pairing HTML page) shares its
# path with `POST /api/tv-auth/approve` (the guarded approval endpoint); with a
# path-only list, allow-listing the page would have silently made the POST
# public too (and a later loss of the POST's guard would have gone undetected).
# A route is skipped only if EVERY one of its methods is allow-listed for its
# path.
PUBLIC_API_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # Monitoring probe. No credential to present (used by uptime checks /
        # container healthchecks before any client is configured).
        ("GET", "/api/health"),
        # TV device-flow bootstrap (RFC 8628-like, `app/api/tv_auth.py`): the
        # TV has no key yet by design — it's the thing being paired.
        ("POST", "/api/tv-auth/start"),
        ("GET", "/api/tv-auth/status"),
        ("POST", "/api/tv-auth/complete"),
        # Public pairing page (static HTML served to a phone that scanned the
        # TV's QR code): carries no session data and performs no DB lookup;
        # the secret that authorises an approval is typed into the page and
        # sent by the page's `fetch` to the POST below. ONLY the GET is public:
        # `POST /api/tv-auth/approve` is guarded by `verify_backend_secret`
        # (aliased `verify_pairing_api_key`), is deliberately NOT in this list,
        # and is caught by the dependency walk like any other guarded route.
        ("GET", "/api/tv-auth/approve"),
    }
)


def _dependency_calls(route: APIRoute) -> set[Callable[..., Any]]:
    """All dependency callables attached to `route`, at any nesting depth."""
    calls: set[Callable[..., Any]] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            calls.add(dependant.call)
        stack.extend(dependant.dependencies)
    return calls


def _dependency_names(route: APIRoute) -> set[str]:
    """Names of all dependency callables of `route` (diagnostics/tests only —
    the audit itself matches by identity, see `_is_guarded`)."""
    return {
        name
        for call in _dependency_calls(route)
        if (name := getattr(call, "__name__", None))
    }


def _is_guarded(route: APIRoute) -> bool:
    """True if the route carries a recognised guard (by identity). Scope guards
    only count under SCRAPE_GATEWAY_PREFIX."""
    calls = _dependency_calls(route)
    if calls & _GENERAL_GUARDS:
        return True
    return route.path.startswith(SCRAPE_GATEWAY_PREFIX) and bool(calls & _SCOPE_GUARDS)


def _is_allow_listed(route: APIRoute) -> bool:
    """True only if EVERY method of `route` is allow-listed for its exact path."""
    methods = route.methods or set()
    return bool(methods) and all((m, route.path) in PUBLIC_API_ALLOWLIST for m in methods)


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
        if _is_allow_listed(route):
            continue
        if _is_guarded(route):
            continue
        methods = ",".join(sorted(route.methods or ())) or "?"
        unguarded.append(f"{methods} {route.path}")

    if unguarded:
        raise RuntimeError(
            "Route auth audit failed (AUDIT-P4-001 / CR-A04): the following "
            "/api/* route(s) are reachable WITHOUT a recognised auth "
            "dependency (one of "
            f"{sorted(_AUTH_DEPENDENCY_NAMES)}; the scope guards only count "
            f"under {SCRAPE_GATEWAY_PREFIX}): "
            + "; ".join(unguarded)
            + ". Either attach a guard (dependencies= at mount site, "
            "module-level on the router, or per-endpoint), or — only if the "
            "route is genuinely meant to be public — add its exact (METHOD, path) to "
            "PUBLIC_API_ALLOWLIST in app/api/route_audit.py with a written "
            "justification."
        )
