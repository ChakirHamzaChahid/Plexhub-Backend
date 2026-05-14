"""Integration tests for migration M008 + sqlite-vec loader binding."""
from __future__ import annotations

import struct

import pytest
from sqlalchemy import text

from app.db.database import _VEC_LOADED
from app.db.migrations import _migration_008_ai_embeddings


pytestmark = pytest.mark.asyncio
pytest_plugins = ["tests.conftest_ai"]


def _serialize_vec(values: list[float]) -> bytes:
    """Pack a Python float list into sqlite-vec's expected little-endian float32 blob."""
    try:
        from sqlite_vec import serialize_float32  # type: ignore

        return serialize_float32(values)
    except Exception:
        return struct.pack(f"<{len(values)}f", *values)


async def test_vec_loaded_after_init(ai_engine) -> None:
    """vec_version() must return a non-empty string and INSERT/SELECT must round-trip."""
    async with ai_engine.connect() as conn:
        version = (await conn.execute(text("SELECT vec_version()"))).scalar()
        assert version is not None and len(str(version)) > 0

        await conn.execute(
            text("INSERT INTO ai_embeddings(tmdb_id, embedding) VALUES (1, :v)"),
            {"v": _serialize_vec([0.1] * 384)},
        )
        await conn.commit()

        row = (
            await conn.execute(
                text("SELECT tmdb_id FROM ai_embeddings WHERE tmdb_id = 1")
            )
        ).fetchone()
        assert row is not None and row[0] == 1

    assert _VEC_LOADED["ok"] is True


async def test_m008_idempotent(ai_engine) -> None:
    """Re-running M008 on an engine where it already ran must not raise."""
    async with ai_engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)
