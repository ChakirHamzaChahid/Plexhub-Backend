"""Execution coverage for CR-T06 (P2): app/services/api_key_service.py had zero
real test coverage (mint/resolve/revoke/expiry, SHA-256 digest storage).

`create_key` mints a fresh ``phk_...`` token, shown to the caller exactly
once, and persists ONLY its SHA-256 hex digest (``ApiKey.key_hash``) — never
the plaintext. `resolve(plaintext)` re-hashes the presented token and looks
the row up by digest, returning it only when the key is neither revoked nor
expired. This file exercises that behavior end-to-end against an in-memory
DB (no mocking of the service itself), the same way `app/api/deps.py`'s
`verify_backend_secret` does in production.

`create_key`/`list_keys`/`get_key`/`revoke_key` take an explicit `db` session
(the caller's transaction), so `db_session` is used directly for those.
`resolve` deliberately opens its OWN short-lived session via the
module-level `async_session_factory` name (see its docstring) — that name is
bound at import time (`from app.db.database import async_session_factory`),
so it must be monkeypatched on `app.services.api_key_service`, not on
`app.db.database` (same caveat as `tests/test_plex_api_security.py`'s
`plex_source_module` wiring).
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.models.database import ApiKey
from app.services import api_key_service
from app.utils.time import now_ms


@pytest.fixture(autouse=True)
def _wire_resolve_to_test_db(monkeypatch, db_factory):
    """`resolve()` opens its own session via the module-level
    `async_session_factory` — point it at the isolated in-memory engine."""
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)


# ─── create_key: digest storage, plaintext shown once ───────────────────────


async def test_create_key_persists_only_sha256_digest_never_plaintext(db_session):
    row, plaintext = await api_key_service.create_key(db_session, label="My Phone")

    # The plaintext token has the documented shape.
    assert plaintext.startswith("phk_")
    assert len(plaintext) > len("phk_")

    # Only the SHA-256 hex digest of the plaintext is persisted.
    expected_digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    assert row.key_hash == expected_digest

    # Re-read the row straight from the DB (not the in-memory object) to make
    # sure nothing leaked the plaintext into any column.
    persisted = (
        await db_session.execute(select(ApiKey).where(ApiKey.id == row.id))
    ).scalars().first()
    assert persisted is not None
    assert persisted.key_hash == expected_digest
    assert persisted.key_hash != plaintext
    # key_prefix is a short *display* fragment of the plaintext (by design,
    # for the admin UI to recognise a key) — never the full token nor able to
    # reconstruct it.
    assert persisted.key_prefix == plaintext[:10]
    assert persisted.key_prefix != plaintext
    # Belt-and-braces: the full plaintext must not appear verbatim anywhere
    # else on the row.
    for column in ("id", "label", "key_hash"):
        assert plaintext not in str(getattr(persisted, column))


async def test_create_key_plaintext_is_not_recoverable_afterwards(db_session):
    """The plaintext is returned once by create_key and never again — list_keys
    only exposes the hash/prefix, matching the docstring's "shown once"."""
    row, plaintext = await api_key_service.create_key(db_session, label="Once")

    keys = await api_key_service.list_keys(db_session)
    assert len(keys) == 1
    assert keys[0].id == row.id
    # ApiKey the ORM model has no plaintext field at all.
    assert not hasattr(keys[0], "key_plaintext")
    assert not hasattr(keys[0], "token")


async def test_create_key_blank_label_defaults_to_unnamed(db_session):
    row, _ = await api_key_service.create_key(db_session, label="   ")
    assert row.label == "unnamed"


async def test_create_key_sets_expires_at_when_provided(db_session):
    expires_at = now_ms() + 86_400_000
    row, _ = await api_key_service.create_key(
        db_session, label="Expiring", expires_at=expires_at,
    )
    assert row.expires_at == expires_at


# ─── resolve: active key ─────────────────────────────────────────────────


async def test_resolve_returns_row_for_active_key(db_session):
    row, plaintext = await api_key_service.create_key(db_session, label="Active")
    await db_session.commit()

    resolved = await api_key_service.resolve(plaintext)

    assert resolved is not None
    assert resolved.id == row.id


