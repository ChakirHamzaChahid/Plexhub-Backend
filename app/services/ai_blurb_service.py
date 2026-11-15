"""Blurb cache lookup, TMDB cache fallback, and Ollama blurb persistence (CR-A01)."""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import AiMediaBlurb
from app.services.recommendation_service import generate_blurb
from app.utils.db_retry import write_with_retry
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.services.ai_blurb")


class BlurbSourceMissingError(ValueError):
    """Raised when neither request fields nor ai_tmdb_cache have title/overview."""


class BlurbNotFoundError(LookupError):
    """Raised when no cached blurb exists for `(tmdb_id, media_type, lang)`."""


async def get_cached_blurb(
    db: AsyncSession,
    *,
    tmdb_id: int,
    media_type: str,
    lang: str,
) -> dict | None:
    """Return a cached blurb dict or None if absent."""
    row = (
        await db.execute(
            select(AiMediaBlurb).where(
                AiMediaBlurb.tmdb_id == tmdb_id,
                AiMediaBlurb.media_type == media_type,
                AiMediaBlurb.lang == lang,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "tmdb_id": row.tmdb_id,
        "media_type": row.media_type,
        "lang": row.lang,
        "summary": row.summary,
        "tags": json.loads(row.tags),
        "cached": True,
        "model": row.model,
    }


async def generate_and_cache_blurb(
    db: AsyncSession,
    *,
    tmdb_id: int,
    media_type: str,
    lang: str,
    title: str | None = None,
    overview: str | None = None,
    genres: str | None = None,
    force: bool = False,
    generator: Callable[..., Awaitable[dict]] | None = None,
) -> dict:
    """Return cached blurb or generate via Ollama and best-effort persist to DB."""
    if not force:
        cached = await get_cached_blurb(
            db, tmdb_id=tmdb_id, media_type=media_type, lang=lang
        )
        if cached is not None:
            return cached

    resolved_title = title or ""
    resolved_overview = overview or ""
    resolved_genres = genres or ""

    if not resolved_title or not resolved_overview:
        cache_row = (
            await db.execute(
                text(
                    "SELECT title, overview, genres FROM ai_tmdb_cache "
                    "WHERE tmdb_id = :tid"
                ),
                {"tid": tmdb_id},
            )
        ).fetchone()
        if cache_row is not None:
            resolved_title = resolved_title or (cache_row[0] or "")
            resolved_overview = resolved_overview or (cache_row[1] or "")
            resolved_genres = resolved_genres or (cache_row[2] or "")

    if not resolved_title and not resolved_overview:
        raise BlurbSourceMissingError(
            "No source text available: provide title/overview or ensure tmdb_id is in ai_tmdb_cache"
        )

    gen_fn = generator or generate_blurb
    result = await gen_fn(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=resolved_title,
        overview=resolved_overview,
        genres=resolved_genres,
        lang=lang,
    )

    tags_json = json.dumps(result["tags"], ensure_ascii=False)

    async def _persist_blurb(session: AsyncSession) -> None:
        await session.execute(
            text(
                "DELETE FROM ai_media_blurb "
                "WHERE tmdb_id = :tid AND media_type = :mt AND lang = :lang"
            ),
            {"tid": tmdb_id, "mt": media_type, "lang": lang},
        )
        session.add(
            AiMediaBlurb(
                tmdb_id=tmdb_id,
                media_type=media_type,
                lang=lang,
                summary=result["summary"],
                tags=tags_json,
                model=settings.OLLAMA_MODEL,
                created_at=now_ms(),
            )
        )
        await session.commit()

    try:
        await write_with_retry(_persist_blurb, op="ai.blurb_cache_persist")
    except Exception:
        # Best-effort cache: return generated result even if persistence fails.
        pass

    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "lang": lang,
        "summary": result["summary"],
        "tags": result["tags"],
        "cached": False,
        "model": settings.OLLAMA_MODEL,
    }
