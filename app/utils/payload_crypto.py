"""Encryption-at-rest helper for TV pairing payloads (Mission 18).

The tv_auth_sessions.payload_encrypted column stores a Fernet token
(AES-128-CBC + HMAC-SHA256, urlsafe base64). The key is NEVER stored in the
repo — resolution order:

1. settings.TV_AUTH_ENCRYPTION_KEY — explicit Fernet key (urlsafe base64,
   32 decoded bytes). RECOMMENDED for production. Generate one with:
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
2. Derived from settings.AI_API_KEY (SHA-256 of a fixed domain-separation
   tag + the secret -> urlsafe base64). Stable across workers/restarts as
   long as the shared API secret is stable, but **operationally coupled**
   to the API bearer secret used to authenticate `/api/tv-auth/approve` and
   the rest of the JSON API (AUDIT-P2-003 / CR-S04): the same secret then
   doubles as authentication credential AND encryption-at-rest key for
   payloads that carry Plex tokens, so compromising one compromises the
   other, and rotating `AI_API_KEY` without re-encrypting existing rows
   makes them undecryptable. The domain-separation tag below
   (`_KEY_DERIVATION_CONTEXT`) at least ensures this derived key is NOT
   byte-identical to the Xtream-password derived key in
   `utils/crypto_fields.py` (which derives from the very same `AI_API_KEY`
   with its own tag) — two different derived secrets, not one secret reused
   verbatim across two unrelated ciphers. A dedicated, independently-set
   `TV_AUTH_ENCRYPTION_KEY` remains strongly recommended for any real
   deployment; it fully sidesteps this coupling (see resolution order above
   — an explicit key always wins and is never touched by this derivation).
3. Neither set -> None: the tv-auth endpoints answer 503 "not configured".

Failure mode on an undecryptable stored payload (wrong/rotated key, corrupt
token, or — as of this fix — an old-derivation token left over from before
the domain-separation tag was introduced): `decrypt_payload` raises
`PayloadDecryptError`, which callers turn into a clean 503 (never a 500,
never a raw stack trace with key material). TV pairing sessions are
short-lived (`settings.TV_AUTH_TTL_SECONDS`, default 900 s) — a device stuck
on such a session simply restarts pairing.

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

# Domain-separation tag: keeps a fallback key derived from AI_API_KEY here
# from being byte-identical to the Xtream-password key derived from the
# same secret in utils/crypto_fields.py (AUDIT-P2-003 / CR-S04). Mirrors
# that module's `_KEY_DERIVATION_CONTEXT` convention exactly, with a tag
# specific to this usage.
_KEY_DERIVATION_CONTEXT = b"plexhub.tv_auth_payload.v1:"


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
            hashlib.sha256(
                _KEY_DERIVATION_CONTEXT + settings.AI_API_KEY.encode("utf-8")
            ).digest()
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
