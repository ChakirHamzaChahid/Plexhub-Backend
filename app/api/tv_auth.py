"""Device-flow TV pairing API (Mission 18).

Reproduces the ARVIO Supabase Edge Functions pattern (tv-auth-start/approve/
complete/status) as native FastAPI endpoints — no Supabase.

Flow (RFC 8628-like):
    1. POST /api/tv-auth/start    TV asks for a session. Gets a long secret
                                  `deviceCode` (poll credential) + a short
                                  `userCode` (displayed on screen + QR).
    2. POST /api/tv-auth/approve  Mobile/web (authenticated via X-API-Key)
                                  validates the userCode and attaches the
                                  config payload (encrypted at rest).
    3. GET  /api/tv-auth/status   TV polls by deviceCode (backoff-friendly).
                                  The decrypted payload is delivered EXACTLY
                                  once, on the first poll after approval.
    4. POST /api/tv-auth/complete TV acknowledges -> session completed
                                  (one-shot), encrypted payload scrubbed.

Security model:
    - Sessions expire after settings.TV_AUTH_TTL_SECONDS (default 15 min).
    - deviceCode is a 32-byte urlsafe token — unguessable.
    - approve requires the backend shared secret (X-API-Key, constant-time).
    - Payload is Fernet-encrypted at rest (app/utils/payload_crypto.py) and
      never re-delivered after the first read.
"""
from __future__ import annotations

import logging
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_backend_secret as verify_pairing_api_key
from app.config import settings
from app.db.database import get_db
from app.models.database import TvAuthSession
from app.utils.db_retry import commit_with_retry
from app.utils.payload_crypto import (
    PayloadDecryptError,
    decrypt_payload,
    encrypt_payload,
    get_fernet,
)

logger = logging.getLogger("plexhub.tvauth")

router = APIRouter(prefix="/tv-auth", tags=["tv-auth"])

# Unambiguous alphabet for the human code (no 0/O, 1/I/L).
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_USER_CODE_LENGTH = 8
_POLL_INTERVAL_SECONDS = 5  # suggested base interval for client backoff
_CLEANUP_GRACE_MS = 60 * 60 * 1000  # purge sessions expired > 1h ago

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
    return int(time.time() * 1000)


def _generate_user_code() -> str:
    return "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_LENGTH))


def _format_user_code(code: str) -> str:
    """ABCDEFGH -> ABCD-EFGH (display form)."""
    half = _USER_CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


def _normalize_user_code(code: str) -> str:
    """Accept 'abcd-efgh', 'ABCD EFGH', 'ABCDEFGH' -> 'ABCDEFGH'."""
    return "".join(c for c in code.upper() if c.isalnum())


def _require_crypto_configured() -> None:
    if get_fernet() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TV pairing not configured",
        )


async def _expire_if_needed(db: AsyncSession, session: TvAuthSession) -> bool:
    """Lazily flip a stale session to expired and scrub its payload.

    Returns True when the session is (now) expired.
    """
    if session.status in (STATUS_EXPIRED, STATUS_COMPLETED):
        return session.status == STATUS_EXPIRED
    if _now_ms() <= session.expires_at:
        return False
    session.status = STATUS_EXPIRED
    session.payload_encrypted = None  # never deliver a stale payload
    # CR-C04: this lazy expiry write can race a long-running sync/validation
    # holding the single WAL writer — retry on "database is locked" like the
    # workers do, instead of surfacing a raw 500 to every caller (approve/
    # status/complete all funnel through this helper).
    await commit_with_retry(db)
    return True


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
    """Create a pending pairing session. Called by the (unauthenticated) TV."""
    _require_crypto_configured()

    now = _now_ms()
    ttl_ms = settings.TV_AUTH_TTL_SECONDS * 1000

    # Opportunistic cleanup: drop sessions expired for more than the grace
    # period (keeps the table tiny and frees unique user codes).
    await db.execute(
        delete(TvAuthSession).where(TvAuthSession.expires_at < now - _CLEANUP_GRACE_MS)
    )

    device_code = secrets.token_urlsafe(32)
    device_name = payload.device_name if payload else None

    # user_code is UNIQUE — retry on the (astronomically rare) collision.
    session = None
    for _ in range(5):
        candidate = TvAuthSession(
            id=uuid.uuid4().hex,
            device_code=device_code,
            user_code=_generate_user_code(),
            status=STATUS_PENDING,
            payload_encrypted=None,
            payload_delivered=False,
            device_name=device_name,
            created_at=now,
            expires_at=now + ttl_ms,
        )
        db.add(candidate)
        try:
            # CR-C04: retry on lock contention; IntegrityError (user_code
            # collision) is a different exception and still falls through to
            # the except clause below unchanged.
            await commit_with_retry(db)
            session = candidate
            break
        except IntegrityError:
            await db.rollback()
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
# POST /api/tv-auth/approve — mobile/web validates the user code
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/approve",
    response_model=ApproveResponse,
    response_model_by_alias=True,
    dependencies=[Depends(verify_pairing_api_key)],
)
async def approve(
    body: ApproveRequest,
    db: AsyncSession = Depends(get_db),
) -> ApproveResponse:
    """Attach the (encrypted) config payload to a pending session.

    Authenticated: requires the backend shared secret in X-API-Key — only a
    device that is already configured can hand its config to a TV.
    """
    _require_crypto_configured()

    if not body.payload:
        raise HTTPException(status_code=422, detail="payload cannot be empty")

    user_code = _normalize_user_code(body.user_code)
    result = await db.execute(
        select(TvAuthSession).where(TvAuthSession.user_code == user_code)
    )
    session = result.scalars().first()
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown pairing code")

    if await _expire_if_needed(db, session):
        raise HTTPException(status_code=410, detail="Pairing session expired")
    if session.status != STATUS_PENDING:
        raise HTTPException(
            status_code=409, detail=f"Pairing session already {session.status}"
        )

    session.payload_encrypted = encrypt_payload(body.payload)
    session.status = STATUS_APPROVED
    session.approved_at = _now_ms()
    await commit_with_retry(db)  # CR-C04: lock-retry on the request path

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
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """Return the session status; deliver the decrypted payload exactly once."""
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
        claim_result = await db.execute(
            update(TvAuthSession)
            .where(
                TvAuthSession.id == session.id,
                TvAuthSession.payload_delivered.is_(False),
            )
            .values(payload_delivered=True)
        )
        await commit_with_retry(db)

        if claim_result.rowcount == 1:
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
    db: AsyncSession = Depends(get_db),
) -> CompleteResponse:
    """Finalize an approved session: single-use, scrubs the encrypted payload."""
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

    session.status = STATUS_COMPLETED
    session.completed_at = _now_ms()
    session.payload_encrypted = None  # scrub the sensitive blob at rest
    await commit_with_retry(db)  # CR-C04: lock-retry on the request path

    logger.info("tv-auth session %s completed", session.id)
    return CompleteResponse(status=STATUS_COMPLETED)
