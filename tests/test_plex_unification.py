"""`app.services.plex_sync_service.calculate_plex_unification_id` (pure) and
the tmdb -> imdb post-pass bridge (`_bridge_tmdb_to_imdb`) — PH-PLEX-03.

Identity rule under test: unlike the Xtream-side
`app.utils.unification.calculate_unification_id`, the Plex variant NEVER
falls back to a title+year key — two different Plex items that share
neither an imdb nor a tmdb guid must get DISTINCT unification ids even if
they happen to share a title/year (Android rule, house-law-adjacent).
"""
from __future__ import annotations

from app.models.database import PlexMediaItem
from app.services.plex_sync_service import (
    _bridge_tmdb_to_imdb,
    calculate_plex_unification_id,
)


# ─── calculate_plex_unification_id ──────────────────────────────────────


class TestCalculatePlexUnificationId:
    def test_imdb_takes_priority_over_tmdb(self):
        result = calculate_plex_unification_id("tt0110912", "680", "plex_srv1", "rk1")
        assert result == "imdb://tt0110912"

    def test_imdb_without_tt_prefix_is_normalized(self):
        result = calculate_plex_unification_id("0110912", None, "plex_srv1", "rk1")
        assert result == "imdb://tt0110912"

    def test_tmdb_used_when_no_imdb(self):
        result = calculate_plex_unification_id(None, "680", "plex_srv1", "rk1")
        assert result == "tmdb://680"

    def test_fallback_is_per_source_never_title_based(self):
        result = calculate_plex_unification_id(None, None, "plex_srv1", "rk42")
        assert result == "plexsrc://plex_srv1/rk42"

    def test_no_ids_never_merges_two_different_items_by_title_year(self):
        """Two DIFFERENT items with neither guid must get DISTINCT keys even
        though a title+year fallback (like the Xtream-side function has)
        would otherwise merge same-title homonyms across sources."""
        a = calculate_plex_unification_id(None, None, "plex_srv1", "rk1")
        b = calculate_plex_unification_id(None, None, "plex_srv1", "rk2")
        assert a != b

    def test_same_source_and_rating_key_is_deterministic(self):
        a = calculate_plex_unification_id(None, None, "plex_srv1", "rk1")
        b = calculate_plex_unification_id(None, None, "plex_srv1", "rk1")
        assert a == b

    def test_empty_strings_treated_as_absent(self):
        result = calculate_plex_unification_id("", "", "plex_srv1", "rk1")
        assert result == "plexsrc://plex_srv1/rk1"


# ─── tmdb -> imdb post-pass bridge ───────────────────────────────────────


def _item(server_id, rating_key, *, media_type="movie", imdb_id=None, tmdb_id=None, unification_id):
    return PlexMediaItem(
        server_id=server_id,
        rating_key=rating_key,
        type=media_type,
        title=f"Title {rating_key}",
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        unification_id=unification_id,
        synced_at=1,
    )


class TestBridgeTmdbToImdb:
    async def test_tmdb_only_item_converges_to_imdb_via_shared_tmdb_id(self, db_factory):
        async with db_factory() as db:
            db.add_all([
                _item("plex_srvA", "100", imdb_id="tt0110912", tmdb_id="680",
                      unification_id="imdb://tt0110912"),
                _item("plex_srvB", "200", imdb_id=None, tmdb_id="680",
                      unification_id="tmdb://680"),
            ])
            await db.commit()

        updated = await _bridge_tmdb_to_imdb(db_factory)
        assert updated == 1

        async with db_factory() as db:
            from sqlalchemy import select

            result = await db.execute(
                select(PlexMediaItem.unification_id).where(
                    PlexMediaItem.server_id == "plex_srvB", PlexMediaItem.rating_key == "200",
                )
            )
            assert result.scalar_one() == "imdb://tt0110912"

    async def test_item_with_imdb_already_is_left_untouched(self, db_factory):
        async with db_factory() as db:
            db.add_all([
                _item("plex_srvA", "1", imdb_id="tt111", tmdb_id="1",
                      unification_id="imdb://tt111"),
                _item("plex_srvB", "2", imdb_id="tt222", tmdb_id="1",
                      unification_id="imdb://tt222"),
            ])
            await db.commit()

        await _bridge_tmdb_to_imdb(db_factory)

        async with db_factory() as db:
            from sqlalchemy import select

            result = await db.execute(
                select(PlexMediaItem.unification_id).where(
                    PlexMediaItem.server_id == "plex_srvB", PlexMediaItem.rating_key == "2",
                )
            )
            # Already had its own imdb id — the bridge must never overwrite it.
            assert result.scalar_one() == "imdb://tt222"

    async def test_no_shared_tmdb_id_leaves_everything_unchanged(self, db_factory):
        async with db_factory() as db:
            db.add_all([
                _item("plex_srvA", "1", imdb_id=None, tmdb_id="10", unification_id="tmdb://10"),
                _item("plex_srvB", "2", imdb_id=None, tmdb_id="20", unification_id="tmdb://20"),
            ])
            await db.commit()

        updated = await _bridge_tmdb_to_imdb(db_factory)
        assert updated == 0

    async def test_episodes_are_excluded_from_bridging(self, db_factory):
        async with db_factory() as db:
            db.add_all([
                _item("plex_srvA", "1", imdb_id="tt999", tmdb_id="55", unification_id="imdb://tt999"),
                _item("plex_srvB", "ep1", media_type="episode", imdb_id=None, tmdb_id="55",
                      unification_id=None),
            ])
            await db.commit()

        await _bridge_tmdb_to_imdb(db_factory)

        async with db_factory() as db:
            from sqlalchemy import select

            result = await db.execute(
                select(PlexMediaItem.unification_id).where(
                    PlexMediaItem.server_id == "plex_srvB", PlexMediaItem.rating_key == "ep1",
                )
            )
            assert result.scalar_one() is None
