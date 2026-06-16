"""Integration tests for POST /api/ai/subtitles/translate (WP5).

Covers acceptance criteria:
  TC-01  SRT happy path — 200, cached=False, cueCount, format, timecodes preserved,
         text translated, camelCase response keys.
  TC-02  Cache hit — second POST returns cached=True and does NOT invoke generate.
  TC-03  VTT happy path — WEBVTT header preserved, format=="vtt".
  TC-04  Malformed input → 422.
  TC-05  Too-large input → 413 (monkeypatch SUBTITLE_MAX_CUES).
  TC-06  Alignment fallback — wrong line count from LLM → 200, no cue dropped,
         original text retained.
  TC-07  Ollama down (httpx.ConnectError) → 503.
  TC-08  Auth — missing X-API-Key → 401.
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import _VEC_LOADED, register_sqlite_vec_listener
from app.db.migrations import _migration_008_ai_embeddings, _migration_010_create_subtitle_cache
from app.models.database import Base
from app.services import ollama_service

# ---------------------------------------------------------------------------
# Sample subtitle constants
# ---------------------------------------------------------------------------

SRT_2_CUES = """\
1
00:00:01,000 --> 00:00:02,000
Hello world

2
00:00:03,000 --> 00:00:04,000
Goodbye world
"""

SRT_3_CUES = """\
1
00:00:01,000 --> 00:00:02,000
First line

2
00:00:03,000 --> 00:00:04,000
Second line

3
00:00:05,000 --> 00:00:06,000
Third line
"""

VTT_2_CUES = """\
WEBVTT

00:00:01.000 --> 00:00:02.000
Hello world

00:00:03.000 --> 00:00:04.000
Goodbye world
"""

NOT_A_SUBTITLE = "this is not a subtitle at all, just plain text"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def subtitle_engine(tmp_path):
    """File-backed async engine: sqlite-vec (M008) + subtitle cache table (M010)."""
    db_path = tmp_path / "subtitle_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    register_sqlite_vec_listener(engine)
    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)
    # M010 operates on engine (not bare conn)
    await _migration_010_create_subtitle_cache(engine)
    # Also ensure the ORM model tables are present (AiSubtitleCache mapped to Base)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def subtitle_factory(subtitle_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(subtitle_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def sub_client(
    subtitle_engine,
    subtitle_factory,
    monkeypatch,
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    """Authenticated ASGI client wired to the subtitle-cache test DB."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    monkeypatch.setattr(settings, "AI_API_KEY", "test-key-subtitle")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)

    from app.main import app
    from app.db import database as db_module

    monkeypatch.setattr(db_module, "async_session_factory", subtitle_factory)
    from app.api import ai as ai_mod
    monkeypatch.setattr(ai_mod, "async_session_factory", subtitle_factory)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with subtitle_factory() as session:
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

def _make_aligned_generate():
    """Return an async generate stub that echoes N translated numbered lines."""
    async def fake_generate(prompt: str) -> str:
        # Count numbered-list lines in the prompt body (e.g. "1. text")
        n = len(re.findall(r"^\s*\d+\.", prompt, re.MULTILINE))
        if n == 0:
            n = 1
        return "\n".join(f"{i}. [fr] ligne {i}" for i in range(1, n + 1))
    return fake_generate


def _make_misaligned_generate():
    """Return a generate stub that always returns WRONG line count (triggers fallback)."""
    async def fake_generate(prompt: str) -> str:
        # Always return only ONE numbered line, regardless of expected count
        return "1. mauvaise réponse"
    return fake_generate


def _make_error_generate():
    """Return a generate stub that raises httpx.ConnectError."""
    async def fake_generate(prompt: str) -> str:
        raise httpx.ConnectError("Connection refused")
    return fake_generate


# ---------------------------------------------------------------------------
# TC-01 — SRT happy path
# ---------------------------------------------------------------------------

