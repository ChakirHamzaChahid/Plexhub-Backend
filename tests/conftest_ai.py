"""Isolated pytest fixtures for AI/embedding tests.

Kept in a dedicated module (not the global tests/conftest.py) so the
sqlite-vec extension loader is only attached when AI tests opt in via
`pytest_plugins = ["tests.conftest_ai"]`. The default in-memory `db_engine`
fixture in tests/conftest.py remains untouched.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import register_sqlite_vec_listener
from app.db.migrations import _migration_008_ai_embeddings


@pytest_asyncio.fixture
async def ai_engine(tmp_path):
    """File-backed async engine with sqlite-vec loaded and M008 applied."""
    db_path = tmp_path / "ai_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def ai_sessionmaker(ai_engine) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker bound to the per-test ai_engine."""
    return async_sessionmaker(ai_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def ai_db_session(ai_sessionmaker) -> AsyncIterator[AsyncSession]:
    """One AsyncSession per test against the AI engine."""
    async with ai_sessionmaker() as session:
        yield session
