"""Integration tests for POST /api/ai/assistant (F4 — catalog RAG assistant).

Uses an ASGI in-process client backed by an isolated sqlite-vec engine (M008).
embedding_service.embed_query and ollama_service.chat are monkeypatched so
tests stay fully offline.

Verifies:
  - Grounding: sources returned are the real seeded titles.
  - The messages list passed to ollama_service.chat contains those titles.
  - mediaType filter limits sources to that type.
  - Empty message -> 422.
  - Ollama down (httpx error) -> 503.
  - Missing X-API-Key -> 401.
  - camelCase aliases (tmdbId, mediaType) in the response.
"""
from __future__ import annotations

import struct
from typing import AsyncIterator

import httpx
import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import _VEC_LOADED, register_sqlite_vec_listener
from app.db.migrations import _migration_008_ai_embeddings
from app.services import embedding_service, ollama_service
from app.services.embedding_service import EMBEDDING_DIM

pytestmark = pytest.mark.asyncio


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _unit_vec(idx: int) -> list[float]:
    """Return a unit vector with 1.0 at position idx % EMBEDDING_DIM."""
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    arr[idx % EMBEDDING_DIM] = 1.0
    return arr.tolist()


def _serialize_vec(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


async def _seed_row(
    session: AsyncSession,
    *,
    tmdb_id: int,
    media_type: str,
    title: str,
    overview: str | None,
    vec: list[float],
) -> None:
    """Insert one row into ai_tmdb_cache and ai_embeddings."""
    now_ms = 1_700_000_000_000
    await session.execute(
        text(
            "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, "
            "overview, genres, fetched_at, embedded_at) "
            "VALUES(:tmdb_id, NULL, :media_type, :title, :overview, NULL, :now, :now)"
        ),
        {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": title,
            "overview": overview,
            "now": now_ms,
        },
    )
    await session.execute(
        text("INSERT INTO ai_embeddings(tmdb_id, embedding) VALUES(:tid, :v)"),
        {"tid": tmdb_id, "v": _serialize_vec(vec)},
    )
    await session.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def asst_engine(tmp_path):
    """Isolated file-backed sqlite-vec engine with M008 applied."""
    db_path = tmp_path / "asst_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def asst_factory(asst_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(asst_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def asst_client(
    asst_engine,
    asst_factory,
    monkeypatch,
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to the isolated AI engine; X-API-Key pre-seeded."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", "test-key-assistant")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    from app.main import app
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", asst_factory)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with asst_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[db_module.get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(db_module.get_db, None)


# Convenience fixture: client + 3 seeded rows (movie x2, tv x1).
# Vectors at indices 0, 1, 2 so query at index 0 ranks tmdb_id=10 first.
@pytest_asyncio.fixture
async def seeded_client(asst_client, asst_factory, monkeypatch):
    """Seed rows, monkeypatch embed_query + ollama chat; return (client, captured)."""
    async with asst_factory() as session:
        # tmdb_id=10: movie, vec at index 0 — closest to query
        await _seed_row(
            session,
            tmdb_id=10,
            media_type="movie",
            title="Inception",
            overview="Un voleur qui s'infiltre dans les rêves.",
            vec=_unit_vec(0),
        )
        # tmdb_id=20: movie, vec at index 1
        await _seed_row(
            session,
            tmdb_id=20,
            media_type="movie",
            title="Interstellar",
            overview="Des astronautes traversent un trou de ver.",
            vec=_unit_vec(1),
        )
        # tmdb_id=30: tv, vec at index 2
        await _seed_row(
            session,
            tmdb_id=30,
            media_type="tv",
            title="Dark",
            overview="Une petite ville allemande cache des secrets temporels.",
            vec=_unit_vec(2),
        )

    # Capture the messages sent to ollama_service.chat so we can assert grounding.
    captured: dict = {"messages": None}

    async def fake_embed_query(text: str) -> list[float]:
        return _unit_vec(0)

    async def fake_chat(messages: list[dict]) -> str:
        captured["messages"] = messages
        return "Je recommande Inception pour son ambiance onirique."

    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(embedding_service, "embed_query", fake_embed_query)
    monkeypatch.setattr(ollama_service, "chat", fake_chat)

    return asst_client, captured


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

async def test_missing_api_key_returns_401(asst_client):
    """No X-API-Key header must return 401."""
    resp = await asst_client.post(
        "/api/ai/assistant",
        json={"message": "Recommande-moi un film"},
    )
    assert resp.status_code == 401


async def test_empty_message_returns_422(asst_client, monkeypatch):
    """Empty message violates min_length=1 -> 422 Unprocessable Entity."""
    resp = await asst_client.post(
        "/api/ai/assistant",
        headers={"X-API-Key": "test-key-assistant"},
        json={"message": ""},
    )
    assert resp.status_code == 422


