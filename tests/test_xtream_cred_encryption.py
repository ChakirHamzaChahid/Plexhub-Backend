"""Guard tests for CR-S03: Xtream account passwords must be encrypted at rest.

Covers:
- `XtreamAccount.password` (mapped through
  `app.utils.crypto_fields.EncryptedString`) is stored as a Fernet token in
  the raw DB row, never plaintext.
- The ORM keeps returning the plaintext transparently on read — no call
  site change required.
- Migration 016 (`app.db.migrations._migration_016_encrypt_xtream_passwords`)
  encrypts pre-existing plaintext rows in place and is idempotent
  (re-running it does not double-encrypt / does not touch already-encrypted
  rows).
"""
from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import select, text

from app.config import settings
from app.db.migrations import _migration_016_encrypt_xtream_passwords
from app.models.database import XtreamAccount
from app.utils.crypto_fields import looks_encrypted


def _account(account_id: str, password: str) -> XtreamAccount:
    return XtreamAccount(
        id=account_id,
        label="Test account",
        base_url=f"http://{account_id}.example",
        port=80,
        username="user",
        password=password,
        is_active=True,
        created_at=0,
    )


async def _raw_password(db_engine, account_id: str) -> str:
    """Read the password column via raw SQL, bypassing the ORM type decorator."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT password FROM xtream_accounts WHERE id = :id"),
            {"id": account_id},
        )
        return result.scalar_one()


async def test_password_is_encrypted_at_rest(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(settings, "XTREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())

    db_session.add(_account("acc1", "s3cr3t-plaintext"))
    await db_session.commit()

    raw_password = await _raw_password(db_engine, "acc1")

    assert raw_password != "s3cr3t-plaintext"
    assert looks_encrypted(raw_password)


async def test_password_reads_back_as_plaintext_via_orm(db_session, monkeypatch):
    monkeypatch.setattr(settings, "XTREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())

    db_session.add(_account("acc2", "another-secret"))
    await db_session.commit()
    db_session.expire_all()

    result = await db_session.execute(
        select(XtreamAccount).where(XtreamAccount.id == "acc2")
    )
    reloaded = result.scalars().first()

    assert reloaded.password == "another-secret"


async def test_no_key_configured_falls_back_to_plaintext(db_session, db_engine, monkeypatch):
    """Fail-open: with no key at all, the column stores/returns plaintext
    rather than breaking account creation (see crypto_fields.py docstring)."""
    monkeypatch.setattr(settings, "XTREAM_ENCRYPTION_KEY", "")
    monkeypatch.setattr(settings, "AI_API_KEY", "")

    db_session.add(_account("acc_nokey", "plaintext-when-unconfigured"))
    await db_session.commit()

    raw_password = await _raw_password(db_engine, "acc_nokey")
    assert raw_password == "plaintext-when-unconfigured"
    assert not looks_encrypted(raw_password)


async def test_migration_016_encrypts_preexisting_plaintext_row(db_engine, monkeypatch):
    monkeypatch.setattr(settings, "XTREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())

    # Simulate a pre-fix row via raw SQL INSERT (bypasses the ORM encryption
    # entirely, just like real rows written before this fix landed).
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO xtream_accounts
                    (id, label, base_url, port, username, password, status,
                     max_connections, allowed_formats, last_synced_at,
                     is_active, created_at, category_filter_mode)
                VALUES
                    (:id, 'Legacy', 'http://legacy.example', 80, 'legacyuser',
                     :password, 'Active', 1, 'ts', 0, 1, 0, 'all')
                """
            ),
            {"id": "legacy1", "password": "legacy-plaintext-pwd"},
        )

    await _migration_016_encrypt_xtream_passwords(db_engine)

    encrypted_once = await _raw_password(db_engine, "legacy1")
    assert encrypted_once != "legacy-plaintext-pwd"
    assert looks_encrypted(encrypted_once)

    # Idempotent: running the migration again must not touch the already
    # encrypted value (no double-encryption, no drift).
    await _migration_016_encrypt_xtream_passwords(db_engine)

    encrypted_twice = await _raw_password(db_engine, "legacy1")
    assert encrypted_twice == encrypted_once


async def test_already_encrypted_value_is_not_double_encrypted(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(settings, "XTREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())

    db_session.add(_account("acc3", "yet-another-secret"))
    await db_session.commit()

    token_before = await _raw_password(db_engine, "acc3")
    assert looks_encrypted(token_before)

    await _migration_016_encrypt_xtream_passwords(db_engine)

    token_after = await _raw_password(db_engine, "acc3")
    assert token_after == token_before
