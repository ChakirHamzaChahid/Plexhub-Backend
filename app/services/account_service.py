"""Account management service: creation/auth, update, cascade delete.

Extracted from app/api/accounts.py (CR-A01) — routers validate + delegate,
business logic lives here. Domain errors are plain exceptions (no FastAPI
dependency in services/); the router maps them to the exact same
HTTPException status/detail it raised inline before the move.

Callers own the transaction boundary: these functions stage writes
(``db.add``/``db.execute``) but do NOT commit — the router still calls
``commit_with_retry`` itself (CR-C04), so the existing lock-retry guard
tests that monkeypatch ``app.api.accounts.commit_with_retry`` keep working
unchanged.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    XtreamAccount, Media, EnrichmentQueue, XtreamCategory, LiveChannel, EpgEntry,
)
from app.models.schemas import AccountCreate, AccountUpdate
from app.services.xtream_credentials import XtreamCredentials
from app.services.xtream_service import xtream_service
from app.utils.server_id import build_server_id
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.services.account")


class AccountAlreadyExistsError(Exception):
    """Raised when an account with the same (base_url, username) already exists."""


class AccountAuthenticationError(Exception):
    """Raised when Xtream authentication fails (create or test-connection)."""


class AccountNotFoundError(Exception):
    """Raised when account_id doesn't resolve to an existing XtreamAccount."""


def generate_account_id(base_url: str, username: str) -> str:
    raw = f"{base_url}{username}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


async def create_account(db: AsyncSession, body: AccountCreate) -> XtreamAccount:
    """Authenticate against Xtream and stage a new account row.

    Raises:
        AccountAlreadyExistsError: an account with this (base_url, username) exists.
        AccountAuthenticationError: Xtream authentication failed.
    """
    account_id = generate_account_id(body.base_url, body.username)

    existing = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    if existing.scalars().first():
        raise AccountAlreadyExistsError(account_id)

    credentials = XtreamCredentials(
        base_url=body.base_url,
        port=body.port,
        username=body.username,
        password=body.password,
    )
    try:
        auth_data = await xtream_service.authenticate(credentials)
        user_info = auth_data.get("user_info", {})
        server_info = auth_data.get("server_info", {})
    except Exception as e:
        raise AccountAuthenticationError(str(e)) from e

    account = XtreamAccount(
        id=account_id,
        label=body.label,
        base_url=body.base_url,
        port=body.port,
        username=body.username,
        password=body.password,
        status=user_info.get("status", "Unknown"),
        expiration_date=int(user_info["exp_date"]) * 1000
        if user_info.get("exp_date")
        else None,
        max_connections=int(user_info.get("max_connections", 1)),
        allowed_formats=",".join(user_info.get("allowed_output_formats", [])),
        server_url=server_info.get("url"),
        https_port=int(server_info["https_port"])
        if server_info.get("https_port")
        else None,
        is_active=True,
        created_at=now_ms(),
    )

    db.add(account)
    return account


async def update_account(
    db: AsyncSession, account_id: str, body: AccountUpdate,
) -> XtreamAccount | None:
    """Apply a partial update to an account and return the refreshed row.

    Raises:
        AccountNotFoundError: no account with this id.
    """
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise AccountNotFoundError(account_id)

    update_data = body.model_dump(exclude_unset=True)
    if update_data:
        await db.execute(
            update(XtreamAccount)
            .where(XtreamAccount.id == account_id)
            .values(**update_data)
        )
        await db.flush()

    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    return result.scalars().first()


async def delete_account_cascade(db: AsyncSession, account_id: str) -> None:
    """Delete an account and every row keyed on its server_id/account_id.

    Raises:
        AccountNotFoundError: no account with this id.
    """
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    if not result.scalars().first():
        raise AccountNotFoundError(account_id)

    server_id = build_server_id(account_id)
    await db.execute(delete(Media).where(Media.server_id == server_id))
    await db.execute(
        delete(EnrichmentQueue).where(EnrichmentQueue.server_id == server_id)
    )
    await db.execute(
        delete(XtreamCategory).where(XtreamCategory.account_id == account_id)
    )
    await db.execute(delete(LiveChannel).where(LiveChannel.server_id == server_id))
    await db.execute(delete(EpgEntry).where(EpgEntry.server_id == server_id))
    await db.execute(delete(XtreamAccount).where(XtreamAccount.id == account_id))


async def test_account_connection(db: AsyncSession, account_id: str) -> dict:
    """Re-authenticate against Xtream using the account's stored credentials.

    Returns the raw ``user_info`` dict for the router to shape into
    AccountTestResponse.

    Raises:
        AccountNotFoundError: no account with this id.
        AccountAuthenticationError: Xtream authentication failed.
    """
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise AccountNotFoundError(account_id)

    try:
        auth_data = await xtream_service.authenticate(account)
    except Exception as e:
        raise AccountAuthenticationError(str(e)) from e

    return auth_data.get("user_info", {})
