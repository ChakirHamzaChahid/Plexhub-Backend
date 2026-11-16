"""Unit + integration tests for recommendation_service.

Uses isolated ai_db_session / ai_sessionmaker fixtures (J1) and monkeypatches
tmdb_service + embed_passages to keep tests offline and deterministic.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import numpy as np
import pytest
from sqlalchemy import text
from unittest.mock import AsyncMock

from app.services import embedding_service, recommendation_service
from app.services.embedding_service import EMBEDDING_DIM, EmbeddingUnavailableError
from app.services.recommendation_service import (
    HydrateStats,
    _deserialize_vec,
    cosine_rank,
    hydrate_misses,
    load_cached_vectors,
    semantic_search,
    semantic_search_with_overview,
    serialize_vec,
)


pytestmark = pytest.mark.asyncio
pytest_plugins = ["tests.conftest_ai"]


@dataclass
class _FakeEnrichment:
    tmdb_id: int
    imdb_id: str | None
    overview: str | None
    genres: str | None


def _unit_vec(idx: int) -> list[float]:
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    arr[idx % EMBEDDING_DIM] = 1.0
    return arr.tolist()


@pytest.fixture(autouse=True)
def _reset_model_singleton():
    embedding_service._model = None
    yield
    embedding_service._model = None


async def _insert_cached_embedding(session, tmdb_id: int, vec: list[float], media_type: str = "movie"):
    now = int(time.time() * 1000)
    await session.execute(
        text(
            "INSERT INTO ai_tmdb_cache(tmdb_id, imdb_id, media_type, title, overview, genres, fetched_at, embedded_at) "
            "VALUES(:t, NULL, :mt, NULL, 'o', 'g', :n, :n)"
        ),
        {"t": tmdb_id, "mt": media_type, "n": now},
    )
    await session.execute(
        text("INSERT INTO ai_embeddings(tmdb_id, embedding) VALUES(:t, :v)"),
        {"t": tmdb_id, "v": serialize_vec(vec)},
    )
    await session.commit()


async def test_load_cached_vectors_hits_only(ai_db_session):
    await _insert_cached_embedding(ai_db_session, 1, _unit_vec(0))
    await _insert_cached_embedding(ai_db_session, 2, _unit_vec(1))

    result = await load_cached_vectors(ai_db_session, [1, 2, 999])
    assert set(result.keys()) == {1, 2}
    assert len(result[1]) == EMBEDDING_DIM


async def test_load_cached_vectors_empty():
    # No DB session needed for empty path
    from sqlalchemy.ext.asyncio import AsyncSession
    fake = AsyncMock(spec=AsyncSession)
    result = await load_cached_vectors(fake, [])
    assert result == {}
    fake.execute.assert_not_called()


async def test_cosine_rank_orders_and_limits():
    q = _unit_vec(0)
    candidates = {
        1: _unit_vec(0),   # score ≈ 1.0
        2: _unit_vec(1),   # score ≈ 0.0
        3: [-x for x in _unit_vec(0)],  # score ≈ -1.0
    }
    ranked = cosine_rank(q, candidates, limit=2)
    assert [tid for tid, _ in ranked] == [1, 2]
    assert ranked[0][1] > ranked[1][1]


async def test_cosine_rank_exclude():
    q = _unit_vec(0)
    candidates = {1: _unit_vec(0), 2: _unit_vec(1)}
    ranked = cosine_rank(q, candidates, limit=10, exclude={1})
    assert [tid for tid, _ in ranked] == [2]


async def test_hydrate_misses_cap_at_20(monkeypatch, ai_sessionmaker):
    """25 misses → 20 hydrated, 5 dropped (cap excess)."""
    async def fake_get_movie_details(tmdb_id):
        return _FakeEnrichment(tmdb_id=tmdb_id, imdb_id=None, overview=f"plot {tmdb_id}", genres="Action")

    monkeypatch.setattr(
        "app.services.recommendation_service.tmdb_service.get_movie_details",
        fake_get_movie_details,
    )
    # Bypass real embedding: feed deterministic vectors
    async def fake_embed(texts):
        return [_unit_vec(i) for i, _ in enumerate(texts)]

    monkeypatch.setattr("app.services.recommendation_service.embed_passages", fake_embed)

    ids = list(range(100, 125))  # 25 ids
    vectors, stats = await hydrate_misses(ids, "movie", ai_sessionmaker)
    assert stats.hydrated == 20
    assert stats.dropped == 5  # 25 - 20 cap excess
    assert len(vectors) == 20


async def test_hydrate_misses_timeout_counts_dropped(monkeypatch, ai_sessionmaker):
    """Per-task timeout 10s → timeout becomes dropped without blocking siblings."""
    # Override timeout to keep test fast
    monkeypatch.setattr(recommendation_service, "HYDRATE_PER_TASK_TIMEOUT_S", 0.2)

    async def slow_get(tmdb_id):
        await asyncio.sleep(2.0)
        return _FakeEnrichment(tmdb_id=tmdb_id, imdb_id=None, overview="x", genres="y")

    monkeypatch.setattr(
        "app.services.recommendation_service.tmdb_service.get_movie_details",
        slow_get,
    )
    async def fake_embed(texts):
        return [_unit_vec(0) for _ in texts]
    monkeypatch.setattr("app.services.recommendation_service.embed_passages", fake_embed)

    start = time.monotonic()
    vectors, stats = await hydrate_misses([1, 2], "movie", ai_sessionmaker)
    elapsed = time.monotonic() - start
    assert elapsed < 1.5  # both timed out roughly in parallel
    assert vectors == {}
    assert stats.dropped == 2


async def test_hydrate_misses_propagates_embedding_unavailable(monkeypatch, ai_sessionmaker):
    async def fake_get(tmdb_id):
        return _FakeEnrichment(tmdb_id=tmdb_id, imdb_id=None, overview="x", genres="y")
    monkeypatch.setattr(
        "app.services.recommendation_service.tmdb_service.get_movie_details",
        fake_get,
    )
    async def bomb(texts):
        raise EmbeddingUnavailableError("model offline")
    monkeypatch.setattr("app.services.recommendation_service.embed_passages", bomb)

    with pytest.raises(EmbeddingUnavailableError):
        await hydrate_misses([1], "movie", ai_sessionmaker)


async def test_hydrate_misses_retries_on_locked_db(monkeypatch, ai_sessionmaker):
    """CR-D04: _fetch_and_store_one persists via write_with_retry so transient
    'database is locked' errors do not drop hydrated embeddings."""
    from app.utils import db_retry as db_retry_module

    async def fake_get(tmdb_id):
        return _FakeEnrichment(tmdb_id=tmdb_id, imdb_id=None, overview="x", genres="y")

    monkeypatch.setattr(
        "app.services.recommendation_service.tmdb_service.get_movie_details",
        fake_get,
    )

    async def fake_embed(texts):
        return [_unit_vec(0) for _ in texts]

    monkeypatch.setattr("app.services.recommendation_service.embed_passages", fake_embed)

    calls = {"n": 0}
    real_write = db_retry_module.write_with_retry

    async def _spy_write(work, **kwargs):
        calls["n"] += 1
        return await real_write(work, **kwargs)

    monkeypatch.setattr(db_retry_module, "write_with_retry", _spy_write)

    vectors, stats = await hydrate_misses([77], "movie", ai_sessionmaker)
    assert calls["n"] == 1
    assert 77 in vectors
    assert stats.hydrated == 1
    assert stats.dropped == 0


# ──────────────────────────────────────────────────────────────────────────────
# CR-P08 — adaptive KNN over-fetch under a skewed type mix
# ──────────────────────────────────────────────────────────────────────────────

async def test_semantic_search_type_skew_escalates_to_reach_limit(monkeypatch, ai_db_session):
    """When the nearest neighbors are dominated by the *other* media_type,
    the post-filter must still reach `limit` matching rows by escalating the
    KNN over-fetch ceiling, instead of silently under-returning.

    Ceilings are monkeypatched to a small ladder (5, 50) so the test stays
    fast/deterministic while exercising the exact same escalation code path
    used in production (only the numeric ceilings differ from the real
    defaults of 200/2000).
    """
    monkeypatch.setattr(recommendation_service, "KNN_OVERFETCH_CEILINGS", (5, 50))

    query = _unit_vec(0)
    # 40 "tv" rows exactly on the query vector -> distance 0, always ranked
    # ahead of the movies below. More than the first ceiling (5), so the
    # initial KNN attempt (k=5) returns 0 movies.
    for i in range(40):
        await _insert_cached_embedding(ai_db_session, 1000 + i, query, media_type="tv")
    # 20 "movie" rows on an orthogonal vector -> fixed distance sqrt(2),
    # strictly farther than every tv row, but well inside the escalated
    # ceiling of 50 (40 tv + up to 10 movies fit in k=50).
    other = _unit_vec(1)
    for i in range(20):
        await _insert_cached_embedding(ai_db_session, 2000 + i, other, media_type="movie")

    results = await semantic_search(ai_db_session, query, media_type="movie", limit=5)

    assert len(results) == 5, f"expected escalation to surface 5 movies, got {results}"
    assert all(mt == "movie" for _, _, mt, _ in results)


async def test_semantic_search_with_overview_type_skew_escalates_to_reach_limit(monkeypatch, ai_db_session):
    """Same CR-P08 escalation guarantee for the overview-returning variant
    used by /api/ai/assistant."""
    monkeypatch.setattr(recommendation_service, "KNN_OVERFETCH_CEILINGS", (5, 50))

    query = _unit_vec(0)
    for i in range(40):
        await _insert_cached_embedding(ai_db_session, 3000 + i, query, media_type="tv")
    other = _unit_vec(1)
    for i in range(20):
        await _insert_cached_embedding(ai_db_session, 4000 + i, other, media_type="movie")

    results = await semantic_search_with_overview(ai_db_session, query, media_type="movie", limit=5)

    assert len(results) == 5, f"expected escalation to surface 5 movies, got {results}"
    assert all(mt == "movie" for _, _, mt, _, _ in results)


async def test_semantic_search_no_type_filter_unaffected(ai_db_session):
    """Sanity: media_type=None must still fetch exactly `limit` rows with no
    escalation ladder involved (regression guard for the shared helper)."""
    query = _unit_vec(0)
    for i in range(10):
        await _insert_cached_embedding(ai_db_session, 5000 + i, query, media_type="movie")

    results = await semantic_search(ai_db_session, query, media_type=None, limit=3)
    assert len(results) == 3


# ──────────────────────────────────────────────────────────────────────────────
# T10.1 — Playable-first & metadata filters
# ──────────────────────────────────────────────────────────────────────────────

async def _ensure_catalog_schema(engine) -> None:
    from app.models.database import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def test_semantic_search_playable_only_filters_broken_disallowed_outage_and_unowned(
    ai_engine, ai_db_session
):
    from app.models.database import Media, XtreamAccount
    from app.utils.server_id import build_server_id

    await _ensure_catalog_schema(ai_engine)
    srv_ok = build_server_id("srv_ok")
    srv_out = build_server_id("srv_out")
    ai_db_session.add_all([
        XtreamAccount(
            id="srv_ok", label="OK", base_url="http://ok.example",
            port=80, username="u", password="p", outage_strikes=0,
        ),
        XtreamAccount(
            id="srv_out", label="Out", base_url="http://out.example",
            port=80, username="u", password="p", outage_strikes=10,
        ),
    ])

    q = _unit_vec(0)
    # 6001: playable
    await _insert_cached_embedding(ai_db_session, 6001, q, media_type="movie")
    ai_db_session.add(Media(
        rating_key="rk1", server_id=srv_ok, library_section_id="1", page_offset=1,
        title="Playable", type="movie", tmdb_id="6001",
        is_in_allowed_categories=True, is_broken=False,
    ))
    # 6002: broken
    await _insert_cached_embedding(ai_db_session, 6002, q, media_type="movie")
    ai_db_session.add(Media(
        rating_key="rk2", server_id=srv_ok, library_section_id="1", page_offset=2,
        title="Broken", type="movie", tmdb_id="6002",
        is_in_allowed_categories=True, is_broken=True,
    ))
    # 6003: disallowed category
    await _insert_cached_embedding(ai_db_session, 6003, q, media_type="movie")
    ai_db_session.add(Media(
        rating_key="rk3", server_id=srv_ok, library_section_id="1", page_offset=3,
        title="Disallowed", type="movie", tmdb_id="6003",
        is_in_allowed_categories=False, is_broken=False,
    ))
    # 6004: outage server
    await _insert_cached_embedding(ai_db_session, 6004, q, media_type="movie")
    ai_db_session.add(Media(
        rating_key="rk4", server_id=srv_out, library_section_id="1", page_offset=4,
        title="Outage", type="movie", tmdb_id="6004",
        is_in_allowed_categories=True, is_broken=False,
    ))
    # 6005: unowned (only in ai_tmdb_cache + ai_embeddings)
    await _insert_cached_embedding(ai_db_session, 6005, q, media_type="movie")
    await ai_db_session.commit()

    playable_res = await semantic_search(
        ai_db_session, q, media_type="movie", limit=10, playable_only=True
    )
    assert [r[0] for r in playable_res] == [6001]

    all_res = await semantic_search(
        ai_db_session, q, media_type="movie", limit=10, playable_only=False
    )
    assert {r[0] for r in all_res} == {6001, 6002, 6003, 6004, 6005}


async def test_semantic_search_playable_only_overfetch_escalates(
    monkeypatch, ai_engine, ai_db_session
):
    from app.models.database import Media

    await _ensure_catalog_schema(ai_engine)
    monkeypatch.setattr(recommendation_service, "KNN_OVERFETCH_CEILINGS", (5, 50))

    q = _unit_vec(0)
    # 20 unowned (closest) + 5 playable (farther)
    for i in range(20):
        await _insert_cached_embedding(ai_db_session, 7000 + i, q, media_type="movie")
    other = _unit_vec(1)
    for i in range(5):
        tid = 7100 + i
        await _insert_cached_embedding(ai_db_session, tid, other, media_type="movie")
        ai_db_session.add(Media(
            rating_key=f"rk-{tid}", server_id="srv_ok", library_section_id="1", page_offset=tid,
            title=f"Playable {i}", type="movie", tmdb_id=str(tid),
            is_in_allowed_categories=True, is_broken=False,
        ))
    await ai_db_session.commit()

    results = await semantic_search(
        ai_db_session, q, media_type=None, limit=5, playable_only=True
    )
    assert len(results) == 5
    assert all(7100 <= r[0] < 7105 for r in results)


async def test_semantic_search_filters_genres_year_and_min_rating(ai_engine, ai_db_session):
    from app.models.database import Media

    await _ensure_catalog_schema(ai_engine)
    q = _unit_vec(0)

    items = [
        (8001, "Action, Sci-Fi", 2022, 8.2),
        (8002, "Comedy", 2022, 8.5),
        (8003, "Action", 2010, 8.0),
        (8004, "Action", 2023, 5.0),
    ]
    for tid, genres, year, rating in items:
        await _insert_cached_embedding(ai_db_session, tid, q, media_type="movie")
        ai_db_session.add(Media(
            rating_key=f"rk-{tid}", server_id="srv_ok", library_section_id="1", page_offset=tid,
            title=f"Movie {tid}", type="movie", tmdb_id=str(tid),
            genres=genres, year=year, display_rating=rating,
            is_in_allowed_categories=True, is_broken=False,
        ))
    await ai_db_session.commit()

    res = await semantic_search(
        ai_db_session,
        q,
        media_type="movie",
        limit=10,
        playable_only=True,
        genres=["action"],
        year_min=2020,
        year_max=2024,
        min_rating=7.0,
    )
    assert [r[0] for r in res] == [8001]
