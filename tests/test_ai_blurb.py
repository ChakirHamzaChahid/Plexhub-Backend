"""Integration tests for POST /api/ai/blurb and GET /api/ai/blurb/{tmdb_id} (F3).

Acceptance criteria:
  TC-01  POST generates + persists; response is camelCase; cached=false first call,
         cached=true second call without re-calling generate.
  TC-02  force=true regenerates even when a cached row exists.
  TC-03  Source-text resolution: title/overview absent from request but present in
         ai_tmdb_cache → still generates.
  TC-04  No source text anywhere → 422.
  TC-05  Malformed LLM JSON → graceful fallback (summary non-empty, tags=[]), 200.
  TC-06  Ollama down → 503.
  TC-07  GET returns cached blurb; GET for ungenerated item → 404.
  TC-08  Missing X-API-Key → 401.
  TC-09  M011 idempotency: ai_media_blurb table exists after fixture setup.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import _VEC_LOADED, register_sqlite_vec_listener
from app.db.migrations import _migration_008_ai_embeddings, _migration_012_create_media_blurb
from app.models.database import Base
from app.services import ollama_service

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_API_KEY = "test-key-blurb"


@pytest_asyncio.fixture
async def blurb_engine(tmp_path):
    """File-backed async engine: sqlite-vec (M008) + blurb table (M011) + ORM tables."""
    db_path = tmp_path / "blurb_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)
    # M011 operates on the engine (not a bare connection), mirroring M010
    await _migration_012_create_media_blurb(engine)
    # Ensure all ORM-mapped tables are also present (AiMediaBlurb, AiSubtitleCache, etc.)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def blurb_factory(blurb_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(blurb_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def blurb_client(
    blurb_engine,
    blurb_factory,
    monkeypatch,
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    """Authenticated ASGI client wired to the blurb test DB."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", _API_KEY)
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    from app.main import app
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", blurb_factory)
    from app.api import ai as ai_mod
    monkeypatch.setattr(ai_mod, "async_session_factory", blurb_factory)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with blurb_factory() as session:
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


# ---------------------------------------------------------------------------
# Fake generate helpers
# ---------------------------------------------------------------------------

def _make_good_generate(summary: str = "Un film captivant.", tags: list | None = None):
    """Return a generate stub that produces valid JSON."""
    _tags = tags if tags is not None else ["drame", "émouvant", "intense"]

    async def fake_generate(prompt: str) -> str:
        return json.dumps({"summary": summary, "tags": _tags}, ensure_ascii=False)

    return fake_generate


def _make_bad_json_generate():
    """Return a generate stub that always produces unparseable output."""
    async def fake_generate(prompt: str) -> str:
        return "Voici un film magnifique avec des paysages époustouflants et une musique envoûtante."
    return fake_generate


def _make_error_generate():
    """Return a generate stub that raises httpx.ConnectError (Ollama down)."""
    async def fake_generate(prompt: str) -> str:
        raise httpx.ConnectError("Connection refused")
    return fake_generate


# ---------------------------------------------------------------------------
# TC-01 — POST generates + persists; camelCase; cached=false then cached=true
# ---------------------------------------------------------------------------

