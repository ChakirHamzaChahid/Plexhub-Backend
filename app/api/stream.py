from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.database import XtreamAccount
from app.models.schemas import StreamResponse
from app.services.stream_service import build_stream_url

router = APIRouter(tags=["stream"])


@router.get("/stream/{rating_key}", response_model=StreamResponse)
async def get_stream(
    rating_key: str,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    # Extract account_id from server_id
    if not server_id.startswith("xtream_"):
        raise HTTPException(400, "Invalid server_id format")

    account_id = server_id[7:]

    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    url = build_stream_url(account, rating_key)
    if not url:
        raise HTTPException(400, f"Cannot build stream URL for: {rating_key}")

    return StreamResponse(url=url)
