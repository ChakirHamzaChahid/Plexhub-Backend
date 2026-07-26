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
from app.utils.db_retry import commit_with_retry, write_with_retry
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

    # AUDIT-P1-001 (S3.3) — deliberately NOT converted to write_with_retry.
    # account_service.create_account mixes a real network call
    # (xtream_service.authenticate against the operator-supplied provider)
    # with the staged `db.add(account)` in one function; write_with_retry's
    # contract requires `work` to be fully replayable, so wrapping the whole
    # call would re-authenticate against the Xtream provider on every lock
    # retry (up to 4x) — an external side effect, and one that can burn a
    # connection slot on `max_connections`-limited accounts. Splitting
    # "authenticate" from "stage+commit" would require restructuring
    # app/services/account_service.py, which is out of this zone (S3.2).
    # commit_with_retry (now honest per ADR 0004) stays: on a real lock it
    # raises the true OperationalError instead of retrying — acceptable here
    # since account creation is a rare, operator-driven action, not a
    # concurrent-writer hot path.
    await commit_with_retry(db)

    # Trigger initial sync in background (after commit so the task can find the account)
    from app.workers.sync_worker import sync_account
    create_background_task(sync_account(account.id), name=f"sync_{account.id}")

    return account


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str, body: AccountUpdate, db: AsyncSession = Depends(get_db),
):
    # AUDIT-P1-001 (S3.3): converted to write_with_retry — a fresh session per
    # attempt is the only pattern that actually survives a real SQLite lock
    # (ADR 0004, Decision 4). account_service.update_account is a plain
    # read-then-UPDATE-by-id with no external side effect, so replaying the
    # whole call on a lock retry is safe and idempotent (same values
    # re-applied). `db` (the request-scoped session from Depends(get_db))
    # is left untouched here — no write, so get_db's own implicit commit at
    # the end of the request is a harmless no-op, not a second write of the
    # same row.
    async def _work(session: AsyncSession):
        updated = await account_service.update_account(session, account_id, body)
        await session.commit()
        return updated

    try:
        updated = await write_with_retry(_work, op="accounts.update_account")
    except account_service.AccountNotFoundError:
        raise HTTPException(404, "Account not found")
    return updated


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: str, db: AsyncSession = Depends(get_db),
):
    # AUDIT-P1-001 (S3.3): converted to write_with_retry. The cascade delete
    # (account_service.delete_account_cascade) is a sequence of DELETE ...
    # WHERE statements with no external side effect and no dependency on a
    # prior read outside this call — replaying the whole cascade on a lock
    # retry is idempotent (deleting an already-absent row is a no-op). `db`
    # (request session) is left untouched, so get_db's own implicit commit
    # at request end has nothing pending to flush.
    async def _work(session: AsyncSession):
        await account_service.delete_account_cascade(session, account_id)
        await session.commit()

    try:
        await write_with_retry(_work, op="accounts.delete_account")
    except account_service.AccountNotFoundError:
        raise HTTPException(404, "Account not found")


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
