"""TV pairing orchestration, Fernet encryption, and DB persistence (CR-A01)."""
from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import TvAuthSession
from app.utils import rate_limit
from app.utils.db_retry import write_with_retry
from app.utils.payload_crypto import encrypt_payload as _crypto_encrypt_payload

logger = logging.getLogger("plexhub.services.tv_auth")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"

USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
USER_CODE_LENGTH = 8
CLEANUP_GRACE_MS = 60 * 60 * 1000
PENDING_CAP_RETRY_AFTER_SECONDS = 30


class UserCodeExhaustedError(Exception):
    """All 5 user_code collision attempts failed within one transaction."""


class UnknownPairingCodeError(LookupError):
    """Raised when no TvAuthSession matches the normalized user_code."""


class PairingExpiredError(Exception):
    """Raised when the pairing session has expired."""


class PairingConflictError(Exception):
    """Raised when the pairing session is not in pending state."""

    def __init__(self, current_status: str) -> None:
        self.current_status = current_status
        super().__init__(f"Pairing session already {current_status}")


def now_ms() -> int:
    return int(time.time() * 1000)


def generate_user_code() -> str:
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))


def format_user_code(code: str) -> str:
    """ABCDEFGH -> ABCD-EFGH (display form)."""
    half = USER_CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


def normalize_user_code(code: str) -> str:
    """Accept 'abcd-efgh', 'ABCD EFGH', 'ABCDEFGH' -> 'ABCDEFGH'."""
    return "".join(c for c in code.upper() if c.isalnum())


async def expire_if_needed(
    db: AsyncSession,
    session: TvAuthSession,
    *,
    write_fn: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Lazily flip a stale session to expired and scrub its payload."""
    if session.status in (STATUS_EXPIRED, STATUS_COMPLETED):
        return session.status == STATUS_EXPIRED
    if now_ms() <= session.expires_at:
        return False

    session_id = session.id

    async def _work(fresh_session: AsyncSession) -> None:
        await fresh_session.execute(
            update(TvAuthSession)
            .where(TvAuthSession.id == session_id)
            .values(status=STATUS_EXPIRED, payload_encrypted=None)
        )
        await fresh_session.commit()

    await (write_fn or write_with_retry)(_work, op="tv_auth.expire_session")
    return True


async def create_pairing_session(
    *,
    device_name: str | None,
    write_fn: Callable[..., Awaitable[Any]] | None = None,
) -> TvAuthSession:
    """Allocate a new TvAuthSession with bounded user_code collision retries."""
    now = now_ms()
    ttl_ms = settings.TV_AUTH_TTL_SECONDS * 1000
    device_code = secrets.token_urlsafe(32)

    async def _work(fresh_session: AsyncSession) -> TvAuthSession:
        await fresh_session.execute(
            delete(TvAuthSession).where(TvAuthSession.expires_at < now - CLEANUP_GRACE_MS)
        )
        pending_count = (
            await fresh_session.execute(
                select(func.count())
                .select_from(TvAuthSession)
                .where(TvAuthSession.status == STATUS_PENDING)
            )
        ).scalar_one()
        rate_limit.check_pending_cap(
            pending_count,
            settings.TV_AUTH_PENDING_SESSIONS_CAP,
            retry_after_seconds=PENDING_CAP_RETRY_AFTER_SECONDS,
        )

        for _ in range(5):
            candidate = TvAuthSession(
                id=uuid.uuid4().hex,
                device_code=device_code,
                user_code=generate_user_code(),
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
        raise UserCodeExhaustedError()

    return await (write_fn or write_with_retry)(_work, op="tv_auth.start")


async def approve_pairing_session(
    db: AsyncSession,
    *,
    user_code: str,
    payload: Any,
    write_fn: Callable[..., Awaitable[Any]] | None = None,
) -> TvAuthSession:
    """Validate and approve a pending pairing session with encrypted payload."""
    normalized = normalize_user_code(user_code)
    result = await db.execute(
        select(TvAuthSession).where(TvAuthSession.user_code == normalized)
    )
    session = result.scalars().first()
    if session is None:
        raise UnknownPairingCodeError(normalized)

    if await expire_if_needed(db, session, write_fn=write_fn):
        raise PairingExpiredError()
    if session.status != STATUS_PENDING:
        raise PairingConflictError(session.status)

    encrypted_payload = _crypto_encrypt_payload(payload)
    approved_at = now_ms()
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

    await (write_fn or write_with_retry)(_work, op="tv_auth.approve")
    return session
