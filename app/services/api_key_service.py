"""Multi API-key management.

Keys are random tokens ``phk_<urlsafe>`` shown once at creation; only their
SHA-256 hex digest is persisted (``ApiKey.key_hash``). Verification (see
``app.api.deps``) hashes the presented header and looks the row up by digest,
then checks it is neither revoked nor expired.

The legacy shared secret ``settings.AI_API_KEY`` remains a permanent master key
handled in ``app.api.deps`` — it is not represented by a row here.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from collections.abc import Iterable

from sqlalchemy import select, update

from app.db.database import async_session_factory
from app.models.database import ApiKey
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.apikey")

_TOKEN_PREFIX = "phk_"
_DISPLAY_PREFIX_LEN = 10          # chars of plaintext kept for display
_LAST_USED_THROTTLE_MS = 60_000   # only bump last_used at most once/min per key

# Closed set of scope names (ADR 0008 § D2.1). The ONLY definition in the repo:
# the API `Literal`, the admin form and the guards all derive from it.
KNOWN_SCOPES: frozenset[str] = frozenset({"scrape", "scrape.admin"})

# Unknown values already reported by parse_scopes (log once per value).
_LOGGED_UNKNOWN_SCOPES: set[str] = set()


class InvalidScopeError(ValueError):
    """A requested scope is not in KNOWN_SCOPES (or is malformed)."""


def normalize_scopes(scopes: Iterable[str]) -> str:
    """Validate requested scopes and return the stored form.

    Closed set, sorted, deduplicated, space-separated. Any unknown value, any
    value containing whitespace, any non-string, or a bare string (which would
    otherwise be iterated character by character) raises InvalidScopeError.
    """
    if isinstance(scopes, (str, bytes)):
        raise InvalidScopeError("scopes doit être une liste de valeurs, pas une chaîne")
    clean: set[str] = set()
    for value in scopes:
        if not isinstance(value, str) or value not in KNOWN_SCOPES:
            shown = repr(value)[:40]
            raise InvalidScopeError(
                f"Scope inconnu : {shown} (valeurs admises : "
                f"{', '.join(sorted(KNOWN_SCOPES))})"
            )
        clean.add(value)
    return " ".join(sorted(clean))


def parse_scopes(raw: str | None) -> frozenset[str]:
    """Known scopes stored in an ``api_keys.scopes`` value.

    Unknown values grant nothing and are dropped (logged once per value —
    never with the key). NB: a key whose column holds only unknown values
    is still a *scoped* key for the guards (they test the raw column).
    """
    if not raw:
        return frozenset()
    known: set[str] = set()
    for value in raw.split():
        if value in KNOWN_SCOPES:
            known.add(value)
        elif value not in _LOGGED_UNKNOWN_SCOPES:
            _LOGGED_UNKNOWN_SCOPES.add(value)
            logger.warning("Ignoring unknown API-key scope read from DB: %r", value[:40])
    return frozenset(known)


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Return a fresh opaque token: 'phk_' + 32 urlsafe random bytes."""
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


def is_active(key: ApiKey, *, at: int | None = None) -> bool:
    now = at if at is not None else now_ms()
    if key.revoked_at is not None:
        return False
    if key.expires_at is not None and key.expires_at <= now:
        return False
    return True


def status_of(key: ApiKey, *, at: int | None = None) -> str:
    now = at if at is not None else now_ms()
    if key.revoked_at is not None:
        return "revoked"
    if key.expires_at is not None and key.expires_at <= now:
        return "expired"
    return "active"


async def create_key(
    db,
    *,
    label: str,
    expires_at: int | None = None,
    scopes: Iterable[str] = (),
) -> tuple[ApiKey, str]:
    """Create a key. Returns (row, plaintext). The plaintext is the ONLY time
    the caller can see the token — it is not recoverable afterwards.

    ``scopes`` is validated against KNOWN_SCOPES BEFORE anything is written
    (InvalidScopeError, a ValueError): this is the single validation point for
    both the JSON API and the admin HTML form (ADR 0008 § D2.1).
    """
    stored_scopes = normalize_scopes(scopes)
    plaintext = generate_token()
    row = ApiKey(
        id=uuid.uuid4().hex,
        key_hash=_hash(plaintext),
        key_prefix=plaintext[:_DISPLAY_PREFIX_LEN],
        label=label.strip() or "unnamed",
        created_at=now_ms(),
        expires_at=expires_at,
        scopes=stored_scopes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "API key created: id=%s label=%r prefix=%s scopes=%r",
        row.id, row.label, row.key_prefix, row.scopes,
    )
    return row, plaintext


async def list_keys(db) -> list[ApiKey]:
    result = await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    return list(result.scalars().all())


async def revoke_key(db, key_id: str) -> ApiKey | None:
    """Mark a key revoked (idempotent). Returns the row or None if unknown."""
    row = await db.get(ApiKey, key_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = now_ms()
        await db.commit()
        await db.refresh(row)
        logger.info("API key revoked: id=%s label=%r", row.id, row.label)
    return row


async def resolve(plaintext: str, *, client_ip: str | None = None) -> ApiKey | None:
    """Return the active ApiKey matching this token, or None.

    Opens its OWN short-lived session so a best-effort last_used bump never
    interferes with the request handler's own transaction, and a locked DB
    (see stream-validation write windows) can't fail authentication.
    """
    if not plaintext:
        return None
    digest = _hash(plaintext)
    try:
        async with async_session_factory() as db:
            result = await db.execute(select(ApiKey).where(ApiKey.key_hash == digest))
            row = result.scalars().first()
            if row is None or not is_active(row):
                return None
            # Throttled, best-effort usage tracking — must never break auth.
            now = now_ms()
            if row.last_used_at is None or (now - row.last_used_at) >= _LAST_USED_THROTTLE_MS:
                try:
                    await db.execute(
                        update(ApiKey)
                        .where(ApiKey.id == row.id)
                        .values(last_used_at=now, last_used_ip=client_ip)
                    )
                    await db.commit()
                except Exception as exc:  # DB locked etc. — ignore, auth still valid
                    logger.debug("last_used bump skipped for %s: %s", row.id, type(exc).__name__)
            return row
    except Exception as exc:
        # Type name only: a SQLAlchemy error text embeds the key's SHA-256.
        logger.warning("API key resolve failed: %s", type(exc).__name__)
        return None