async def test_srt_happy_path(sub_client, monkeypatch):
    monkeypatch.setattr(ollama_service, "generate", _make_aligned_generate())

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": SRT_2_CUES, "targetLang": "fr"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # camelCase keys
    assert "translatedContent" in body
    assert "cueCount" in body
    assert "durationSeconds" in body
    assert "cached" in body
    assert "format" in body
    assert "model" in body

    # Values
    assert body["cached"] is False
    assert body["cueCount"] == 2
    assert body["format"] == "srt"
    assert isinstance(body["durationSeconds"], float)

    translated = body["translatedContent"]

    # Timecodes preserved verbatim
    assert "00:00:01,000 --> 00:00:02,000" in translated
    assert "00:00:03,000 --> 00:00:04,000" in translated

    # SRT numeric indices preserved (1, 2)
    lines = translated.splitlines()
    assert "1" in lines
    assert "2" in lines

    # Cue text changed to translated marker
    assert "[fr]" in translated


# ---------------------------------------------------------------------------
# TC-02 — Cache hit
# ---------------------------------------------------------------------------

async def test_cache_hit(sub_client, monkeypatch):
    call_count = 0

    async def counting_generate(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        n = len(re.findall(r"^\s*\d+\.", prompt, re.MULTILINE)) or 1
        return "\n".join(f"{i}. [fr] cached {i}" for i in range(1, n + 1))

    monkeypatch.setattr(ollama_service, "generate", counting_generate)

    body_payload = {"content": SRT_2_CUES, "targetLang": "fr"}

    # First call — cache miss
    r1 = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json=body_payload,
    )
    assert r1.status_code == 200
    assert r1.json()["cached"] is False
    assert call_count >= 1  # at least one generate call

    calls_after_first = call_count

    # Second call — should be a cache hit
    r2 = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json=body_payload,
    )
    assert r2.status_code == 200
    assert r2.json()["cached"] is True
    # generate must NOT have been called again
    assert call_count == calls_after_first


# ---------------------------------------------------------------------------
# TC-03 — VTT happy path
# ---------------------------------------------------------------------------

async def test_vtt_happy_path(sub_client, monkeypatch):
    monkeypatch.setattr(ollama_service, "generate", _make_aligned_generate())

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": VTT_2_CUES, "targetLang": "fr"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["format"] == "vtt"
    assert body["cueCount"] == 2
    assert body["cached"] is False

    translated = body["translatedContent"]
    # WEBVTT header must be preserved
    assert translated.startswith("WEBVTT")
    # Timecodes preserved
    assert "00:00:01.000 --> 00:00:02.000" in translated
    assert "00:00:03.000 --> 00:00:04.000" in translated
    # Translation marker present
    assert "[fr]" in translated


# ---------------------------------------------------------------------------
# TC-04 — Malformed input → 422
# ---------------------------------------------------------------------------

async def test_malformed_input_422(sub_client, monkeypatch):
    # No generate call should happen
    monkeypatch.setattr(ollama_service, "generate", _make_aligned_generate())

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": NOT_A_SUBTITLE, "targetLang": "fr"},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# TC-05 — Too large → 413
# ---------------------------------------------------------------------------

async def test_too_large_413(sub_client, monkeypatch):
    # Reduce SUBTITLE_MAX_CUES to 1 so that a 2-cue SRT is rejected
    monkeypatch.setattr(settings, "SUBTITLE_MAX_CUES", 1)
    monkeypatch.setattr(ollama_service, "generate", _make_aligned_generate())

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": SRT_2_CUES, "targetLang": "fr"},
    )
    assert resp.status_code == 413, resp.text


# ---------------------------------------------------------------------------
# TC-06 — Alignment fallback: wrong LLM line count → original text kept
# ---------------------------------------------------------------------------

async def test_alignment_fallback(sub_client, monkeypatch):
    """When the LLM returns the wrong number of lines (even after retry),
    the service falls back to original cue text — no cue is lost."""
    monkeypatch.setattr(ollama_service, "generate", _make_misaligned_generate())

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": SRT_3_CUES, "targetLang": "fr"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # No cue dropped
    assert body["cueCount"] == 3

    translated = body["translatedContent"]
    # Original cue texts are retained because alignment failed
    assert "First line" in translated
    assert "Second line" in translated
    assert "Third line" in translated


# ---------------------------------------------------------------------------
# TC-07 — Ollama down → 503
# ---------------------------------------------------------------------------

async def test_ollama_down_503(sub_client, monkeypatch):
    monkeypatch.setattr(ollama_service, "generate", _make_error_generate())

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": SRT_2_CUES, "targetLang": "fr"},
    )
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# TC-08 — Auth: missing X-API-Key → 401
# ---------------------------------------------------------------------------

