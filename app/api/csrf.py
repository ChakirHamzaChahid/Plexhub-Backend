"""CSRF guard for the browser-facing `/admin*` UI (AUDIT-P2-005 / CR-S07, S4.3).

**Problem** (`docs/plans/2026-07-26-refacto-audit-v1-plan.md` §S4.3): the four
admin routers (`admin`, `admin_downloads`, `admin_plex_downloads`,
`admin_unified_downloads`) are guarded by HTTP Basic Auth only
(`verify_admin_basic_auth`, `app/api/deps.py`). A browser automatically
replays cached Basic Auth credentials on every request to the same origin,
including one triggered by a third-party page (an auto-submitting `<form
method=post>`, or `fetch()` with `credentials: "include"`-equivalent
behaviour for Basic Auth, which needs no explicit opt-in). There is no
synchronizer token and no session cookie to scope — this is classic CSRF
against the mutating POST endpoints (enqueue/cancel/retry downloads, Plex
catalogue sync, category refresh, API-key creation/revocation, NFO
rescrape/id-fix).

**Decision — `Sec-Fetch-Site` instead of a synchronizer token** (accepted
trade-off, spelled out here rather than glossed over):

`Sec-Fetch-Site` is a Fetch Metadata request header set by the BROWSER
itself, not overridable from page JavaScript (it is a "forbidden header
name") — a cross-site attacker page cannot spoof it to `same-origin`. When
present, its value is authoritative for the request's origin relationship:

  * ``same-origin`` / ``same-site`` / ``none`` (the last covers a user
    typing the URL, a bookmark, or a browser extension) -> **legitimate**,
    let the request through unconditionally.
  * ``cross-site`` -> **the exact CSRF shape this guard exists to stop**:
    reject with 403.

The header is **absent** for any client that isn't a Fetch-Metadata-capable
browser: `curl`, `httpx`, Python test clients, very old browsers (Safari
<12.1, pre-2020 Chromium/Firefox releases), and some browser extensions/dev
tools. This module's explicit choice is to **accept the request when the
header is absent** (fail-OPEN on this one signal), for two reasons:

  1. A non-browser client sending no cookie jar and no ambient Basic Auth
     credential is not the CSRF threat model in the first place — CSRF
     specifically exploits a browser's automatic credential replay. A
     script that wants to hit `/admin*` already has to supply
     `Authorization` explicitly; it was never relying on ambient auth.
  2. Rejecting on absence would break every non-browser caller of `/admin*`
     with NO recovery path (no token to mint, no origin to configure) —
     operator scripts, `tests/test_admin*.py`, and any future automation.
     That is a real, avoidable regression for a header no attacker
     controls anyway (an attacker's own cross-site *browser* request WILL
     carry `Sec-Fetch-Site: cross-site` — they cannot make their victim's
     browser omit it).

**Honest residual limit**: this is not full CSRF protection. A legitimate
browser old enough to omit Fetch Metadata (see above) is indistinguishable,
from this middleware's point of view, from a non-browser script — both pass
through unchecked. This guard closes the gap for every currently-shipping
browser (Fetch Metadata has been supported since ~2020 across all major
engines) but is not a cryptographic guarantee like a synchronizer token
would be. Documented, not hidden — see the plan §S4.3 and CLAUDE.md §9
piège 10.

**Scope**: only non-safe methods (`POST`/`PUT`/`PATCH`/`DELETE`) whose path
starts with `/admin` are checked. `GET`/`HEAD`/`OPTIONS` (navigation, HTMX
`hx-get` swaps, static asset loads) are never touched — this is
deliberately narrow so the HTMX-heavy admin UI (CLAUDE.md §5.10: HTMX
trigger bugs on this exact UI have bitten this project before) sees zero
behavioural change on read paths. HTMX's own `hx-post` calls are same-origin
`fetch()`s issued by the browser itself, so they carry
`Sec-Fetch-Site: same-origin` and pass unaffected (verified by
`tests/test_admin_csrf.py`, not just assumed).

The JSON mirrors (`/api/admin/downloads`, `/api/admin/plex-downloads`,
`/api/admin/enrichment`, `/api/admin/keys`) live under `/api`, not `/admin`,
so the `/admin` prefix check never matches them — and they are guarded by
`X-API-Key` (a header a browser never attaches ambiently), so they were
never in the CSRF threat model to begin with (see the ticket brief). No
change needed there, and this module doesn't touch them.
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("plexhub.csrf")

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_GUARDED_PATH_PREFIX = "/admin"
_BLOCKED_SEC_FETCH_SITE = "cross-site"


class AdminCsrfMiddleware(BaseHTTPMiddleware):
    """Rejects cross-site mutations against the browser-facing `/admin*` UI.

    See this module's docstring for the full decision record (why
    `Sec-Fetch-Site`, why an absent header is accepted, and the residual
    limit that leaves).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if (
            request.method in _UNSAFE_METHODS
            and request.url.path.startswith(_GUARDED_PATH_PREFIX)
        ):
            sec_fetch_site = request.headers.get("sec-fetch-site")
            if sec_fetch_site == _BLOCKED_SEC_FETCH_SITE:
                logger.warning(
                    "csrf: rejected cross-site %s %s (Sec-Fetch-Site: cross-site)",
                    request.method,
                    request.url.path,
                )
                return JSONResponse(
                    {"detail": "Cross-site request rejected"},
                    status_code=403,
                )
        return await call_next(request)


__all__ = ["AdminCsrfMiddleware"]
