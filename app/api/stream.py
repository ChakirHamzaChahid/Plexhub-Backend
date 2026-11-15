from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.schemas import StreamResponse
from app.services.stream_service import (
    InvalidStreamServerIdError,
    StreamAccountNotFoundError,
    UnbuildableStreamUrlError,
    resolve_stream_url,
)

router = APIRouter(tags=["stream"])


@router.get("/stream/{rating_key}", response_model=StreamResponse)
async def get_stream(
    rating_key: str,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    try:
        url = await resolve_stream_url(db, rating_key, server_id)
    except InvalidStreamServerIdError as exc:
        raise HTTPException(400, str(exc))
    except StreamAccountNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except UnbuildableStreamUrlError as exc:
        raise HTTPException(400, str(exc))
    return StreamResponse(url=url)