async def test_missing_auth_401(sub_client):
    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        # No X-API-Key header
        json={"content": SRT_2_CUES, "targetLang": "fr"},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# TC-09 — SRT fidelity: two-line cue with <i> tags survives translation
# ---------------------------------------------------------------------------

SRT_TWO_LINE_ITALIC = """\
1
00:00:01,000 --> 00:00:02,000
<i>First line of cue</i>
<i>Second line of cue</i>
"""


async def test_srt_two_line_italic_fidelity(sub_client, monkeypatch):
    """A cue with two physical lines containing <i> tags must keep the line
    break and both opening/closing <i></i> tags after translation."""

    async def fake_generate(prompt: str) -> str:
        # Echo back each numbered line preserving the sentinel and tags
        n = len(re.findall(r"^\s*\d+\.", prompt, re.MULTILINE)) or 1
        lines_in = [m.group(0) for m in re.finditer(r"^\s*\d+\..*", prompt, re.MULTILINE)]
        out: list[str] = []
        for i, raw_line in enumerate(lines_in[:n], start=1):
            # strip the "N. " prefix, keep the rest verbatim (sentinel + tags)
            text_part = re.sub(r"^\s*\d+\.\s*", "", raw_line)
            out.append(f"{i}. [fr] {text_part}")
        return "\n".join(out)

    monkeypatch.setattr(ollama_service, "generate", fake_generate)

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": SRT_TWO_LINE_ITALIC, "targetLang": "fr"},
    )
    assert resp.status_code == 200, resp.text
    translated = resp.json()["translatedContent"]

    # Both <i> open and </i> close tags must survive
    assert "<i>" in translated
    assert "</i>" in translated
    # The newline between the two physical lines must be restored
    # (the cue text must contain a real \n, not the sentinel)
    cue_text_block = translated.split("00:00:01,000 --> 00:00:02,000\n", 1)[1].split("\n\n")[0]
    assert "\n" in cue_text_block, "Intra-cue line break was not restored"


# ---------------------------------------------------------------------------
# TC-10 — VTT fidelity: NOTE/STYLE/cue-id/cue-settings all survive
# ---------------------------------------------------------------------------

VTT_FULL_FEATURES = """\
WEBVTT

NOTE This is a comment block

STYLE
::cue {
  color: white;
}

intro-cue
00:00:01.000 --> 00:00:02.000 align:start position:10%
Hello world
"""


async def test_vtt_full_feature_fidelity(sub_client, monkeypatch):
    """NOTE block, STYLE block, cue identifier, and cue settings after the
    timecode must all appear verbatim in the translated output."""
    monkeypatch.setattr(ollama_service, "generate", _make_aligned_generate())

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": VTT_FULL_FEATURES, "targetLang": "fr", "format": "vtt"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["format"] == "vtt"

    translated = body["translatedContent"]

    # NOTE block preserved verbatim
    assert "NOTE This is a comment block" in translated
    # STYLE block preserved verbatim
    assert "STYLE" in translated
    assert "::cue" in translated
    # Cue identifier preserved
    assert "intro-cue" in translated
    # Cue settings preserved after timecode
    assert "align:start position:10%" in translated


# ---------------------------------------------------------------------------
# TC-11 — Cache discriminates on format / sourceLang (FIX 1 regression guard)
# ---------------------------------------------------------------------------

async def test_cache_discriminates_source_lang(sub_client, monkeypatch):
    """The same content posted with different sourceLang values must NOT be
    served from the same cache entry — generate must be called for both."""
    call_count = 0

    async def counting_generate(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        n = len(re.findall(r"^\s*\d+\.", prompt, re.MULTILINE)) or 1
        return "\n".join(f"{i}. [fr] line {i}" for i in range(1, n + 1))

    monkeypatch.setattr(ollama_service, "generate", counting_generate)

    headers = {"X-API-Key": "test-key-subtitle"}

    # First request — sourceLang: "en"
    r1 = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers=headers,
        json={"content": SRT_2_CUES, "targetLang": "fr", "sourceLang": "en"},
    )
    assert r1.status_code == 200
    assert r1.json()["cached"] is False
    calls_after_first = call_count

    # Second request — sourceLang: "de" (different key → cache miss)
    r2 = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers=headers,
        json={"content": SRT_2_CUES, "targetLang": "fr", "sourceLang": "de"},
    )
    assert r2.status_code == 200
    assert r2.json()["cached"] is False, (
        "Different sourceLang must NOT hit the cache entry from the first request"
    )
    # generate must have been called at least once more
    assert call_count > calls_after_first, (
        "generate was not called for the second (different sourceLang) request"
    )


