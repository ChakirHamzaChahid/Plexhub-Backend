import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.database import XtreamAccount
from app.models.schemas import (
    AccountCreate,
    AccountUpdate,
    AccountResponse,
    AccountTestResponse,
)
from app.services import account_service
from app.utils.db_retry import commit_with_retry
from app.utils.tasks import create_background_task

logger = logging.getLogger("plexhub.api.accounts")
router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(XtreamAccount))
    return result.scalars().all()


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    body: AccountCreate, db: AsyncSession = Depends(get_db),
):
    try:
        account = await account_service.create_account(db, body)
    except account_service.AccountAlreadyExistsError:
        raise HTTPException(409, "Account already exists")
    except account_service.AccountAuthenticationError as e:
        raise HTTPException(400, f"Authentication failed: {e}")

    # CR-C04: retry on "database is locked" — a sync/validation cycle can be
    # holding the single WAL writer when a new account is created.
    await commit_with_retry(db)

    # Trigger initial sync in background (after commit so the task can find the account)
    from app.workers.sync_worker import sync_account
    create_background_task(sync_account(account.id), name=f"sync_{account.id}")

    return account


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str, body: AccountUpdate, db: AsyncSession = Depends(get_db),
):
    try:
        updated = await account_service.update_account(db, account_id, body)
    except account_service.AccountNotFoundError:
        raise HTTPException(404, "Account not found")

    # CR-C04: this endpoint previously relied on get_db's implicit commit on
    # successful return (db/database.py get_db), which is NOT retried. Commit
    # explicitly here so the write gets the same lock-retry as the workers.
    await commit_with_retry(db)
    return updated


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: str, db: AsyncSession = Depends(get_db),
):
    try:
        await account_service.delete_account_cascade(db, account_id)
    except account_service.AccountNotFoundError:
        raise HTTPException(404, "Account not found")

    # CR-C04: this endpoint previously relied on get_db's implicit commit on
    # successful return (db/database.py get_db), which is NOT retried. This
    # is a multi-table cascade delete — commit explicitly with lock-retry.
    await commit_with_retry(db)


@router.post("/{account_id}/test", response_model=AccountTestResponse)
async def test_account(
    account_id: str, db: AsyncSession = Depends(get_db),
):
    try:
        user_info = await account_service.test_account_connection(db, account_id)
    except account_service.AccountNotFoundError:
        raise HTTPException(404, "Account not found")
    except account_service.AccountAuthenticationError as e:
        raise HTTPException(400, f"Connection test failed: {e}")

    # Building the response parses provider-supplied fields (exp_date,
    # max_connections, …). Malformed provider data → 400, same as the old
    # inline try/except that wrapped this block (behavior parity, CR-A01).
    try:
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
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f"Connection test failed: {e}")
