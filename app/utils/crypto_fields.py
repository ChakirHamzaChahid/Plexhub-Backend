"""Transparent at-rest encryption for SQLAlchemy string columns (CR-S03).

`EncryptedString` is a `TypeDecorator` that Fernet-encrypts a value when it
is bound as a SQL parameter (INSERT/UPDATE — including a bulk Core
``update(Model).values(...)`` statement, since SQLAlchemy still resolves the
column's type for parameter binding) and decrypts it when a row is loaded
(SELECT via ``select(Model)`` -> ORM instances). Every existing call site
that reads/writes the mapped Python attribute (e.g. ``account.password``)
keeps seeing the plaintext string — **no change needed anywhere** outside
the column type declaration.

Applied to ``XtreamAccount.password`` (`app/models/database.py`) to close
CR-S03 (Xtream provider passwords stored in plaintext at rest, including in
the online DB backup snapshots taken by the backup cron).

Key resolution — see `get_xtream_fernet()`:
  1. ``settings.XTREAM_ENCRYPTION_KEY`` — dedicated Fernet key (urlsafe
     base64, 32 decoded bytes). RECOMMENDED for production. Generate with::

         python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

  2. Derived from ``settings.AI_API_KEY`` (SHA-256 of a fixed
     domain-separation tag + the secret). This makes encryption work
     out-of-the-box without a second secret to manage, but it is
     **operationally coupled** to the API bearer secret: if `AI_API_KEY`
     is ever rotated without first re-encrypting existing rows under the
     new derived key, those rows become undecryptable (the column falls
     back to returning ciphertext rather than crashing — see
     `EncryptedString.process_result_value` — but the credential is
     effectively lost until repaired). A dedicated, independently-rotated
     `XTREAM_ENCRYPTION_KEY` is strongly recommended for any real
     deployment. The domain-separation tag also ensures this derived key is
     NOT byte-identical to the tv-auth payload key derived the same way in
     `utils/payload_crypto.py` from the same `AI_API_KEY` (CR-S04 already
     flags that reuse for tv-auth; this avoids adding a second identical
     copy of the same derived secret).
  3. Neither set -> fail **OPEN**, not closed: the column is stored/read as
     plaintext, exactly as before this fix, and a warning is logged once.
     This deliberately does not brick account creation / Xtream sync when
     no key is configured — encryption-at-rest is a hardening layer here,
     not an availability gate for a P2 finding. Configure either key above
     to get ciphertext at rest.

Migration 016 (`db/migrations.py`) performs a one-time, idempotent pass over
pre-existing `xtream_accounts` rows using this exact same key resolution, so
values written before this fix landed get encrypted in place.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import settings

logger = logging.getLogger("plexhub.crypto.fields")

# Fernet tokens are urlsafe-base64 of a versioned, fixed-format token; for
# the current Fernet version byte (0x80) the encoded string always starts
# with this literal prefix. Cheap, reliable "is this already encrypted?"
# probe that avoids attempting (and logging failures for) a real decrypt.
_FERNET_PREFIX = "gAAAAA"

# Domain-separation tag: keeps a fallback key derived from AI_API_KEY here
# from being byte-identical to the tv-auth key derived from the same secret
# in utils/payload_crypto.py.
_KEY_DERIVATION_CONTEXT = b"plexhub.xtream_password.v1:"

_warned_no_key = False


def looks_encrypted(value: str) -> bool:
    """Cheap check: is `value` already a Fernet token (vs. legacy plaintext)?"""
    return value.startswith(_FERNET_PREFIX)


def get_xtream_fernet() -> Fernet | None:
    """Resolve the Fernet instance for Xtream credential encryption.

    Returns None when unconfigured — callers must treat that as "no
    encryption available" and fail OPEN (see module docstring), never raise.
    Settings are read at call time (not import time) so tests can monkeypatch.
    """
    global _warned_no_key
    if settings.XTREAM_ENCRYPTION_KEY:
        try:
            return Fernet(settings.XTREAM_ENCRYPTION_KEY.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            logger.error("XTREAM_ENCRYPTION_KEY is not a valid Fernet key: %s", exc)
            return None
    if settings.AI_API_KEY:
        derived = base64.urlsafe_b64encode(
            hashlib.sha256(
                _KEY_DERIVATION_CONTEXT + settings.AI_API_KEY.encode("utf-8")
            ).digest()
        )
        return Fernet(derived)
    if not _warned_no_key:
        logger.warning(
            "Neither XTREAM_ENCRYPTION_KEY nor AI_API_KEY is set — Xtream "
            "account passwords will be stored in PLAINTEXT at rest (CR-S03). "
            "Set XTREAM_ENCRYPTION_KEY (recommended) or AI_API_KEY to encrypt."
        )
        _warned_no_key = True
    return None


class EncryptedString(TypeDecorator):
    """Fernet-encrypt a TEXT column on write, decrypt it on read.

    - Fail-open when no key is configured: values pass through unchanged
      (see module docstring) so a missing key can never break account
      creation, sync, or streaming — only the at-rest confidentiality
      guarantee is lost, exactly as it was before this fix.
    - Idempotent: a value that already looks like a Fernet token is never
      re-encrypted (`looks_encrypted`).
    - Never raises on decrypt failure (wrong/rotated key, corrupt token):
      returns the raw stored value instead, so one bad row can't 500 an
      entire account listing. The failure is logged.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if not value:
            return value
        if looks_encrypted(value):
            return value
        fernet = get_xtream_fernet()
        if fernet is None:
            return value
        return fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def process_result_value(self, value, dialect):
        if not value:
            return value
        if not looks_encrypted(value):
            return value
        fernet = get_xtream_fernet()
        if fernet is None:
            logger.error(
                "Cannot decrypt a stored Xtream password: no key configured "
                "(XTREAM_ENCRYPTION_KEY/AI_API_KEY both empty). Returning ciphertext."
            )
            return value
        try:
            return fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken:
            logger.error(
                "Cannot decrypt a stored Xtream password: invalid Fernet token "
                "for the currently configured key (rotated/mismatched key?)."
            )
            return value