async def test_sources_are_real_seeded_titles(seeded_client):
    """sources returned must be real seeded titles, not hallucinated ones."""
    client, _captured = seeded_client
    resp = await client.post(
        "/api/ai/assistant",
        headers={"X-API-Key": "test-key-assistant"},
        json={"message": "Recommande-moi un film de science-fiction"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "reply" in body
    assert "sources" in body
    assert "model" in body

    source_ids = {s["tmdbId"] for s in body["sources"]}
    # All returned source ids must be among the seeded ids.
    assert source_ids <= {10, 20, 30}, f"unexpected source ids: {source_ids}"
    # At least one source returned.
    assert len(body["sources"]) >= 1


async def test_grounding_titles_appear_in_chat_messages(seeded_client):
    """The messages list passed to ollama chat must contain the retrieved titles."""
    client, captured = seeded_client
    resp = await client.post(
        "/api/ai/assistant",
        headers={"X-API-Key": "test-key-assistant"},
        json={"message": "Quel film recommandes-tu ?"},
    )
    assert resp.status_code == 200, resp.text

    messages = captured["messages"]
    assert messages is not None, "ollama_service.chat was not called"

    # The first message must be a system message containing the catalog context.
    assert messages[0]["role"] == "system"
    system_content = messages[0]["content"]

    # The retrieved titles must appear verbatim in the system prompt.
    source_ids = {s["tmdbId"] for s in resp.json()["sources"]}
    title_map = {10: "Inception", 20: "Interstellar", 30: "Dark"}
    for tid in source_ids:
        expected_title = title_map[tid]
        assert expected_title in system_content, (
            f"Title {expected_title!r} not found in system prompt:\n{system_content}"
        )

    # Last message must be the user message.
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Quel film recommandes-tu ?"


async def test_media_type_filter_limits_sources(seeded_client):
    """mediaType='tv' must return only tv sources."""
    client, _captured = seeded_client
    resp = await client.post(
        "/api/ai/assistant",
        headers={"X-API-Key": "test-key-assistant"},
        json={"message": "Recommande une série", "mediaType": "tv"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for source in body["sources"]:
        assert source["mediaType"] == "tv", f"Expected only tv but got {source}"

    # tmdb_id=30 (Dark, tv) must be present.
    assert any(s["tmdbId"] == 30 for s in body["sources"])

    # Movie ids must NOT appear.
    movie_ids = {s["tmdbId"] for s in body["sources"]} & {10, 20}
    assert not movie_ids, f"Movie sources leaked through filter: {movie_ids}"


async def test_ollama_down_returns_503(asst_client, asst_factory, monkeypatch):
    """If ollama_service.chat raises an httpx error, the endpoint must return 503."""
    async with asst_factory() as session:
        await _seed_row(
            session,
            tmdb_id=99,
            media_type="movie",
            title="TestFilm",
            overview="Test overview.",
            vec=_unit_vec(0),
        )

    async def fake_embed_query(text: str) -> list[float]:
        return _unit_vec(0)

    async def failing_chat(messages: list[dict]) -> str:
        raise httpx.ConnectError("Ollama unreachable")

    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(embedding_service, "embed_query", fake_embed_query)
    monkeypatch.setattr(ollama_service, "chat", failing_chat)

    resp = await asst_client.post(
        "/api/ai/assistant",
        headers={"X-API-Key": "test-key-assistant"},
        json={"message": "Recommande-moi un film"},
    )
    assert resp.status_code == 503
    assert "LLM unavailable" in resp.json()["detail"]


async def test_camelcase_aliases_in_response(seeded_client):
    """All response fields must use camelCase aliases (tmdbId, mediaType)."""
    client, _captured = seeded_client
    resp = await client.post(
        "/api/ai/assistant",
        headers={"X-API-Key": "test-key-assistant"},
        json={"message": "Quel film ?"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Top-level fields.
    assert "reply" in body
    assert "sources" in body
    assert "model" in body

    if body["sources"]:
        source = body["sources"][0]
        # camelCase aliases must be present.
        assert "tmdbId" in source, f"tmdbId missing from source: {source}"
        assert "mediaType" in source, f"mediaType missing from source: {source}"
        # snake_case must NOT appear.
        assert "tmdb_id" not in source
        assert "media_type" not in source


async def test_history_is_passed_to_chat(seeded_client):
    """Prior conversation history must appear in the messages list before the user turn."""
    client, captured = seeded_client
    history = [
        {"role": "user", "content": "Bonjour"},
        {"role": "assistant", "content": "Bonjour ! Comment puis-je vous aider ?"},
    ]
    resp = await client.post(
        "/api/ai/assistant",
        headers={"X-API-Key": "test-key-assistant"},
        json={"message": "Donne-moi une recommandation", "history": history},
    )
    assert resp.status_code == 200, resp.text

    messages = captured["messages"]
    assert messages is not None

    # The history turns must appear between the system message and the final user turn.
    roles = [m["role"] for m in messages]
    assert roles[0] == "system"
    assert roles[-1] == "user"

    # Both history turns must be present (in order).
    history_slice = messages[1:-1]
    assert len(history_slice) == 2
    assert history_slice[0]["role"] == "user"
    assert history_slice[0]["content"] == "Bonjour"
    assert history_slice[1]["role"] == "assistant"


async def test_limit_bounds_sources(asst_client, asst_factory, monkeypatch):
    """limit=2 must return at most 2 sources even when more rows exist."""
    async with asst_factory() as session:
        for i in range(5):
            await _seed_row(
                session,
                tmdb_id=100 + i,
                media_type="movie",
                title=f"Film {i}",
                overview=f"Overview {i}",
                vec=_unit_vec(i),
            )

    async def fake_embed_query(text: str) -> list[float]:
        return _unit_vec(0)

    async def fake_chat(messages: list[dict]) -> str:
        return "Réponse test."

    monkeypatch.setattr(embedding_service, "_model", object())
    monkeypatch.setattr(embedding_service, "embed_query", fake_embed_query)
    monkeypatch.setattr(ollama_service, "chat", fake_chat)

    resp = await asst_client.post(
        "/api/ai/assistant",
        headers={"X-API-Key": "test-key-assistant"},
        json={"message": "Films", "limit": 2},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["sources"]) <= 2
