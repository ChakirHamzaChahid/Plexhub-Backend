"""Device-flow TV pairing API (Mission 18).

Reproduces the ARVIO Supabase Edge Functions pattern (tv-auth-start/approve/
complete/status) as native FastAPI endpoints — no Supabase.

Flow (RFC 8628-like):
    1. POST /api/tv-auth/start    TV asks for a session. Gets a long secret
                                  `deviceCode` (poll credential) + a short
                                  `userCode` (displayed on screen + QR).
    2. GET  /api/tv-auth/approve  PUBLIC HTML page (the QR code target): a
                                  phone opens it, the user types the backend
                                  key and picks the payload to hand over.
       POST /api/tv-auth/approve  Mobile/web (authenticated via X-API-Key)
                                  validates the userCode and attaches the
                                  config payload (encrypted at rest). The
                                  page above calls it with `fetch`.
    3. GET  /api/tv-auth/status   TV polls by deviceCode (backoff-friendly).
                                  The decrypted payload is delivered EXACTLY
                                  once, on the first poll after approval.
    4. POST /api/tv-auth/complete TV acknowledges -> session completed
                                  (one-shot), encrypted payload scrubbed.

Security model:
    - Sessions expire after settings.TV_AUTH_TTL_SECONDS (default 15 min).
    - deviceCode is a 32-byte urlsafe token — unguessable.
    - POST approve requires an X-API-Key (constant-time): the backend shared
      secret OR any active per-user key (same guard as the JSON API,
      `verify_backend_secret`). The GET page is public by construction — no
      session data, no DB lookup — rate-limited per IP and served with a strict
      nonce-based CSP (see `approve_page`). The route audit
      (`app/api/route_audit.py`) reasons on (METHOD, path) so the public GET
      can never make the guarded POST public.
    - Payload is Fernet-encrypted at rest (app/utils/payload_crypto.py) and
      never re-delivered after the first read.
"""
from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_backend_secret as verify_pairing_api_key
from app.config import settings
from app.db.database import get_db
from app.models.database import TvAuthSession
from app.services import tv_auth_service
from app.utils import rate_limit
from app.utils.db_retry import session_factory_for, write_with_retry
from app.utils.payload_crypto import (
    PayloadDecryptError,
    decrypt_payload,
    get_fernet,
)

logger = logging.getLogger("plexhub.tvauth")

router = APIRouter(prefix="/tv-auth", tags=["tv-auth"])

# Pairing page templates (Jinja2 autoescape ON — never use |safe / Markup there).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Unambiguous alphabet for the human code (no 0/O, 1/I/L).
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_USER_CODE_LENGTH = 8
# Format accepted for the `?userCode=` prefill of the pairing page (display
# form with optional hyphen, case-insensitive input is upper-cased first).
_USER_CODE_PAGE_RE = re.compile(rf"^[{_USER_CODE_ALPHABET}]{{4}}-?[{_USER_CODE_ALPHABET}]{{4}}$")
_POLL_INTERVAL_SECONDS = 5  # suggested base interval for client backoff
_CLEANUP_GRACE_MS = 60 * 60 * 1000  # purge sessions expired > 1h ago

# AUDIT-P2-004 (S4.2): heuristic Retry-After for a rejected /start due to the
# GLOBAL pending-session cap (app.utils.rate_limit.check_pending_cap) — there
# is no cheap way to compute the exact next-free instant (that would need an
# extra query per rejection), so this is a fixed, conservative suggestion,
# not derived from any single row's real expiry.
_PENDING_CAP_RETRY_AFTER_SECONDS = 30

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"


# Auth dependency for /tv-auth/approve — the backend shared secret, constant-time.
# Now sourced from app.api.deps.verify_backend_secret (imported above as
# verify_pairing_api_key) so the JSON API and pairing share one implementation.


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas (camelCase aliases, same convention as app/api/ai.py)
# ──────────────────────────────────────────────────────────────────────────────

_CAMEL_CONFIG = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class StartRequest(BaseModel):
    model_config = _CAMEL_CONFIG

    device_name: str | None = Field(default=None, max_length=120)


class StartResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    device_code: str
    user_code: str  # formatted "ABCD-EFGH" for on-screen display
    verification_uri: str
    expires_in: int  # seconds
    interval: int  # suggested poll base interval, seconds


class ApproveRequest(BaseModel):
    model_config = _CAMEL_CONFIG

    user_code: str = Field(min_length=4, max_length=16)
    payload: dict  # config to deliver (e.g. Plex token) — must be non-empty


class ApproveResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    status: str


