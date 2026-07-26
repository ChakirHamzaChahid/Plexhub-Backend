"""Guard tests for AUDIT-P2-003 / CR-S04: the tv-auth Fernet key derived from
`AI_API_KEY` must be domain-separated from the Xtream-password key derived
from the very same secret in `app.utils.crypto_fields` (S4.5).

Covers:
- The two derived keys are NOT byte-identical (a token encrypted under one
  cannot be decrypted under the other) even for the same `AI_API_KEY`.
- An explicit `TV_AUTH_ENCRYPTION_KEY` always wins over the derivation and is
  unaffected by this change.
- A payload encrypted under the *old* (pre-fix, non-domain-separated)
  derivation is now undecryptable and raises `PayloadDecryptError` — never an
  uncaught exception / 500 — so `GET /api/tv-auth/status` can keep answering
  a clean 503 (see `app/api/tv_auth.py::get_status`).
- Fail-closed behaviour is preserved: with neither key configured,
  `get_fernet()` is `None` and `encrypt_payload`/`decrypt_payload` raise
  `RuntimeError` (turned into 503 "not configured" by the router), never a
  bare exception leaking key material.
- No secret/key material ever appears in a log message.
"""
from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.utils import crypto_fields, payload_crypto

API_KEY = "test-shared-secret-do-not-reuse"


def test_derived_key_differs_from_xtream_derivation(monkeypatch):
    """Same AI_API_KEY, both dedicated keys unset: the two derived Fernet
    keys must not be interchangeable (domain separation, the core invariant
    of this fix)."""
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")
    monkeypatch.setattr(settings, "XTREAM_ENCRYPTION_KEY", "")

    tv_auth_fernet = payload_crypto.get_fernet()
    xtream_fernet = crypto_fields.get_xtream_fernet()
    assert tv_auth_fernet is not None
    assert xtream_fernet is not None

    token = tv_auth_fernet.encrypt(b"secret-plex-token")
    with pytest.raises(InvalidToken):
        xtream_fernet.decrypt(token)


def test_derivation_uses_dedicated_context_tag(monkeypatch):
    """Pin the exact derivation formula so a future refactor can't silently
    re-collide the two derived keys."""
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")

    expected_key = base64.urlsafe_b64encode(
        hashlib.sha256(
            payload_crypto._KEY_DERIVATION_CONTEXT + API_KEY.encode("utf-8")
        ).digest()
    )
    reference = Fernet(expected_key)
    resolved = payload_crypto.get_fernet()

    token = reference.encrypt(b"x")
    assert resolved.decrypt(token) == b"x"
    # Context tag itself must differ from the Xtream one (belt-and-suspenders
    # against copy-paste reuse of the same literal tag).
    assert (
        payload_crypto._KEY_DERIVATION_CONTEXT
        != crypto_fields._KEY_DERIVATION_CONTEXT
    )


def test_explicit_key_wins_over_derivation(monkeypatch):
    explicit_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", explicit_key)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)

    resolved = payload_crypto.get_fernet()
    assert resolved is not None

    # It must be the explicit key, not a derivation from AI_API_KEY.
    reference = Fernet(explicit_key.encode("utf-8"))
    token = reference.encrypt(b"payload")
    assert resolved.decrypt(token) == b"payload"


def test_roundtrip_encrypt_decrypt_with_derived_key(monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)

    payload = {"plexToken": "abc123", "serverUrl": "https://example.invalid"}
    token = payload_crypto.encrypt_payload(payload)
    assert payload_crypto.decrypt_payload(token) == payload


def test_old_derivation_token_is_cleanly_undecryptable(monkeypatch):
    """A token produced by the pre-fix derivation (sha256(AI_API_KEY) with no
    context tag) must fail closed via `PayloadDecryptError` — the same
    exception type the router already catches to answer 503 — never an
    uncaught `InvalidToken` / 500."""
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)

    old_derived = base64.urlsafe_b64encode(
        hashlib.sha256(API_KEY.encode("utf-8")).digest()
    )
    stale_token = Fernet(old_derived).encrypt(b'{"stale": true}').decode("ascii")

    with pytest.raises(payload_crypto.PayloadDecryptError):
        payload_crypto.decrypt_payload(stale_token)


def test_fail_closed_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", "")
    monkeypatch.setattr(settings, "AI_API_KEY", "")

    assert payload_crypto.get_fernet() is None
    with pytest.raises(RuntimeError):
        payload_crypto.encrypt_payload({"a": 1})
    with pytest.raises(RuntimeError):
        payload_crypto.decrypt_payload("irrelevant")


def test_no_key_material_in_error_logs(monkeypatch, caplog):
    """An invalid explicit TV_AUTH_ENCRYPTION_KEY logs an error but must
    never include the offending key value itself (piège §9.10)."""
    bogus_key = "not-a-valid-fernet-key"
    monkeypatch.setattr(settings, "TV_AUTH_ENCRYPTION_KEY", bogus_key)

    with caplog.at_level("ERROR"):
        assert payload_crypto.get_fernet() is None

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert bogus_key not in logged
