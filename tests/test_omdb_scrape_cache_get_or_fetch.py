"""ADR 0005 D3 (W0) — `omdb_scrape_cache_service.get_or_fetch`: the shared
implementation now delegated to by both
`enrichment_worker._fetch_omdb_by_id` and
`enrichment_backfill_worker._fetch_omdb_by_id`.

Also covers: the two wrapper functions still exist with their original
name/signature and still respect `monkeypatch.setattr(<module>,
"omdb_service", fake)` (the pattern `tests/test_enrichment_backfill.py`
relies on).
"""
from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.models.database import OmdbScrapeCache
from app.services import omdb_scrape_cache_service as omdb_scrape_cache
from app.services.omdb_service import OMDbData
from app.utils.time import now_ms


class _FakeOmdbSvc:
    """Call-counting double — no AsyncMock (house style)."""

    def __init__(self, data: OMDbData | None = None, *, configured: bool = True, count: int = 0):
        self._data = data
        self._configured = configured
        self._count = count
        self.calls: list[str] = []

    @property
    def is_configured(self) -> bool:
        return self._configured

    def get_request_count(self) -> int:
        return self._count

    async def get_by_imdb_id(self, imdb_id: str) -> OMDbData | None:
        self.calls.append(imdb_id)
        return self._data


def _data(imdb_id="tt0088247") -> OMDbData:
    return OMDbData(
        title="Terminator", year="1984", runtime_minutes=107, genre="Action",
        director="James Cameron", actors="Arnold", plot="A cyborg...",
        imdb_rating=8.1, imdb_votes=900000, type="movie", imdb_id=imdb_id,
    )


async def test_blank_id_no_op(db_factory):
    fake = _FakeOmdbSvc(_data())
    result, put = await omdb_scrape_cache.get_or_fetch("", session_factory=db_factory, client=fake)
    assert result is None and put is None
    assert fake.calls == []


async def test_unconfigured_no_op(db_factory):
    fake = _FakeOmdbSvc(_data(), configured=False)
    result, put = await omdb_scrape_cache.get_or_fetch(
        "tt0088247", session_factory=db_factory, client=fake,
    )
    assert result is None and put is None
    assert fake.calls == []


async def test_budget_exhausted_no_op(db_factory, monkeypatch):
    monkeypatch.setattr(settings, "OMDB_DAILY_LIMIT", 5)
    fake = _FakeOmdbSvc(_data(), count=5)
    result, put = await omdb_scrape_cache.get_or_fetch(
        "tt0088247", session_factory=db_factory, client=fake,
    )
    assert result is None and put is None
    assert fake.calls == []


async def test_cache_hit_no_network_no_put(db_session, db_factory):
    imdb_id = "tt0088247"
    await omdb_scrape_cache.put(db_session, imdb_id, "found", _data(imdb_id), now_ms())
    await db_session.commit()

    fake = _FakeOmdbSvc(_data(imdb_id))
    result, put = await omdb_scrape_cache.get_or_fetch(
        imdb_id, session_factory=db_factory, client=fake,
    )
    assert result is not None
    assert result.imdb_id == imdb_id
    assert put is None  # cache hit -> nothing new to write
    assert fake.calls == []  # get_or_fetch itself never wrote/read via client


async def test_fresh_fetch_found_returns_pending_put(db_factory):
    fake = _FakeOmdbSvc(_data("tt0000123"))
    result, put = await omdb_scrape_cache.get_or_fetch(
        "tt0000123", session_factory=db_factory, client=fake,
    )
    assert result is not None
    assert put == ("tt0000123", "found")
    assert fake.calls == ["tt0000123"]


async def test_fresh_fetch_not_found_returns_pending_put(db_factory):
    fake = _FakeOmdbSvc(None)
    result, put = await omdb_scrape_cache.get_or_fetch(
        "tt9999999", session_factory=db_factory, client=fake,
    )
    assert result is None
    assert put == ("tt9999999", "not_found")


async def test_get_or_fetch_never_writes(db_engine, db_factory):
    """The function itself must never persist anything — the caller applies
    `pending_put` in its own transaction. Verified by counting rows before
    and after a fresh (found) fetch."""
    fake = _FakeOmdbSvc(_data("tt0000123"))
    await omdb_scrape_cache.get_or_fetch("tt0000123", session_factory=db_factory, client=fake)

    async with db_factory() as check:
        rows = (await check.execute(select(OmdbScrapeCache))).scalars().all()
    assert rows == []


class TestWrapperDelegation:
    """The two module-private `_fetch_omdb_by_id` wrappers keep their
    original name/signature and still respect module-level monkeypatching
    of `omdb_service` (the pattern the existing test suite relies on)."""

    async def test_enrichment_worker_wrapper_delegates(self, db_factory, monkeypatch):
        from app.workers import enrichment_worker as worker

        fake = _FakeOmdbSvc(_data("tt0000777"))
        monkeypatch.setattr(worker, "omdb_service", fake)
        monkeypatch.setattr(worker, "async_session_factory", db_factory)
        data, put = await worker._fetch_omdb_by_id("tt0000777")
        assert data is not None
        assert put == ("tt0000777", "found")
        assert fake.calls == ["tt0000777"]

    async def test_backfill_worker_wrapper_delegates(self, db_factory, monkeypatch):
        from app.workers import enrichment_backfill_worker as backfill

        fake = _FakeOmdbSvc(_data("tt0000888"))
        monkeypatch.setattr(backfill, "omdb_service", fake)

        data, put = await backfill._fetch_omdb_by_id("tt0000888", db_factory)
        assert data is not None
        assert put == ("tt0000888", "found")
        assert fake.calls == ["tt0000888"]
