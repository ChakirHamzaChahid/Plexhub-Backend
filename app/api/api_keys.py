"""API-key management endpoints (JSON).

Mounted at ``/api/admin/keys`` and guarded by ``verify_master_key`` (only the
master secret ``AI_API_KEY`` may manage keys — a per-user key cannot). The
plaintext token is returned exactly once, by the create endpoint.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_master_key
from app.db.database import get_db
from app.models.database import ApiKey
from app.services import api_key_service
from app.utils.time import now_ms

router = APIRouter(
    prefix="/api/admin/keys",
    tags=["api-keys"],
    dependencies=[Depends(verify_master_key)],
)

_CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ApiKeyCreate(BaseModel):
    model_config = _CAMEL

    label: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    # First barrier only: api_key_service.create_key re-validates (ADR 0008
    # § D2.1). Keep in sync with api_key_service.KNOWN_SCOPES (asserted by
    # tests/test_api_key_scopes.py). Duplicates are deduplicated by the service.
    scopes: list[Literal["scrape", "scrape.admin"]] = Field(default_factory=list)


class ApiKeyOut(BaseModel):
    model_config = _CAMEL

    id: str
    label: str
    key_prefix: str
    status: str  # active | revoked | expired
    created_at: int
    expires_at: int | None = None
    revoked_at: int | None = None
    last_used_at: int | None = None
    last_used_ip: str | None = None
    # Stored values, as-is ([] for a key without scope). An unknown value read
    # from the DB is shown (it still confines the key) but grants nothing.
    scopes: list[str] = Field(default_factory=list)


class ApiKeyCreated(ApiKeyOut):
    key: str  # plaintext — shown ONCE


def _to_out(row: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=row.id,
        label=row.label,
        key_prefix=row.key_prefix,
        status=api_key_service.status_of(row),
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        last_used_ip=row.last_used_ip,
        scopes=(row.scopes or "").split(),
    )


@router.get("", response_model=list[ApiKeyOut], response_model_by_alias=True)
async def list_api_keys(db: AsyncSession = Depends(get_db)):
    rows = await api_key_service.list_keys(db)
    return [_to_out(r) for r in rows]


@router.post(
    "",
    response_model=ApiKeyCreated,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(body: ApiKeyCreate, db: AsyncSession = Depends(get_db)):
    expires_at = None
    if body.expires_in_days is not None:
        expires_at = now_ms() + body.expires_in_days * 86_400_000
    try:
        row, plaintext = await api_key_service.create_key(
            db, label=body.label, expires_at=expires_at, scopes=body.scopes
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    out = _to_out(row)
    return ApiKeyCreated(**out.model_dump(), key=plaintext)


@router.post("/{key_id}/revoke", response_model=ApiKeyOut, response_model_by_alias=True)
async def revoke_api_key(key_id: str, db: AsyncSession = Depends(get_db)):
    row = await api_key_service.revoke_key(db, key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="API key not found")
    return _to_out(row)


# DELETE is a soft-revoke too (keeps the audit row), for REST-style clients.
@router.delete("/{key_id}", response_model=ApiKeyOut, response_model_by_alias=True)
async def delete_api_key(key_id: str, db: AsyncSession = Depends(get_db)):
    row = await api_key_service.revoke_key(db, key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="API key not found")
    return _to_out(row)


__all__ = ["router"]
