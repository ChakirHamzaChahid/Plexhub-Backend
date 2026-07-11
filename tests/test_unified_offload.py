"""Guard tests for CR-P01: the unified-catalog aggregation must run OFF the
event loop (via asyncio.to_thread), with pagination/sort results unchanged.

Two things are asserted per aggregation path (movies list, unified episodes):
  1. Correctness is byte-identical to before the fix (sort order + slicing).
  2. The CPU-bound aggregation call actually executes on a worker thread, not
     the event-loop (test) thread — proving the stall is genuinely offloaded,
     not just wrapped in a no-op.
"""
import threading

import pytest

import app.services.media_service as media_service_module
from app.models.database import Media, XtreamAccount
from app.utils.server_id import build_server_id


def _account(id_: str, label: str) -> XtreamAccount:
    return XtreamAccount(
        id=id_, label=label, base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


def _movie(account_id: str, rating_key: str, title: str, unif: str,
           added_at: int = 0, page_offset: int = 0) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=1984, unification_id=unif,
        added_at=added_at, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


def _show(account_id: str, rating_key: str, title: str, unif: str,
          page_offset: int = 0) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=title, type="show", year=2008, unification_id=unif,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
    )


def _episode(account_id: str, rating_key: str, grandparent_rating_key: str,
             season: int, episode: int, page_offset: int = 0) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=f"S{season:02d}E{episode:02d}", type="episode",
        grandparent_rating_key=grandparent_rating_key,
        parent_index=season, index=episode, page_offset=page_offset,
    )


# ─── get_unified_list (movies) ──────────────────────────────────────────────


class TestUnifiedListOffload:

    @pytest.mark.asyncio
    async def test_sort_and_pagination_unchanged_after_offload(self, db_session):
        """(1) Correctness guard: same sort (added_at desc) + same slicing as
        the pre-fix synchronous implementation."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _movie("a", "vod_1.mp4", "Movie One", "tmdb://1", added_at=100, page_offset=0),
            _movie("a", "vod_2.mp4", "Movie Two", "tmdb://2", added_at=300, page_offset=1),
            _movie("a", "vod_3.mp4", "Movie Three", "tmdb://3", added_at=200, page_offset=2),
        ])
        await db_session.commit()

        page1, total = await media_service.get_unified_list(
            db_session, "movie", limit=2, offset=0,
        )
        assert total == 3
        assert [g.key for g in page1] == ["tmdb://2", "tmdb://3"]

        page2, total2 = await media_service.get_unified_list(
            db_session, "movie", limit=2, offset=2,
        )
        assert total2 == 3
        assert [g.key for g in page2] == ["tmdb://1"]

    @pytest.mark.asyncio
    async def test_aggregation_runs_on_a_worker_thread(self, db_session, monkeypatch):
        """(2) Offload guard: aggregate_movies must execute on a different
        thread than the event loop (test) thread — proves asyncio.to_thread
        is actually engaged, not a pass-through."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _movie("a", "vod_1.mp4", "Movie One", "tmdb://1", added_at=100),
        ])
        await db_session.commit()

        caller_thread_id = threading.get_ident()
        seen: dict[str, int] = {}
        original = media_service_module.aggregate_movies

        def spy(rows):
            seen["thread_id"] = threading.get_ident()
            return original(rows)

        monkeypatch.setattr(media_service_module, "aggregate_movies", spy)

        groups, total = await media_service.get_unified_list(
            db_session, "movie", limit=200, offset=0,
        )

        assert total == 1
        assert "thread_id" in seen, "aggregate_movies was never called"
        assert seen["thread_id"] != caller_thread_id, (
            "aggregation ran on the event-loop thread — CR-P01 offload not engaged"
        )


# ─── get_unified_episodes (shows) ───────────────────────────────────────────


class TestUnifiedEpisodesOffload:

    @pytest.mark.asyncio
    async def test_episode_grouping_unchanged_after_offload(self, db_session):
        """(1) Correctness guard: episode slots still built correctly."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _show("a", "series_1", "Breaking Bad", "tmdb://1396", page_offset=0),
            _episode("a", "ep_1", "series_1", season=1, episode=1, page_offset=1),
            _episode("a", "ep_2", "series_1", season=1, episode=2, page_offset=2),
        ])
        await db_session.commit()

        result = await media_service.get_unified_episodes(db_session, "tmdb://1396")
        assert result is not None
        shows, group = result
        assert len(shows) == 1
        slots = sorted(group.slots, key=lambda s: (s.season, s.episode))
        assert [(s.season, s.episode) for s in slots] == [(1, 1), (1, 2)]

    @pytest.mark.asyncio
    async def test_aggregation_runs_on_a_worker_thread(self, db_session, monkeypatch):
        """(2) Offload guard: aggregate_series must execute off the event-loop
        thread."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _show("a", "series_1", "Breaking Bad", "tmdb://1396", page_offset=0),
            _episode("a", "ep_1", "series_1", season=1, episode=1, page_offset=1),
        ])
        await db_session.commit()

        caller_thread_id = threading.get_ident()
        seen: dict[str, int] = {}
        original = media_service_module.aggregate_series

        def spy(shows, episodes):
            seen["thread_id"] = threading.get_ident()
            return original(shows, episodes)

        monkeypatch.setattr(media_service_module, "aggregate_series", spy)

        result = await media_service.get_unified_episodes(db_session, "tmdb://1396")

        assert result is not None
        assert "thread_id" in seen, "aggregate_series was never called"
        assert seen["thread_id"] != caller_thread_id, (
            "aggregation ran on the event-loop thread — CR-P01 offload not engaged"
        )