# ---------------------------------------------------------------------------
# TC-12 — Global timeout → 503 (FIX 3 regression guard)
# ---------------------------------------------------------------------------

async def test_global_timeout_503(sub_client, monkeypatch):
    """When SUBTITLE_TOTAL_TIMEOUT is exceeded, the endpoint must return 503."""
    # Set the total timeout to a tiny value so the fake slow generate exceeds it
    monkeypatch.setattr(settings, "SUBTITLE_TOTAL_TIMEOUT", 1)

    async def slow_generate(prompt: str) -> str:
        await asyncio.sleep(5)  # longer than SUBTITLE_TOTAL_TIMEOUT=1
        return "1. translated"

    monkeypatch.setattr(ollama_service, "generate", slow_generate)

    resp = await sub_client.post(
        "/api/ai/subtitles/translate",
        headers={"X-API-Key": "test-key-subtitle"},
        json={"content": SRT_2_CUES, "targetLang": "fr"},
    )
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# TC-13 — cleanup_cache: prunes old rows, keeps recent, no-op when retention=0
# ---------------------------------------------------------------------------

async def test_cleanup_cache_prunes_old_keeps_recent(subtitle_factory, monkeypatch):
    """cleanup_cache deletes rows older than retention days and keeps recent ones."""
    from app.models.database import AiSubtitleCache
    from app.services import subtitle_service
    from app.utils.time import now_ms

    monkeypatch.setattr(settings, "SUBTITLE_CACHE_RETENTION_DAYS", 30)

    now = now_ms()
    forty_days_ago = now - 40 * 24 * 60 * 60 * 1000

    recent_key = "cache-key-recent"
    old_key = "cache-key-old"

    async with subtitle_factory() as db:
        db.add(AiSubtitleCache(
            cache_key=recent_key,
            target_lang="fr",
            model="test-model",
            source_format="srt",
            cue_count=2,
            translated_content="recent content",
            created_at=now,
        ))
        db.add(AiSubtitleCache(
            cache_key=old_key,
            target_lang="fr",
            model="test-model",
            source_format="srt",
            cue_count=2,
            translated_content="old content",
            created_at=forty_days_ago,
        ))
        await db.commit()

    deleted = await subtitle_service.cleanup_cache(subtitle_factory)

    assert deleted == 1, f"Expected 1 deleted row, got {deleted}"

    from sqlalchemy import select
    async with subtitle_factory() as db:
        result = await db.execute(
            select(AiSubtitleCache.cache_key)
        )
        remaining_keys = {row[0] for row in result}

    assert recent_key in remaining_keys, "Recent cache entry was incorrectly deleted"
    assert old_key not in remaining_keys, "Old cache entry was not deleted"


async def test_cleanup_cache_noop_when_retention_zero(subtitle_factory, monkeypatch):
    """cleanup_cache returns 0 and deletes nothing when retention is 0."""
    from app.models.database import AiSubtitleCache
    from app.services import subtitle_service
    from app.utils.time import now_ms

    monkeypatch.setattr(settings, "SUBTITLE_CACHE_RETENTION_DAYS", 0)

    forty_days_ago = now_ms() - 40 * 24 * 60 * 60 * 1000
    old_key = "cache-key-zero-retention"

    async with subtitle_factory() as db:
        db.add(AiSubtitleCache(
            cache_key=old_key,
            target_lang="fr",
            model="test-model",
            source_format="srt",
            cue_count=1,
            translated_content="old content",
            created_at=forty_days_ago,
        ))
        await db.commit()

    deleted = await subtitle_service.cleanup_cache(subtitle_factory)

    assert deleted == 0, f"Expected 0 deleted rows (retention=0), got {deleted}"

    from sqlalchemy import select
    async with subtitle_factory() as db:
        result = await db.execute(
            select(AiSubtitleCache.cache_key).where(AiSubtitleCache.cache_key == old_key)
        )
        row = result.first()
    assert row is not None, "Row was deleted despite retention=0"