async def test_resolve_returns_none_for_wrong_token(db_session):
    await api_key_service.create_key(db_session, label="Someone")
    await db_session.commit()

    resolved = await api_key_service.resolve("phk_definitely-not-the-right-token")
    assert resolved is None


async def test_resolve_returns_none_for_unknown_token_empty_db():
    resolved = await api_key_service.resolve("phk_anything")
    assert resolved is None


async def test_resolve_returns_none_for_empty_string():
    resolved = await api_key_service.resolve("")
    assert resolved is None


async def test_resolve_is_exact_digest_match_not_prefix_or_case_insensitive(db_session):
    """A token differing only by case (or truncated) hashes to a different
    digest and must NOT resolve — proves resolve() is a real hash lookup, not
    a prefix/substring/case-insensitive match."""
    row, plaintext = await api_key_service.create_key(db_session, label="Case")
    await db_session.commit()

    flipped = plaintext.upper() if plaintext != plaintext.upper() else plaintext.lower()
    assert await api_key_service.resolve(flipped) is None
    assert await api_key_service.resolve(plaintext[:-1]) is None  # truncated by one char


# ─── resolve: revoked ──────────────────────────────────────────────────────


async def test_resolve_returns_none_after_revoke(db_session):
    row, plaintext = await api_key_service.create_key(db_session, label="Revoke me")
    await db_session.commit()

    assert await api_key_service.resolve(plaintext) is not None  # active before revoke

    revoked = await api_key_service.revoke_key(db_session, row.id)
    assert revoked is not None
    assert revoked.revoked_at is not None

    assert await api_key_service.resolve(plaintext) is None  # stops resolving


async def test_revoke_key_unknown_id_returns_none(db_session):
    assert await api_key_service.revoke_key(db_session, "no-such-id") is None


async def test_revoke_key_is_idempotent_keeps_original_timestamp(monkeypatch, db_session):
    row, _ = await api_key_service.create_key(db_session, label="Idempotent")
    await db_session.commit()

    ticker = iter([1_000, 2_000, 3_000])
    monkeypatch.setattr(api_key_service, "now_ms", lambda: next(ticker))

    first = await api_key_service.revoke_key(db_session, row.id)
    assert first.revoked_at == 1_000

    second = await api_key_service.revoke_key(db_session, row.id)
    assert second.revoked_at == 1_000  # unchanged by the second call (2_000 never used)


# ─── resolve: expiry ────────────────────────────────────────────────────────


async def test_resolve_returns_none_for_expired_key(db_session):
    past = now_ms() - 1_000
    row, plaintext = await api_key_service.create_key(
        db_session, label="Expired", expires_at=past,
    )
    await db_session.commit()

    assert await api_key_service.resolve(plaintext) is None


async def test_resolve_returns_row_for_key_expiring_in_the_future(db_session):
    future = now_ms() + 86_400_000
    row, plaintext = await api_key_service.create_key(
        db_session, label="Not yet expired", expires_at=future,
    )
    await db_session.commit()

    resolved = await api_key_service.resolve(plaintext)
    assert resolved is not None
    assert resolved.id == row.id


def test_is_active_false_for_key_expiring_exactly_now():
    """`expires_at <= now` is the documented boundary (is_active/status_of) —
    a key expiring at exactly `now` must already be treated as expired, not
    valid-until-strictly-past."""
    key = ApiKey(
        id="k", key_hash="h", key_prefix="phk_x", label="l",
        created_at=0, expires_at=1_000,
    )
    assert api_key_service.is_active(key, at=1_000) is False
    assert api_key_service.status_of(key, at=1_000) == "expired"


# ─── resolve: last_used tracking (best-effort, throttled) ───────────────────


async def test_resolve_sets_last_used_at_and_ip_on_first_use(db_session):
    row, plaintext = await api_key_service.create_key(db_session, label="Tracked")
    key_id = row.id
    await db_session.commit()
    assert row.last_used_at is None

    resolved = await api_key_service.resolve(plaintext, client_ip="203.0.113.5")
    assert resolved is not None

    # Re-read from DB to confirm the bump was actually persisted (resolve()
    # commits it in its own session). expire_all() drops this session's
    # identity-map cache so the SELECT below hits the DB, instead of
    # returning the stale in-memory `row` object untouched by that write.
    # `key_id` (a plain str captured before expiring) avoids touching any
    # attribute of the now-expired `row` outside of an awaited ORM call.
    db_session.expire_all()
    refreshed = (
        await db_session.execute(select(ApiKey).where(ApiKey.id == key_id))
    ).scalars().first()
    assert refreshed.last_used_at is not None
    assert refreshed.last_used_ip == "203.0.113.5"


