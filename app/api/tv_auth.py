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
from app.utils.db_retry import commit_with_retry, write_with_retry
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

    AUDIT-P1-001 (S3.3): the write is converted to `write_with_retry` (ADR
    0004, Decision 4 — a fresh session per attempt is the only pattern that
    actually survives a real SQLite lock). This does NOT mutate the
    `session` ORM object passed in (which belongs to the caller's `db`,
    the request-scoped session from Depends(get_db)) — it targets the row
    by id with a blind UPDATE instead. That's safe here because every
    caller (approve/get_status/complete) only branches on this function's
    **return value** when it's True (410/EXPIRED), never re-reads
    `session.status`/`session.payload_encrypted` afterwards in that branch
    — so there's nothing that depends on the in-memory object reflecting
    the new state, and no risk of `db`'s own implicit commit (at the end of
    the request) re-writing the same row a second time.
    """
    if session.status in (STATUS_EXPIRED, STATUS_COMPLETED):
        return session.status == STATUS_EXPIRED
    if _now_ms() <= session.expires_at:
        return False

    session_id = session.id

    async def _work(fresh_session: AsyncSession) -> None:
        await fresh_session.execute(
            update(TvAuthSession)
            .where(TvAuthSession.id == session_id)
            .values(status=STATUS_EXPIRED, payload_encrypted=None)
        )
        await fresh_session.commit()

    await write_with_retry(_work, op="tv_auth.expire_session")
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

    device_code = secrets.token_urlsafe(32)
    device_name = payload.device_name if payload else None

    # AUDIT-P1-001 (S3.3): converted to write_with_retry. The user_code
    # collision retry (IntegrityError) and the lock retry (OperationalError)
    # are two DIFFERENT concerns that must not be conflated (ADR 0004,
    # Decision 4 — the exact "cas délicat" flagged for this endpoint): a
    # lock-retry must NOT replay the same candidate (that would just trade
    # one failure mode for another), so the entire 5-attempt collision loop
    # lives INSIDE `work` and generates a fresh random user_code every time
    # it runs — including on a brand-new invocation triggered by an outer
    # lock retry (a fresh session, per write_with_retry's contract). Inside
    # a single `work` invocation, `session.rollback()` before trying a new
    # candidate on the SAME session is the normal, safe idiom for a business
    # constraint violation (IntegrityError) — this is NOT the same trap as
    # retrying a commit on the same session after a lock (PendingRollbackError,
    # ADR 0004): IntegrityError doesn't invalidate the transaction the way a
    # failed commit under a lock does.
    #
    # The opportunistic cleanup delete moved IN HERE too (it used to be
    # staged on the request-scoped `db` session, ahead of the loop). Leaving
    # it on `db` — uncommitted until get_db's implicit commit at the very
    # end of the request — turned out to be a genuine self-deadlock, not a
    # harmless decoupling: SQLite allows only ONE writer at a time across
    # ALL connections (even from the same process), so `db`'s still-open
    # write transaction would starve every attempt this fresh session makes
    # to acquire the write lock for the insert, and `db` never gets to
    # commit because the endpoint is still awaiting `write_with_retry`
    # (caught by the real-lock regression test below — it hung/failed until
    # this fix). Running both in the SAME fresh-session transaction removes
    # the second writer entirely and mirrors the original single-session
    # coupling (delete-then-insert, both committed — or rolled back and
    # retried — together).
    class _UserCodeExhaustedError(Exception):
        """All 5 collision attempts failed within one `work` invocation."""

    async def _work(fresh_session: AsyncSession) -> TvAuthSession:
        await fresh_session.execute(
            delete(TvAuthSession).where(TvAuthSession.expires_at < now - _CLEANUP_GRACE_MS)
        )
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
            fresh_session.add(candidate)
            try:
                await fresh_session.commit()
                return candidate
            except IntegrityError:
                await fresh_session.rollback()
        raise _UserCodeExhaustedError()

    try:
        session = await write_with_retry(_work, op="tv_auth.start")
    except _UserCodeExhaustedError:
        session = None
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

    # AUDIT-P1-001 (S3.3): converted to write_with_retry. Validation above
    # (unknown code / expired / already-approved) already happened against
    # `db`, the request-scoped session — this targets the row by id with a
    # blind UPDATE of the already-computed values instead of mutating the
    # `session` ORM object, so replaying it whole on a lock retry is a
    # harmless no-op re-write, and `db`'s own implicit commit at the end of
    # the request has nothing left dirty to double-write (ADR 0004,
    # Decision 4).
    encrypted_payload = encrypt_payload(body.payload)
    approved_at = _now_ms()
    session_id = session.id

    async def _work(fresh_session: AsyncSession) -> None:
        await fresh_session.execute(
            update(TvAuthSession)
            .where(TvAuthSession.id == session_id)
            .values(
                payload_encrypted=encrypted_payload,
                status=STATUS_APPROVED,
                approved_at=approved_at,
            )
        )
        await fresh_session.commit()

    await write_with_retry(_work, op="tv_auth.approve")

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
        # AUDIT-P1-001 (S3.3) — deliberately NOT converted to write_with_retry.
        # This is the CR-F07 atomic one-shot delivery claim: correctness
        # depends only on `rowcount` from THIS execute() being read before
        # commit, which holds regardless of which session/connection issues
        # the UPDATE. The blocker is test/production identity, not
        # correctness: write_with_retry's fresh session is resolved from a
        # single process-wide default (app.db.database.async_session_factory)
        # unless a `session_factory` bound to the SAME engine as `db` is
        # explicitly derived and threaded through — and the existing
        # regression test for this exact race
        # (tests/test_tv_auth.py::test_status_concurrent_polls_deliver_payload_exactly_once)
        # deliberately drives `get_status` with a hand-built, per-thread
        # session/engine pointing at a test-only file, specifically to prove
        # the claim survives real cross-connection contention — swapping in
        # a different, globally-resolved engine here would silently stop
        # exercising (or even miswire) that exact scenario. Given the
        # explicit warning that a fresh session in the wrong place would ruin
        # this atomicity, the safer choice is zero structural change:
        # `commit_with_retry` (now honest per ADR 0004) still protects the
        # common case where SQLite's own busy_timeout resolves the
        # contention before commit() ever raises; a real, sustained lock now
        # surfaces the true OperationalError instead of a misleading one.
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
