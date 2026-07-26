"""Process-local, TARGETED rate limiting (AUDIT-P2-004 / CR-S05, S4.2).

Scope decision — already made, not re-litigated here (docs/plans/
2026-07-26-refacto-audit-v1-plan.md §S4.2): a GLOBAL rate limiter was
REJECTED. PlexHubTV polls several authenticated endpoints on a tight,
legitimate schedule (the tv-auth device-flow's ``GET /tv-auth/status`` in
particular — the TV polls it in a loop while waiting to be approved, see
``app/api/tv_auth.py``'s module docstring) and a blanket per-IP/per-key
limiter risks throttling that traffic. Generic brute-force protection on
the X-API-Key / HTTP Basic Auth surfaces (``/admin``, ``/dav``, ``/metrics``)
is delegated to the WAF/ingress (Cloudflare) — the SAME doctrine already
documented for ``/dav`` (CLAUDE.md §9 piège 18b: "l'exclusion ingress reste
le prérequis"). This module does not attempt that; nothing here is wired
into any router other than ``POST /api/tv-auth/start``.

Why that one endpoint: it is BOTH unauthenticated by design (the TV has no
key yet) AND writes a database row on every call, so an anonymous flood is
a direct DB-write-lock / unbounded-table-growth vector. A request-RATE
limit alone does not fully close that: a patient attacker who paces
requests just under any per-minute cap can still grow the table without
bound given enough time. Two independent guards are wired from
``app/api/tv_auth.py::start()``:

  1. ``SlidingWindowLimiter`` — a per-client-key sliding-window REQUEST-RATE
     cap. Used via TWO SEPARATE instances (see the singletons at the
     bottom of this module) for two different purposes/windows — sharing
     one instance across both would be wrong: purging a key's deque for a
     short window would evict timestamps the long-window check still needs
     (and vice versa).
       - a short window (``TV_AUTH_START_RATE_LIMIT_MAX`` per
         ``TV_AUTH_START_RATE_LIMIT_WINDOW_SECONDS``, default 5 / 60s) —
         slows down a naive flood.
       - a long window matched to the pairing session TTL
         (``TV_AUTH_PENDING_SESSIONS_CAP_PER_IP`` per
         ``TV_AUTH_TTL_SECONDS``) — an IN-PROCESS APPROXIMATION of "how
         many sessions has this apparent client started that haven't
         expired yet". It deliberately OVER-counts (a session that was
         approved+completed early is still counted here until the window
         elapses) — a conservative, safe bias, never an under-count.

  2. ``check_pending_cap()`` — the actual bound on table growth: a
     DB-authoritative ``COUNT(*) WHERE status='pending'`` (computed by the
     caller, ``app/api/tv_auth.py``) compared against
     ``TV_AUTH_PENDING_SESSIONS_CAP`` — GLOBAL, not per-IP, and correct
     even across multiple processes sharing one database (unlike the two
     in-memory approximations above). This is the guard the audit finding
     actually asks for ("la vraie protection contre le flood DB"); the
     per-IP limiters are a courtesy that slows the common case down before
     it ever reaches this ceiling.

HONESTY about client identification (explicitly required, not glossed
over): client identity is resolved by ``resolve_client_ip()`` below, which
mirrors ``app.api.deps._client_ip``'s header precedence (``CF-Connecting-
IP``, then ``X-Forwarded-For``, then the ASGI transport's
``request.client.host``) — duplicated rather than imported, see that
function's docstring for why. Behind Cloudflare, in this deployment's
normal topology, that header IS the real client IP. But NEITHER this
function NOR ``deps._client_ip`` verifies that a request actually arrived
through Cloudflare's edge: if this process is ever reachable directly
(bypassing the tunnel — e.g. a misconfigured firewall, or a LAN client),
an attacker can set ``CF-Connecting-IP``/``X-Forwarded-For`` to an
arbitrary value on EVERY request and appear as a brand-new "IP" each time,
which defeats BOTH ``SlidingWindowLimiter`` instances above completely.
That is exactly why guard #2 (the global, DB-authoritative pending cap)
does NOT depend on IP identification at all — it is the one guard here
that holds even against a fully spoofed client identity. ``_evict_if_full``
below additionally bounds this module's OWN memory use in that scenario
(an attacker minting unbounded distinct "IPs" would otherwise grow the
in-memory dict without bound too).

Process-local (module-level singletons, in-memory, single asyncio event
loop — no lock needed for the dict mutations here): this repo's
``Dockerfile`` runs a single uvicorn worker (the same constraint already
documented by ``app/dav/throttle.py``) — none of the in-memory state in
this module is shared across multiple processes/replicas. The
DB-authoritative pending cap (guard #2) is the only one of the three
guards described above that would remain correct in a hypothetical
multi-process deployment.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from fastapi import Request

logger = logging.getLogger("plexhub.rate_limit")

# Bounds this module's own memory even under IP-spoofing (see module
# docstring) — an attacker minting a fresh apparent "IP" per request would
# otherwise grow `_hits` without bound. Generously sized: legitimate
# deployments see, at most, a handful of distinct client IPs hitting
# tv-auth/start.
_MAX_TRACKED_KEYS = 5000


class RateLimitExceeded(Exception):
    """Raised by `SlidingWindowLimiter.hit()` when a key is over budget.

    `retry_after_seconds` is the caller's suggested `Retry-After` value
    (always >= 1, rounded up by the HTTP-facing caller).
    """

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(retry_after_seconds, 0.1)
        super().__init__(
            f"rate limit exceeded, retry after {self.retry_after_seconds:.1f}s"
        )


class PendingCapExceeded(Exception):
    """The DB-authoritative `pending` tv-auth session count is at or above
    the configured cap.

    `retry_after_seconds` is a heuristic (sessions free up as they expire
    or get approved+completed; there is no cheap way to compute the exact
    next-free instant without an extra query per rejected request) — it is
    NOT derived from any single row's real expiry.
    """

    def __init__(self, current: int, cap: int, retry_after_seconds: float) -> None:
        self.current = current
        self.cap = cap
        self.retry_after_seconds = max(retry_after_seconds, 0.1)
        super().__init__(f"pending session cap reached ({current}/{cap})")


class SlidingWindowLimiter:
    """Per-key sliding-window event counter, process-local (in-memory).

    Single-asyncio-event-loop assumption: `hit()` never awaits, so a call
    runs to completion without yielding control — no lock is needed to keep
    a single key's deque consistent across concurrent requests.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def hit(
        self,
        key: str,
        *,
        max_events: int,
        window_seconds: float,
        now: float | None = None,
    ) -> None:
        """Record one event for `key`.

        Raises `RateLimitExceeded` if this would exceed `max_events` within
        the trailing `window_seconds`. A REJECTED call is NOT recorded (it
        doesn't count against the caller's own next attempt).

        `max_events <= 0` disables the limiter entirely for this call
        (never records, never raises) — an explicit, documented opt-out,
        mirroring `check_pending_cap`'s own `cap <= 0` convention below.
        """
        if max_events <= 0:
            return
        now = time.monotonic() if now is None else now
        window_start = now - window_seconds

        events = self._hits.get(key)
        if events is None:
            self._evict_if_full()
            events = deque()
            self._hits[key] = events

        # An event exactly `window_seconds` old (age == window_seconds) has
        # just left the trailing window — hence `<=`, not `<`: otherwise an
        # event at t=0 with window=10 would still count at now=10 (age 10),
        # one tick later than the window's own definition allows.
        while events and events[0] <= window_start:
            events.popleft()

        if len(events) >= max_events:
            retry_after = events[0] + window_seconds - now
            raise RateLimitExceeded(retry_after)

        events.append(now)

    def _evict_if_full(self) -> None:
        """Defensive bound on dict size — see module docstring re: an
        IP-spoofing attacker minting unbounded distinct keys. Drops the
        oldest-INSERTED key (Python dicts preserve insertion order); this is
        a coarse, best-effort bound, not an LRU — acceptable because it only
        ever engages under sustained abuse from many distinct apparent
        clients, a scenario this module is explicit about not being able to
        fully defend against by IP alone (guard #2, the DB-authoritative
        pending cap, is what actually holds in that case).
        """
        if len(self._hits) >= _MAX_TRACKED_KEYS:
            oldest_key = next(iter(self._hits))
            del self._hits[oldest_key]
            logger.warning(
                "rate_limit: tracked-key cap (%d) reached, evicting oldest "
                "entry — possible client-identity spoofing (see module "
                "docstring)",
                _MAX_TRACKED_KEYS,
            )

    def reset(self) -> None:
        """Test-only: clear all recorded state (also used by test fixtures
        to prevent cross-test state leakage — this is a module-level
        singleton, so without an explicit reset, one test's hits would
        count against a later test using the same apparent client IP)."""
        self._hits.clear()