async def test_resolve_throttles_repeated_last_used_bump_within_one_minute(db_session):
    """Two resolves within the same 60s throttle window must not re-bump
    last_used_at a second time (best-effort tracking, not a write on every
    request)."""
    row, plaintext = await api_key_service.create_key(db_session, label="Throttled")
    key_id = row.id
    await db_session.commit()

    await api_key_service.resolve(plaintext, client_ip="1.1.1.1")
    db_session.expire_all()  # drop identity-map cache — force a fresh SELECT
    first = (
        await db_session.execute(select(ApiKey).where(ApiKey.id == key_id))
    ).scalars().first()
    first_seen = first.last_used_at
    assert first_seen is not None

    # Immediately again, well within the 60s throttle window.
    await api_key_service.resolve(plaintext, client_ip="2.2.2.2")
    db_session.expire_all()
    second = (
        await db_session.execute(select(ApiKey).where(ApiKey.id == key_id))
    ).scalars().first()
    assert second.last_used_at == first_seen  # not bumped again
    assert second.last_used_ip == "1.1.1.1"  # ip from the throttled call not recorded


# ─── is_active / status_of (pure helpers, no DB) ────────────────────────────


def test_is_active_true_for_fresh_key():
    key = ApiKey(id="k", key_hash="h", key_prefix="phk_x", label="l", created_at=0)
    assert api_key_service.is_active(key, at=1_000) is True
    assert api_key_service.status_of(key, at=1_000) == "active"


def test_is_active_false_when_revoked():
    key = ApiKey(
        id="k", key_hash="h", key_prefix="phk_x", label="l",
        created_at=0, revoked_at=500,
    )
    assert api_key_service.is_active(key, at=1_000) is False
    assert api_key_service.status_of(key, at=1_000) == "revoked"


def test_is_active_false_when_expired():
    key = ApiKey(
        id="k", key_hash="h", key_prefix="phk_x", label="l",
        created_at=0, expires_at=500,
    )
    assert api_key_service.is_active(key, at=1_000) is False
    assert api_key_service.status_of(key, at=1_000) == "expired"


def test_is_active_true_when_expires_at_in_the_future():
    key = ApiKey(
        id="k", key_hash="h", key_prefix="phk_x", label="l",
        created_at=0, expires_at=5_000,
    )
    assert api_key_service.is_active(key, at=1_000) is True
    assert api_key_service.status_of(key, at=1_000) == "active"


def test_revoked_takes_priority_over_expiry_in_status_of():
    key = ApiKey(
        id="k", key_hash="h", key_prefix="phk_x", label="l",
        created_at=0, expires_at=500, revoked_at=600,
    )
    assert api_key_service.status_of(key, at=1_000) == "revoked"


# ─── list_keys / get_key ────────────────────────────────────────────────────


async def test_list_keys_orders_newest_first(db_session, monkeypatch):
    ticker = iter([100, 200, 300])
    monkeypatch.setattr(api_key_service, "now_ms", lambda: next(ticker))

    row_a, _ = await api_key_service.create_key(db_session, label="A")
    row_b, _ = await api_key_service.create_key(db_session, label="B")
    row_c, _ = await api_key_service.create_key(db_session, label="C")

    keys = await api_key_service.list_keys(db_session)
    assert [k.id for k in keys] == [row_c.id, row_b.id, row_a.id]


async def test_get_key_returns_none_for_unknown_id(db_session):
    assert await api_key_service.get_key(db_session, "nope") is None


async def test_get_key_returns_the_row(db_session):
    row, _ = await api_key_service.create_key(db_session, label="Findable")
    fetched = await api_key_service.get_key(db_session, row.id)
    assert fetched is not None
    assert fetched.id == row.id
