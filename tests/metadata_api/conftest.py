"""Fixtures of the metadata API tests (lot B2): wired DB, factice keys, ASGI client."""
from __future__ import annotations

import pytest_asyncio

from app.config import settings
from app.db import database as db_module
from app.services import api_key_service
from tests._metadata_support import MASTER, make_key


@pytest_asyncio.fixture
async def wire(monkeypatch, db_factory):
    """Point every session factory at the in-memory test DB and set a factice master key."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER)
    return db_factory


@pytest_asyncio.fixture
async def keys(wire):
    return {
        "master": MASTER,
        "read": await make_key(wire, scopes="media.read"),
        "write": await make_key(wire, scopes="media.write"),
        "rw": await make_key(wire, scopes="media.read media.write"),
        "scrape_read": await make_key(wire, scopes="media.read scrape"),
        "scrape": await make_key(wire, scopes="scrape"),
        "none": await make_key(wire),
        "revoked": await make_key(wire, scopes="media.read media.write", revoked=True),
        "expired": await make_key(wire, scopes="media.read media.write", expired=True),
    }


@pytest_asyncio.fixture
async def client(api_client, wire):
    return api_client
