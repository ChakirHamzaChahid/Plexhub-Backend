"""Encryption-at-rest helper for TV pairing payloads (Mission 18).

The tv_auth_sessions.payload_encrypted column stores a Fernet token
(AES-128-CBC + HMAC-SHA256, urlsafe base64). The key is NEVER stored in the
repo — resolution order:

1. settings.TV_AUTH_ENCRYPTION_KEY — explicit Fernet key (urlsafe base64,
   32 decoded bytes). Generate one with:
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
2. Derived from settings.AI_API_KEY (SHA-256 -> urlsafe base64). Stable
   across workers/restarts as long as the shared API secret is stable.
3. Neither set -> None: the tv-auth endpoints answer 503 "not configured".

Settings are read at call time (not import time) so tests can monkeypatch.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger("plexhub.tvauth.crypto")


class PayloadDecryptError(Exception):
    """Raised when a stored payload cannot be decrypted (bad key / corrupt)."""


def get_fernet() -> Fernet | None:
    """Resolve the Fernet instance from settings, or None when unconfigured."""
    if settings.TV_AUTH_ENCRYPTION_KEY:
        try:
            return Fernet(settings.TV_AUTH_ENCRYPTION_KEY.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            logger.error("TV_AUTH_ENCRYPTION_KEY is not a valid Fernet key: %s", exc)
            return None
    if settings.AI_API_KEY:
        derived = base64.urlsafe_b64encode(
            hashlib.sha256(settings.AI_API_KEY.encode("utf-8")).digest()
        )
        return Fernet(derived)
    return None


def encrypt_payload(payload: dict) -> str:
    """Serialize a JSON-safe dict and encrypt it to a Fernet token string."""
    fernet = get_fernet()
    if fernet is None:
        raise RuntimeError("TV pairing encryption key not configured")
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return fernet.encrypt(raw).decode("ascii")


def decrypt_payload(token: str) -> dict:
    """Decrypt a Fernet token string back to the original dict."""
    fernet = get_fernet()
    if fernet is None:
        raise RuntimeError("TV pairing encryption key not configured")
    try:
        raw = fernet.decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise PayloadDecryptError("stored payload cannot be decrypted") from exc
    return json.loads(raw.decode("utf-8"))