async def test_post_generates_and_caches(blurb_client, monkeypatch):
    call_count = 0

    async def counting_generate(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        return json.dumps({"summary": "Synopsis généré.", "tags": ["action", "thriller"]})

    monkeypatch.setattr(ollama_service, "generate", counting_generate)

    payload = {
        "tmdbId": 1001,
        "mediaType": "movie",
        "title": "Le Film",
        "overview": "Un homme part en aventure.",
        "genres": "Action, Thriller",
    }
    headers = {"X-API-Key": _API_KEY}

    # First call — cache miss, generate is called
    r1 = await blurb_client.post("/api/ai/blurb", headers=headers, json=payload)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()

    # camelCase keys
    assert "tmdbId" in body1
    assert "mediaType" in body1
    assert "summary" in body1
    assert "tags" in body1
    assert "cached" in body1
    assert "model" in body1

    assert body1["tmdbId"] == 1001
    assert body1["mediaType"] == "movie"
    assert body1["cached"] is False
    assert body1["summary"] == "Synopsis généré."
    assert isinstance(body1["tags"], list)
    assert "action" in body1["tags"]
    calls_after_first = call_count

    # Second call (same key, no force) — cache hit, generate NOT called again
    r2 = await blurb_client.post("/api/ai/blurb", headers=headers, json=payload)
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["cached"] is True
    assert body2["summary"] == "Synopsis généré."
    assert call_count == calls_after_first, "generate must NOT be called on cache hit"


# ---------------------------------------------------------------------------
# TC-02 — force=true regenerates even when cached
# ---------------------------------------------------------------------------

async def test_force_regenerates(blurb_client, monkeypatch):
    call_count = 0

    async def counting_generate(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        return json.dumps({"summary": f"Synopsis #{call_count}.", "tags": ["sci-fi"]})

    monkeypatch.setattr(ollama_service, "generate", counting_generate)

    headers = {"X-API-Key": _API_KEY}
    payload = {
        "tmdbId": 2002,
        "mediaType": "tv",
        "title": "La Série",
        "overview": "Une série spatiale.",
        "genres": "Sci-Fi",
    }

    # Seed the cache with one call
    r1 = await blurb_client.post("/api/ai/blurb", headers=headers, json=payload)
    assert r1.status_code == 200
    assert r1.json()["cached"] is False
    after_first = call_count

    # force=true must call generate again
    r2 = await blurb_client.post(
        "/api/ai/blurb", headers=headers, json={**payload, "force": True}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["cached"] is False
    assert call_count > after_first, "generate must be called when force=true"


# ---------------------------------------------------------------------------
# TC-03 — Source-text resolution from ai_tmdb_cache when request has none
# ---------------------------------------------------------------------------

async def test_source_from_tmdb_cache(blurb_engine, blurb_client, monkeypatch):
    """No title/overview in the POST body, but ai_tmdb_cache has data → should generate."""
    monkeypatch.setattr(ollama_service, "generate", _make_good_generate("Synopsis cache."))

    # Pre-seed ai_tmdb_cache for tmdb_id=3003
    async with blurb_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO ai_tmdb_cache(tmdb_id, media_type, title, overview, genres, fetched_at) "
            "VALUES(3003, 'movie', 'Cached Title', 'Cached overview text.', 'Drama', 0)"
        ))

    r = await blurb_client.post(
        "/api/ai/blurb",
        headers={"X-API-Key": _API_KEY},
        json={"tmdbId": 3003, "mediaType": "movie"},  # no title/overview/genres
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == "Synopsis cache."
    assert body["cached"] is False


# ---------------------------------------------------------------------------
# TC-04 — No source text anywhere → 422
# ---------------------------------------------------------------------------

async def test_no_source_text_422(blurb_client, monkeypatch):
    monkeypatch.setattr(ollama_service, "generate", _make_good_generate())

    r = await blurb_client.post(
        "/api/ai/blurb",
        headers={"X-API-Key": _API_KEY},
        # tmdb_id 9999 is not in ai_tmdb_cache and no title/overview provided
        json={"tmdbId": 9999, "mediaType": "movie"},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# TC-05 — Malformed LLM JSON → graceful fallback (summary non-empty, tags=[])
# ---------------------------------------------------------------------------

async def test_malformed_json_fallback(blurb_client, monkeypatch):
    monkeypatch.setattr(ollama_service, "generate", _make_bad_json_generate())

    r = await blurb_client.post(
        "/api/ai/blurb",
        headers={"X-API-Key": _API_KEY},
        json={
            "tmdbId": 4004,
            "mediaType": "movie",
            "title": "Fallback Film",
            "overview": "Un film mystérieux.",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]        # non-empty
    assert isinstance(body["tags"], list)
    assert body["tags"] == []     # fallback: empty tags


# ---------------------------------------------------------------------------
# TC-06 — Ollama down → 503
# ---------------------------------------------------------------------------

async def test_ollama_down_503(blurb_client, monkeypatch):
    monkeypatch.setattr(ollama_service, "generate", _make_error_generate())

    r = await blurb_client.post(
        "/api/ai/blurb",
        headers={"X-API-Key": _API_KEY},
        json={
            "tmdbId": 5005,
            "mediaType": "movie",
            "title": "Unreachable",
            "overview": "Some overview.",
        },
    )
    assert r.status_code == 503, r.text


# ---------------------------------------------------------------------------
# TC-07 — GET returns cached blurb; GET for ungenerated item → 404
# ---------------------------------------------------------------------------

async def test_get_returns_cached_and_404_when_missing(blurb_client, monkeypatch):
    monkeypatch.setattr(ollama_service, "generate", _make_good_generate("Synopsis GET."))

    headers = {"X-API-Key": _API_KEY}

    # Seed a blurb via POST
    r_post = await blurb_client.post(
        "/api/ai/blurb",
        headers=headers,
        json={
            "tmdbId": 6006,
            "mediaType": "movie",
            "title": "GET Film",
            "overview": "For GET test.",
        },
    )
    assert r_post.status_code == 200

    # GET the cached blurb
    r_get = await blurb_client.get(
        "/api/ai/blurb/6006",
        headers=headers,
        params={"mediaType": "movie", "lang": "fr"},
    )
    assert r_get.status_code == 200, r_get.text
    body = r_get.json()
    assert body["tmdbId"] == 6006
    assert body["mediaType"] == "movie"
    assert body["cached"] is True
    assert body["summary"] == "Synopsis GET."

    # GET for a tmdb_id that has never been generated → 404
    r_404 = await blurb_client.get(
        "/api/ai/blurb/7777",
        headers=headers,
        params={"mediaType": "movie"},
    )
    assert r_404.status_code == 404, r_404.text


# ---------------------------------------------------------------------------
# TC-08 — Missing X-API-Key → 401
# ---------------------------------------------------------------------------

async def test_missing_auth_401(blurb_client):
    r_post = await blurb_client.post(
        "/api/ai/blurb",
        json={"tmdbId": 1, "mediaType": "movie", "title": "T", "overview": "O"},
    )
    assert r_post.status_code == 401, r_post.text

    r_get = await blurb_client.get(
        "/api/ai/blurb/1",
        params={"mediaType": "movie"},
    )
    assert r_get.status_code == 401, r_get.text


# ---------------------------------------------------------------------------
# TC-09 — M011 idempotency: ai_media_blurb table exists after fixture setup
# ---------------------------------------------------------------------------

async def test_m011_table_exists(blurb_engine):
    """Running M011 twice must not raise; table must be queryable."""
    # Run M011 a second time — must be a no-op (idempotent)
    await _migration_012_create_media_blurb(blurb_engine)

    async with blurb_engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM ai_media_blurb"))
        count = result.scalar()
    assert count == 0  # empty table, but exists
