"""Shared fixtures/helpers of the metadata API tests (lot B2).

Import the fixtures into a test module (`from tests._metadata_support import wire, keys, client`)
and keep `wire` autouse there. Keys are factice test keys only.
"""
from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import database as db_module
from app.models.database import ApiKey, Media
from app.services import api_key_service
from app.utils.time import now_ms

MASTER = "test-master-key-metadata-api"
SERVER = "xtream_aaaa1111"
BASE = "/api/metadata/v1"

_page_offset = itertools.count(1)


async def make_key(db_factory, *, scopes: str = "", revoked: bool = False, expired: bool = False) -> str:
    plaintext = api_key_service.generate_token()
    now = now_ms()
    async with db_factory() as db:
        db.add(ApiKey(
            id=uuid.uuid4().hex, key_hash=api_key_service._hash(plaintext),
            key_prefix=plaintext[:10], label="t", created_at=now - 10_000,
            expires_at=(now - 1_000) if expired else None,
            revoked_at=(now - 1_000) if revoked else None, scopes=scopes,
        ))
        await db.commit()
    return plaintext


@pytest_asyncio.fixture
async def wire(monkeypatch, db_factory):
    from app.api import deps  # noqa: F401  (import for monkeypatch side effects)

    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER)
    return db_factory


@pytest_asyncio.fixture
async def keys(wire):
    db_factory = wire
    return {
        "master": MASTER,
        "read": await make_key(db_factory, scopes="media.read"),
        "write": await make_key(db_factory, scopes="media.write"),
        "rw": await make_key(db_factory, scopes="media.read media.write"),
        "scrape_read": await make_key(db_factory, scopes="media.read scrape"),
        "scrape": await make_key(db_factory, scopes="scrape"),
        "none": await make_key(db_factory),
        "revoked": await make_key(db_factory, scopes="media.read media.write", revoked=True),
        "expired": await make_key(db_factory, scopes="media.read media.write", expired=True),
    }


@pytest_asyncio.fixture
async def client(api_client, wire):
    return api_client


def h(key: str | None, **extra: str) -> dict[str, str]:
    headers = {} if key is None else {"X-API-Key": key}
    headers.update(extra)
    return headers


def write_headers(key: str, revision: str, idem: str = "idem-1", **extra: str) -> dict[str, str]:
    return h(key, **{"If-Match": f'"{revision}"', "Idempotency-Key": idem, **extra})


async def seed(
    db_factory,
    *,
    rating_key: str = "vod_1.mkv",
    server_id: str = SERVER,
    type: str = "movie",
    variants: tuple[tuple[str, str], ...] = (("all", "default"),),
    **cols: Any,
) -> None:
    """Insert one `media` row per `(filter, sort_order)` variant (distinct page offsets)."""
    async with db_factory() as db:
        for filter_, sort_order in variants:
            row = dict(
                rating_key=rating_key, server_id=server_id, filter=filter_, sort_order=sort_order,
                library_section_id="xtream_vod" if type == "movie" else "xtream_series",
                title="The Matrix", title_sortable="the matrix", page_offset=next(_page_offset),
                type=type, year=1999, summary="old", genres="Action",
                unification_id="title_the_matrix_1999", history_group_key="title_the_matrix_1999",
                display_rating=6.0,
            )
            row.update(cols)
            db.add(Media(**row))
        await db.commit()


async def fetch(db_factory, rating_key: str = "vod_1.mkv", server_id: str = SERVER) -> list[Media]:
    async with db_factory() as db:
        rows = (await db.execute(
            select(Media).where(Media.rating_key == rating_key, Media.server_id == server_id)
            .order_by(Media.filter, Media.sort_order)
        )).scalars().all()
        for r in rows:
            db.expunge(r)
        return list(rows)


async def count_edits(db_factory) -> int:
    from sqlalchemy import func

    from app.models.database import MetadataEdit

    async with db_factory() as db:
        session: AsyncSession = db
        return (await session.execute(select(func.count()).select_from(MetadataEdit))).scalar_one()