def resolve_client_ip(request: Request) -> str | None:
    """Best-effort client IP behind the Cloudflare tunnel.

    Deliberately DUPLICATES `app.api.deps._client_ip`'s header precedence
    rather than importing it: that helper is private to `deps`, and
    reaching into it from an unrelated module would just relocate the
    existing reach-in debt (CR-A06/A07) instead of avoiding it. See this
    module's docstring for the honesty caveat about header spoofing when
    this process is reachable outside the Cloudflare tunnel.
    """
    xff = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def check_pending_cap(
    current_count: int, cap: int, *, retry_after_seconds: float
) -> None:
    """Pure guard: raise `PendingCapExceeded` if `current_count >= cap`.

    `cap <= 0` disables the check (explicit opt-out, mirrors
    `SlidingWindowLimiter.hit`'s own `max_events <= 0` convention).
    """
    if cap <= 0:
        return
    if current_count >= cap:
        raise PendingCapExceeded(current_count, cap, retry_after_seconds)


# Module-level singletons — one process, one instance per concern (see
# module docstring for why two SEPARATE instances rather than one shared
# instance called with two different windows). Consumed exclusively by
# `app/api/tv_auth.py::start()`.
tv_auth_start_rate_limiter = SlidingWindowLimiter()
tv_auth_start_pending_ip_limiter = SlidingWindowLimiter()