class CompleteRequest(BaseModel):
    model_config = _CAMEL_CONFIG

    device_code: str = Field(min_length=16, max_length=128)


class CompleteResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    status: str


class StatusResponse(BaseModel):
    model_config = _CAMEL_CONFIG

    status: str
    expires_in: int | None = None  # seconds left, None once terminal
    payload: dict | None = None  # delivered exactly once after approval


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    return tv_auth_service.now_ms()


def _generate_user_code() -> str:
    return tv_auth_service.generate_user_code()


def _format_user_code(code: str) -> str:
    return tv_auth_service.format_user_code(code)


def _normalize_user_code(code: str) -> str:
    return tv_auth_service.normalize_user_code(code)


def _require_crypto_configured() -> None:
    if get_fernet() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TV pairing not configured",
        )


def _too_many_requests(retry_after_seconds: float, detail: str) -> HTTPException:
    retry_after = max(1, round(retry_after_seconds))
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
    )


async def _expire_if_needed(db: AsyncSession, session: TvAuthSession) -> bool:
    return await tv_auth_service.expire_if_needed(
        db, session, write_fn=write_with_retry
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/tv-auth/start — the TV asks for a device code
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/start",
    response_model=StartResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def start(
    request: Request,
    payload: StartRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> StartResponse:
    """Create a pending pairing session. Called by the (unauthenticated) TV.

    AUDIT-P2-004 (S4.2, CR-S05): this is the ONE unauthenticated endpoint
    that writes a database row on every call, so it is rate-limited + capped
    — see app/utils/rate_limit.py's module docstring for the full doctrine
    (targeted, not a global limiter; the WAF/ingress still owns generic
    brute-force protection on every other surface).
    """
    _require_crypto_configured()

    client_key = rate_limit.resolve_client_ip(request) or "unknown"
    try:
        # Short-window request-rate cap — slows down a naive flood.
        rate_limit.tv_auth_start_rate_limiter.hit(
            client_key,
            max_events=settings.TV_AUTH_START_RATE_LIMIT_MAX,
            window_seconds=settings.TV_AUTH_START_RATE_LIMIT_WINDOW_SECONDS,
        )
        # Long-window (~session TTL) approximation of "sessions this
        # apparent client hasn't let expire yet" — a courtesy layer; the
        # DB-authoritative global cap below is the guard that actually
        # bounds table growth regardless of IP identification.
        rate_limit.tv_auth_start_pending_ip_limiter.hit(
            client_key,
            max_events=settings.TV_AUTH_PENDING_SESSIONS_CAP_PER_IP,
            window_seconds=settings.TV_AUTH_TTL_SECONDS,
        )
    except rate_limit.RateLimitExceeded as exc:
        raise _too_many_requests(
            exc.retry_after_seconds, "Too many pairing requests, slow down"
        ) from None

    device_name = payload.device_name if payload else None

    try:
        session = await tv_auth_service.create_pairing_session(
            device_name=device_name,
            write_fn=write_with_retry,
        )
    except tv_auth_service.UserCodeExhaustedError:
        session = None
    except rate_limit.PendingCapExceeded as exc:
        raise _too_many_requests(
            exc.retry_after_seconds,
            "Too many pending pairing sessions, try again later",
        ) from None
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not allocate a pairing code, retry",
        )

    logger.info(
        "tv-auth session %s created (device=%s, expires in %ss)",
        session.id, device_name or "?", settings.TV_AUTH_TTL_SECONDS,
    )
    return StartResponse(
        device_code=session.device_code,
        user_code=_format_user_code(session.user_code),
        verification_uri=f"{request.base_url}api/tv-auth/approve",
        expires_in=settings.TV_AUTH_TTL_SECONDS,
        interval=_POLL_INTERVAL_SECONDS,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/tv-auth/approve — PUBLIC pairing page (Lot 10 sous-lot B, FR-BE-075)
# ──────────────────────────────────────────────────────────────────────────────

def _prefill_user_code(raw: str | None) -> str:
    """Return the display form 'ABCD-EFGH' of `raw`, or '' if it is not a valid code.

    The page NEVER echoes the raw query value: it is only shown when it matches
    the user-code format, and what is shown is rebuilt from the alphabet.
    """
    if not raw:
        return ""
    candidate = raw.strip().upper()
    if not _USER_CODE_PAGE_RE.fullmatch(candidate):
        return ""
    return _format_user_code(_normalize_user_code(candidate))


def _approve_page_headers(nonce: str) -> dict[str, str]:
    """Security headers of the pairing page (no global middleware exists)."""
    return {
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}'; "
            "connect-src 'self'; "
            "form-action 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        ),
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


@router.get("/approve", response_class=HTMLResponse, include_in_schema=False)
async def approve_page(request: Request) -> HTMLResponse:
    """Public HTML page opened from the TV's QR code (`<verificationUri>?userCode=...`).

    PUBLIC by construction: it renders a static template and does no DB lookup
    and no crypto — it never depends on `get_db` nor on the Fernet key, and it
    contains no session data. The secret that authorises an approval is typed
    by the user and sent by the page's `fetch` as `X-API-Key` to the guarded
    `POST /approve` below (same path, different method; the route audit reasons
    on (METHOD, path)). Per-IP rate limit (TV_AUTH_APPROVE_PAGE_RATE_LIMIT_*),
    429 + Retry-After when exceeded; strict nonce-based CSP.
    """
    client_key = rate_limit.resolve_client_ip(request) or "unknown"
    try:
        rate_limit.tv_auth_approve_page_limiter.hit(
            client_key,
            max_events=settings.TV_AUTH_APPROVE_PAGE_RATE_LIMIT_MAX,
            window_seconds=settings.TV_AUTH_APPROVE_PAGE_RATE_LIMIT_WINDOW_SECONDS,
        )
    except rate_limit.RateLimitExceeded as exc:
        raise _too_many_requests(
            exc.retry_after_seconds, "Too many requests, slow down"
        ) from None

    nonce = secrets.token_urlsafe(16)
    return _templates.TemplateResponse(
        request,
        "pairing/approve.html",
        {
            "nonce": nonce,
            "user_code": _prefill_user_code(request.query_params.get("userCode")),
        },
        headers=_approve_page_headers(nonce),
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/tv-auth/approve — mobile/web validates the user code
# ──────────────────────────────────────────────────────────────────────────────

def _enforce_ip_limit(
    request: Request | None,
    limiter: rate_limit.SlidingWindowLimiter,
    max_events: int,
    window_seconds: int = 60,
) -> None:
    if request is None:
        return
    client_key = rate_limit.resolve_client_ip(request) or "unknown"
    try:
        limiter.hit(client_key, max_events=max_events, window_seconds=window_seconds)
    except rate_limit.RateLimitExceeded as exc:
        raise _too_many_requests(
            exc.retry_after_seconds, "Too many requests, slow down"
        ) from None


@router.post(
    "/approve",
    response_model=ApproveResponse,
    response_model_by_alias=True,
    dependencies=[Depends(verify_pairing_api_key)],
)
async def approve(
    body: ApproveRequest,
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
) -> ApproveResponse:
    """Attach the (encrypted) config payload to a pending session.

    Authenticated: requires an X-API-Key (backend shared secret or an active
    per-user key) — only a device that is already configured can hand its
    config to a TV.
    """
    _enforce_ip_limit(
        request,
        rate_limit.tv_auth_approve_limiter,
        settings.TV_AUTH_APPROVE_RATE_LIMIT_MAX,
    )
    _require_crypto_configured()

    if not body.payload:
        raise HTTPException(status_code=422, detail="payload cannot be empty")

    try:
        session = await tv_auth_service.approve_pairing_session(
            db,
            user_code=body.user_code,
            payload=body.payload,
            write_fn=write_with_retry,
        )
    except tv_auth_service.UnknownPairingCodeError:
        raise HTTPException(status_code=404, detail="Unknown pairing code")
    except tv_auth_service.PairingExpiredError:
        raise HTTPException(status_code=410, detail="Pairing session expired")
    except tv_auth_service.PairingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    logger.info("tv-auth session %s approved", session.id)
    return ApproveResponse(status=STATUS_APPROVED)


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/tv-auth/status — TV poll (lightweight, backoff-friendly)
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/status", response_model=StatusResponse, response_model_by_alias=True)
async def get_status(
    # CR-F06: accept `deviceCode` (camelCase, preferred — consistent with the
    # rest of the API) while keeping the legacy snake_case `device_code` alive
    # for back-compat. Either may be supplied; deviceCode wins if both are.
    device_code: str | None = Query(
        default=None, alias="deviceCode", min_length=16, max_length=128
    ),
    device_code_legacy: str | None = Query(
        default=None, alias="device_code", min_length=16, max_length=128
    ),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """Return the session status; deliver the decrypted payload exactly once."""
    _enforce_ip_limit(
        request,
        rate_limit.tv_auth_status_limiter,
        settings.TV_AUTH_STATUS_RATE_LIMIT_MAX,
    )
    code = device_code or device_code_legacy
    if not code:
        raise HTTPException(
            status_code=422,
            detail="deviceCode (or device_code) query parameter is required",
        )

    result = await db.execute(
        select(TvAuthSession).where(TvAuthSession.device_code == code)
    )
    session = result.scalars().first()
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown device code")

    if await _expire_if_needed(db, session):
        return StatusResponse(status=STATUS_EXPIRED, expires_in=None)

    expires_in = max(0, (session.expires_at - _now_ms()) // 1000)

    if session.status == STATUS_APPROVED and not session.payload_delivered:
        if not session.payload_encrypted:
            # Defensive: approved without payload should be impossible.
            raise HTTPException(status_code=500, detail="Pairing payload missing")
        try:
            payload = decrypt_payload(session.payload_encrypted)
        except (PayloadDecryptError, RuntimeError) as exc:
            logger.error("tv-auth session %s payload undecryptable: %s", session.id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pairing payload unavailable",
            ) from exc

        # CR-F07: the read-then-mark-delivered above is NOT atomic across two
        # concurrent pollers (both could observe payload_delivered=False and
        # both decrypt+return). Make the actual "claim" atomic with a single
        # conditional UPDATE: SQLite serializes writers, so of two concurrent
        # claims only one UPDATE can match `payload_delivered IS FALSE` and
        # report rowcount == 1 — only that request is allowed to return the
        # payload. `decrypt_payload` above is pure (no side effect), so
        # computing it twice under contention is harmless; only the DELIVERY
        # is guarded.
        # Derive a session_factory bound to the same engine as `db` so both
        # production and per-thread/per-test engines survive real locks.
        async def _claim(s: AsyncSession) -> int:
            res = await s.execute(
                update(TvAuthSession)
                .where(
                    TvAuthSession.id == session.id,
                    TvAuthSession.payload_delivered.is_(False),
                )
                .values(payload_delivered=True)
            )
            await s.commit()
            return res.rowcount

        claimed_rows = await write_with_retry(
            _claim,
            session_factory=session_factory_for(db),
            op="tv_auth.claim_payload",
        )

        if claimed_rows == 1:
            session.payload_delivered = True  # keep the ORM object in sync
            logger.info("tv-auth session %s payload delivered", session.id)
            return StatusResponse(
                status=STATUS_APPROVED, expires_in=int(expires_in), payload=payload
            )
        # Lost the race: another concurrent poll already claimed delivery —
        # fall through to the normal (payload-less) status response below.

    return StatusResponse(
        status=session.status,
        expires_in=None if session.status == STATUS_COMPLETED else int(expires_in),
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/tv-auth/complete — TV acknowledges (one-shot)
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/complete", response_model=CompleteResponse, response_model_by_alias=True)
async def complete(
    body: CompleteRequest,
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
) -> CompleteResponse:
    """Finalize an approved session: single-use, scrubs the encrypted payload."""
    _enforce_ip_limit(
        request,
        rate_limit.tv_auth_complete_limiter,
        settings.TV_AUTH_COMPLETE_RATE_LIMIT_MAX,
    )
    result = await db.execute(
        select(TvAuthSession).where(TvAuthSession.device_code == body.device_code)
    )
    session = result.scalars().first()
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown device code")

    if await _expire_if_needed(db, session):
        raise HTTPException(status_code=410, detail="Pairing session expired")
    if session.status == STATUS_COMPLETED:
        raise HTTPException(status_code=409, detail="Pairing session already completed")
    if session.status != STATUS_APPROVED:
        raise HTTPException(status_code=409, detail="Pairing session not approved yet")

    # AUDIT-P1-001 (S3.3): converted to write_with_retry — same shape as
    # approve() above: validation already happened against `db`, this
    # targets the row by id with a blind UPDATE of already-computed values
    # (never mutating the `session` ORM object), so it's safely replayable
    # on a lock retry and leaves nothing for get_db's implicit commit to
    # double-write (ADR 0004, Decision 4).
    completed_at = _now_ms()
    session_id = session.id

    async def _work(fresh_session: AsyncSession) -> None:
        await fresh_session.execute(
            update(TvAuthSession)
            .where(TvAuthSession.id == session_id)
            .values(
                status=STATUS_COMPLETED,
                completed_at=completed_at,
                payload_encrypted=None,  # scrub the sensitive blob at rest
            )
        )
        await fresh_session.commit()

    await write_with_retry(_work, op="tv_auth.complete")

    logger.info("tv-auth session %s completed", session.id)
    return CompleteResponse(status=STATUS_COMPLETED)
