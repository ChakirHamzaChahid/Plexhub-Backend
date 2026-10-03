"""FR-366 (Lot 6 E-b): une réponse vide ou partielle ne vide pas le catalogue.

Couvre (1) l'échec PARTIEL par catégorie en mode whitelist (le nettoyage
différentiel ne doit jamais toucher une catégorie dont le fetch a échoué),
(2) les gardes « réponse vide » films/séries/live (jamais de suppression),
(3) le nouveau contrat de `_fetch_streams_by_categories`.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.database import LiveChannel, Media, XtreamAccount, XtreamCategory
from app.services.xtream_service import xtream_service
from app.workers import sync_worker as sw


def _account(account_id: str, mode: str) -> XtreamAccount:
    return XtreamAccount(
        id=account_id, label="T", base_url="http://x.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
        category_filter_mode=mode,
    )


def _cats(account_id: str) -> list[XtreamCategory]:
    return [
        XtreamCategory(account_id=account_id, category_id=cid, category_type=t,
                       category_name=f"{t}{cid}", is_allowed=True, last_fetched_at=0)
        for t in ("vod", "series", "live") for cid in ("A", "B")
    ]


def _vod(sid: int, cat: str) -> dict:
    return {"stream_id": sid, "name": f"Movie {sid}", "added": "1", "category_id": cat,
            "container_extension": "mp4", "rating": "7", "stream_icon": None}


def _series(sid: int, cat: str) -> dict:
    return {"series_id": sid, "name": f"Show {sid}", "category_id": cat, "cover": None,
            "plot": "p", "genre": "Drama", "rating": "8", "backdrop_path": None,
            "episode_run_time": "30", "last_modified": "1"}


def _live(sid: int, cat: str) -> dict:
    return {"stream_id": sid, "name": f"Chan {sid}", "category_id": cat,
            "container_extension": "ts", "added": "1"}


@pytest_asyncio.fixture
async def db(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sw, "async_session_factory", factory)

    async def _empty_list(*a, **kw):
        return []

    async def _empty_dict(*a, **kw):
        return {}

    for name in ("get_vod_categories", "get_series_categories", "get_live_categories",
                 "get_vod_streams", "get_series", "get_live_streams"):
        monkeypatch.setattr(xtream_service, name, _empty_list)
    monkeypatch.setattr(xtream_service, "get_vod_info", _empty_dict)
    monkeypatch.setattr(xtream_service, "get_series_info", _empty_dict)
    return factory


def _per_category(catalog: dict[str, list[dict]], failing: set[str]):
    """Fake `get_*(account, category_id=...)` : lève pour `failing`."""
    async def _fn(account, category_id=None):
        if category_id in failing:
            raise RuntimeError("HTTP 502")
        if category_id is None:
            return [dict(d) for v in catalog.values() for d in v]
        return [dict(d) for d in catalog.get(category_id, [])]
    return _fn


async def _titles(factory, typ):
    async with factory() as s:
        return set((await s.execute(select(Media.title).where(Media.type == typ))).scalars())


async def _live_ids(factory):
    async with factory() as s:
        return set((await s.execute(select(LiveChannel.stream_id))).scalars())


async def _seed(factory, account_id, mode):
    async with factory() as s:
        s.add(_account(account_id, mode))
        s.add_all(_cats(account_id))
        await s.commit()


def _install(monkeypatch, vod, series, live):
    monkeypatch.setattr(xtream_service, "get_vod_streams", vod)
    monkeypatch.setattr(xtream_service, "get_series", series)
    monkeypatch.setattr(xtream_service, "get_live_streams", live)


FULL_VOD = {"A": [_vod(1, "A"), _vod(2, "A")], "B": [_vod(3, "B"), _vod(4, "B")]}
FULL_SERIES = {"A": [_series(11, "A"), _series(12, "A")], "B": [_series(13, "B"), _series(14, "B")]}
FULL_LIVE = {"A": [_live(21, "A"), _live(22, "A")], "B": [_live(23, "B"), _live(24, "B")]}


async def _first_full_sync(factory, monkeypatch, account_id, mode="whitelist"):
    await _seed(factory, account_id, mode)
    _install(monkeypatch, _per_category(FULL_VOD, set()),
             _per_category(FULL_SERIES, set()), _per_category(FULL_LIVE, set()))
    await sw.sync_account(account_id)
    assert await _titles(factory, "movie") == {f"Movie {i}" for i in (1, 2, 3, 4)}
    assert await _titles(factory, "show") == {f"Show {i}" for i in (11, 12, 13, 14)}
    assert await _live_ids(factory) == {21, 22, 23, 24}


class TestPartialCategoryFailure:
    async def test_failed_category_is_kept_fetched_category_is_cleaned(self, db, monkeypatch):
        acc = "fr366_partial"
        await _first_full_sync(db, monkeypatch, acc)

        # Re-sync : A a retiré son 2e élément chez le fournisseur, B échoue.
        _install(monkeypatch,
                 _per_category({"A": [_vod(1, "A")]}, {"B"}),
                 _per_category({"A": [_series(11, "A")]}, {"B"}),
                 _per_category({"A": [_live(21, "A")]}, {"B"}))
        await sw.sync_account(acc)

        # B (échec) intacte ; A nettoyée du retiré.
        assert await _titles(db, "movie") == {"Movie 1", "Movie 3", "Movie 4"}
        assert await _titles(db, "show") == {"Show 11", "Show 13", "Show 14"}
        assert await _live_ids(db) == {21, 23, 24}

    async def test_total_failure_cleans_nothing(self, db, monkeypatch):
        acc = "fr366_total"
        await _first_full_sync(db, monkeypatch, acc)
        _install(monkeypatch, _per_category({}, {"A", "B"}),
                 _per_category({}, {"A", "B"}), _per_category({}, {"A", "B"}))
        await sw.sync_account(acc)
        assert len(await _titles(db, "movie")) == 4
        assert len(await _titles(db, "show")) == 4
        assert len(await _live_ids(db)) == 4

    async def test_recovered_category_is_cleaned_next_sync(self, db, monkeypatch):
        acc = "fr366_recover"
        await _first_full_sync(db, monkeypatch, acc)
        _install(monkeypatch, _per_category({"A": FULL_VOD["A"]}, {"B"}),
                 _per_category(FULL_SERIES, set()), _per_category(FULL_LIVE, set()))
        await sw.sync_account(acc)
        assert len(await _titles(db, "movie")) == 4
        # B répond de nouveau, mais sans l'élément 4 : il doit maintenant partir.
        _install(monkeypatch, _per_category({"A": FULL_VOD["A"], "B": [_vod(3, "B")]}, set()),
                 _per_category(FULL_SERIES, set()), _per_category(FULL_LIVE, set()))
        await sw.sync_account(acc)
        assert await _titles(db, "movie") == {"Movie 1", "Movie 2", "Movie 3"}


class TestEmptyResponseGuards:
    """Réponse ENTIÈREMENT vide : aucune suppression (films, séries, live)."""

    @pytest.mark.parametrize("mode", ["all", "whitelist"])
    async def test_empty_responses_delete_nothing(self, db, monkeypatch, mode):
        acc = f"fr366_empty_{mode}"
        await _first_full_sync(db, monkeypatch, acc, mode)

        # Le fournisseur répond 200 avec des listes vides.
        _install(monkeypatch, _per_category({}, set()), _per_category({}, set()),
                 _per_category({}, set()))
        await sw.sync_account(acc)

        assert len(await _titles(db, "movie")) == 4
        assert len(await _titles(db, "show")) == 4
        assert len(await _live_ids(db)) == 4


class TestFetchByCategoriesContract:
    async def test_returns_streams_and_failed_ids(self):
        fn = _per_category({"A": [{"stream_id": 1}], "B": [{"stream_id": 2}]}, {"B"})
        streams, failed = await sw._fetch_streams_by_categories(fn, object(), ["A", "B"], "VOD")
        assert streams == [{"stream_id": 1}]
        assert failed == {"B"}

    async def test_no_failure_returns_empty_failed_set(self):
        fn = _per_category({"A": [{"stream_id": 1}]}, set())
        streams, failed = await sw._fetch_streams_by_categories(fn, object(), ["A"], "VOD")
        assert streams == [{"stream_id": 1}]
        assert failed == set()


class TestCleanupCategoryIds:
    def test_excludes_failed_and_disallowed(self):
        allowed = {"A": True, "B": True, "C": False}
        assert sw._cleanup_category_ids(allowed, {"B"}, "VOD") == ["A"]
        assert sw._cleanup_category_ids(allowed, set(), "VOD") == ["A", "B"]
        assert sw._cleanup_category_ids(allowed, {"A", "B"}, "VOD") == []
