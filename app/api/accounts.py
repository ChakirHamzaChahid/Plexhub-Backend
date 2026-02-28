import asyncio
import hashlib
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.database import XtreamAccount
from app.models.schemas import (
    AccountCreate,
    AccountUpdate,
    AccountResponse,
    AccountTestResponse,
)
from app.services.xtream_service import xtream_service

logger = logging.getLogger("plexhub.api.accounts")
router = APIRouter(prefix="/accounts", tags=["accounts"])


def _generate_account_id(base_url: str, username: str) -> str:
    raw = f"{base_url}{username}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def now_ms() -> int:
    return int(time.time() * 1000)


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(XtreamAccount))
    return result.scalars().all()


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    body: AccountCreate, db: AsyncSession = Depends(get_db),
):
    account_id = _generate_account_id(body.base_url, body.username)

    # Check if exists
    existing = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    if existing.scalars().first():
        raise HTTPException(409, "Account already exists")

    # Create account object for auth test
    class TempAccount:
        pass
    temp = TempAccount()
    temp.base_url = body.base_url
    temp.port = body.port
    temp.username = body.username
    temp.password = body.password

    # Authenticate with Xtream
    try:
        auth_data = await xtream_service.authenticate(temp)
        user_info = auth_data.get("user_info", {})
        server_info = auth_data.get("server_info", {})
    except Exception as e:
        raise HTTPException(400, f"Authentication failed: {e}")

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
    await db.flush()

    # Trigger initial sync in background
    from app.workers.sync_worker import sync_account
    asyncio.create_task(sync_account(account_id))

    return account


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str, body: AccountUpdate, db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    update_data = body.model_dump(exclude_unset=True)
    if update_data:
        await db.execute(
            update(XtreamAccount)
            .where(XtreamAccount.id == account_id)
            .values(**update_data)
        )
        await db.flush()

    # Reload
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    return result.scalars().first()


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: str, db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    if not result.scalars().first():
        raise HTTPException(404, "Account not found")

    # Delete account and its media
    from app.models.database import Media, EnrichmentQueue

    server_id = f"xtream_{account_id}"
    await db.execute(delete(Media).where(Media.server_id == server_id))
    await db.execute(
        delete(EnrichmentQueue).where(EnrichmentQueue.server_id == server_id)
    )
    await db.execute(
        delete(XtreamAccount).where(XtreamAccount.id == account_id)
    )


@router.post("/{account_id}/test", response_model=AccountTestResponse)
async def test_account(
    account_id: str, db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    try:
        auth_data = await xtream_service.authenticate(account)
        user_info = auth_data.get("user_info", {})

        return AccountTestResponse(
            status=user_info.get("status", "Unknown"),
            expiration_date=int(user_info["exp_date"]) * 1000
            if user_info.get("exp_date")
            else None,
            max_connections=int(user_info.get("max_connections", 1)),
            allowed_formats=",".join(
                user_info.get("allowed_output_formats", [])
            ),
        )
    except Exception as e:
        raise HTTPException(400, f"Connection test failed: {e}")
