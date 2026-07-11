"""Unit tests for app.services.account_service (CR-A01 extraction).

Exercises the extracted service functions directly (no HTTP layer), proving
the moved business logic (Xtream auth via the shared XtreamCredentials
dataclass, 6-table cascade delete, partial update) behaves the same in
isolation as it did inline in app/api/accounts.py. Endpoint-level coverage
for the same behavior lives in tests/test_accounts_retry.py.
"""
from __future__ import annotations

import pytest

from app.models.database import (
    XtreamAccount, Media, EnrichmentQueue, XtreamCategory, LiveChannel, EpgEntry,
)
from app.models.schemas import AccountCreate, AccountUpdate
from app.services import account_service
from app.services.xtream_credentials import XtreamCredentials
from app.services.xtream_service import xtream_service
from app.utils.server_id import build_server_id


def _account(id_: str = "a") -> XtreamAccount:
    return XtreamAccount(
        id=id_, label="Compte", base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


# ─── create_account ──────────────────────────────────────────────────────


async def test_create_account_authenticates_with_shared_credentials_dataclass(
    monkeypatch, db_session,
):
    """CR-C10: create_account must build a XtreamCredentials (not a throwaway
    anonymous class) and hand it to xtream_service.authenticate."""
    seen = {}

    async def _fake_authenticate(creds):
        seen["creds"] = creds
        return {
            "user_info": {
                "status": "Active", "exp_date": "1700000000",
                "max_connections": "2", "allowed_output_formats": ["ts", "m3u8"],
            },
            "server_info": {"url": "example.com", "https_port": "8443"},
        }

    monkeypatch.setattr(xtream_service, "authenticate", _fake_authenticate)

    body = AccountCreate(
        label="My IPTV", base_url="http://provider.example", port=8080,
        username="bob", password="secret",
    )
    account = await account_service.create_account(db_session, body)

    # Authenticated with the shared dataclass, carrying the submitted creds.
    assert isinstance(seen["creds"], XtreamCredentials)
    assert seen["creds"].base_url == "http://provider.example"
    assert seen["creds"].port == 8080
    assert seen["creds"].username == "bob"
    assert seen["creds"].password == "secret"

    # Persisted fields mirror the Xtream auth response.
    assert account.status == "Active"
    assert account.expiration_date == 1700000000 * 1000
    assert account.max_connections == 2
    assert account.allowed_formats == "ts,m3u8"
    assert account.server_url == "example.com"
    assert account.https_port == 8443
    assert account.is_active is True


async def test_create_account_duplicate_raises(monkeypatch, db_session):
    db_session.add(_account_for("http://dup.example", "bob"))
    await db_session.flush()

    body = AccountCreate(
        label="Dup", base_url="http://dup.example", port=80,
        username="bob", password="pw",
    )
    with pytest.raises(account_service.AccountAlreadyExistsError):
        await account_service.create_account(db_session, body)


async def test_create_account_auth_failure_raises(monkeypatch, db_session):
    async def _boom(creds):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(xtream_service, "authenticate", _boom)

    body = AccountCreate(
        label="X", base_url="http://unreachable.example", port=80,
        username="u", password="p",
    )
    with pytest.raises(account_service.AccountAuthenticationError, match="connection refused"):
        await account_service.create_account(db_session, body)


def _account_for(base_url: str, username: str) -> XtreamAccount:
    return XtreamAccount(
        id=account_service.generate_account_id(base_url, username),
        label="Compte", base_url=base_url, port=80,
        username=username, password="p", is_active=True, created_at=0,
    )


# ─── update_account ───────────────────────────────────────────────────────


async def test_update_account_not_found_raises(db_session):
    with pytest.raises(account_service.AccountNotFoundError):
        await account_service.update_account(
            db_session, "does-not-exist", AccountUpdate(label="New"),
        )


async def test_update_account_applies_partial_update(db_session):
    db_session.add(_account("a"))
    await db_session.flush()

    updated = await account_service.update_account(
        db_session, "a", AccountUpdate(label="Renamed"),
    )
    assert updated is not None
    assert updated.label == "Renamed"
    assert updated.username == "u"  # untouched field preserved


# ─── delete_account_cascade ───────────────────────────────────────────────


async def test_delete_account_cascade_not_found_raises(db_session):
    with pytest.raises(account_service.AccountNotFoundError):
        await account_service.delete_account_cascade(db_session, "nope")


async def test_delete_account_cascade_removes_all_related_rows(db_session):
    from sqlalchemy import select

    account_id = "a"
    server_id = build_server_id(account_id)

    db_session.add(_account(account_id))
    db_session.add(Media(
        rating_key="1", server_id=server_id, library_section_id="1",
        title="Movie", type="movie", added_at=0,
    ))
    db_session.add(EnrichmentQueue(
        rating_key="1", server_id=server_id, media_type="movie",
        title="Movie", created_at=0,
    ))
    db_session.add(XtreamCategory(
        account_id=account_id, category_id="1", category_type="vod",
        category_name="Action", last_fetched_at=0,
    ))
    db_session.add(LiveChannel(
        stream_id=1, server_id=server_id, name="Chan", added_at=0,
    ))
    db_session.add(EpgEntry(
        server_id=server_id, epg_channel_id="c1", stream_id=1,
        title="Show", start_time=0, end_time=1,
    ))
    await db_session.flush()

    await account_service.delete_account_cascade(db_session, account_id)
    await db_session.flush()

    assert (await db_session.execute(select(XtreamAccount))).scalars().first() is None
    assert (await db_session.execute(select(Media))).scalars().first() is None
    assert (await db_session.execute(select(EnrichmentQueue))).scalars().first() is None
    assert (await db_session.execute(select(XtreamCategory))).scalars().first() is None
    assert (await db_session.execute(select(LiveChannel))).scalars().first() is None
    assert (await db_session.execute(select(EpgEntry))).scalars().first() is None


# ─── test_account_connection ──────────────────────────────────────────────


async def test_test_account_connection_not_found_raises(db_session):
    with pytest.raises(account_service.AccountNotFoundError):
        await account_service.test_account_connection(db_session, "nope")


async def test_test_account_connection_returns_user_info(monkeypatch, db_session):
    db_session.add(_account("a"))
    await db_session.flush()

    async def _fake_authenticate(account):
        assert account.id == "a"
        return {"user_info": {"status": "Active", "max_connections": "3"}}

    monkeypatch.setattr(xtream_service, "authenticate", _fake_authenticate)

    user_info = await account_service.test_account_connection(db_session, "a")
    assert user_info == {"status": "Active", "max_connections": "3"}


async def test_test_account_connection_auth_failure_raises(monkeypatch, db_session):
    db_session.add(_account("a"))
    await db_session.flush()

    async def _boom(account):
        raise RuntimeError("timeout")

    monkeypatch.setattr(xtream_service, "authenticate", _boom)

    with pytest.raises(account_service.AccountAuthenticationError, match="timeout"):
        await account_service.test_account_connection(db_session, "a")
