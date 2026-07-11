"""Tests for the optional unification_id filter on /movies/unified and /shows/unified.

Three scenarios per media type:
  (a) known unification_id → exactly one item, versions[] populated correctly.
  (b) unknown unification_id → empty list, total=0, has_more=False.
  (c) no unification_id → list/search/pagination path unchanged (regression guard).

Uses the service-level fixture (db_session) so we can seed rows without HTTP,
mirroring the style of TestUnifiedApiService in test_plex_dedup.py.
"""
import pytest

from app.models.database import Media, XtreamAccount
from app.utils.server_id import build_server_id


# ─── helpers ────────────────────────────────────────────────────────────────


def _account(id_: str, label: str) -> XtreamAccount:
    return XtreamAccount(
        id=id_, label=label, base_url=f"http://{id_}.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


def _movie(account_id: str, rating_key: str, title: str, unif: str,
           page_offset: int = 0) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title=title, type="movie", year=1984, unification_id=unif,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
    )


def _show(account_id: str, rating_key: str, title: str, unif: str,
          page_offset: int = 0) -> Media:
    return Media(
        rating_key=rating_key, server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=title, type="show", year=2008, unification_id=unif,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
    )


# ─── /movies/unified?unification_id= ────────────────────────────────────────


class TestMoviesUnifiedById:

    @pytest.mark.asyncio
    async def test_known_unification_id_returns_single_group_with_versions(
        self, db_session,
    ):
        """(a) Exact match: one group, versions[] carries server_id + label."""
        from app.services.media_service import media_service
        from app.api.media import _build_versions

        db_session.add_all([
            _account("a", "Compte 1"),
            _account("b", "Compte 2"),
            # Two versions of the same film across two accounts.
            _movie("a", "vod_1.mp4", "Terminator (1984) (VF)", "tmdb://218", 0),
            _movie("b", "vod_9.mp4", "Terminator (1984) (HD)", "tmdb://218", 0),
            # Unrelated film that must NOT appear in the result.
            _movie("a", "vod_5.mp4", "Alien (1979)", "imdb://tt0078748", 1),
        ])
        await db_session.commit()

        group = await media_service.get_unified_group(db_session, "movie", "tmdb://218")

        assert group is not None, "expected a group for tmdb://218"
        assert group.key == "tmdb://218"
        assert len(group.members) == 2

        labels = await media_service.account_labels(db_session)
        versions = _build_versions(group.members, labels)

        assert len(versions) == 2
        assert {v.server_id for v in versions} == {"xtream_a", "xtream_b"}
        # Labels carry the qualifier (VF/HD) + account name.
        label_set = {v.label for v in versions}
        assert "VF · Compte 1" in label_set
        assert "HD · Compte 2" in label_set
        # Verify the fields the Android app reads.
        for v in versions:
            assert v.rating_key  # non-empty
            assert v.server_id.startswith("xtream_")

    @pytest.mark.asyncio
    async def test_unknown_unification_id_returns_none(self, db_session):
        """(b) No rows match → service returns None → endpoint total=0."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _movie("a", "vod_1.mp4", "Alien (1979)", "imdb://tt0078748", 0),
        ])
        await db_session.commit()

        result = await media_service.get_unified_group(
            db_session, "movie", "imdb://tt9999999",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_unification_id_list_path_unchanged(self, db_session):
        """(c) Regression guard: no filter → existing paginated list still works."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _movie("a", "vod_1.mp4", "Terminator (1984) (VF)", "tmdb://218", 0),
            _movie("a", "vod_5.mp4", "Alien (1979)", "imdb://tt0078748", 1),
        ])
        await db_session.commit()

        groups, total = await media_service.get_unified_list(
            db_session, "movie", limit=200, offset=0,
        )
        assert total == 2
        keys = {g.key for g in groups}
        assert "tmdb://218" in keys
        assert "imdb://tt0078748" in keys


# ─── /shows/unified?unification_id= ─────────────────────────────────────────


class TestShowsUnifiedById:

    @pytest.mark.asyncio
    async def test_known_unification_id_returns_single_group_with_versions(
        self, db_session,
    ):
        """(a) Exact match for a show: versions[] populated from both accounts."""
        from app.services.media_service import media_service
        from app.api.media import _build_versions

        db_session.add_all([
            _account("a", "Compte 1"),
            _account("b", "Compte 2"),
            _show("a", "series_1", "Breaking Bad", "tmdb://1396", 0),
            _show("b", "series_9", "Breaking Bad", "tmdb://1396", 0),
            # Different show that must not appear.
            _show("a", "series_2", "Westworld", "tmdb://63247", 1),
        ])
        await db_session.commit()

        group = await media_service.get_unified_group(db_session, "show", "tmdb://1396")

        assert group is not None
        assert group.key == "tmdb://1396"
        assert len(group.members) == 2

        labels = await media_service.account_labels(db_session)
        versions = _build_versions(group.members, labels)

        assert len(versions) == 2
        assert {v.server_id for v in versions} == {"xtream_a", "xtream_b"}

    @pytest.mark.asyncio
    async def test_unknown_unification_id_returns_none(self, db_session):
        """(b) No show matches → service returns None."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _show("a", "series_1", "Breaking Bad", "tmdb://1396", 0),
        ])
        await db_session.commit()

        result = await media_service.get_unified_group(
            db_session, "show", "tmdb://0000000",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_unification_id_list_path_unchanged(self, db_session):
        """(c) Regression guard: no filter → paginated list works as before."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _show("a", "series_1", "Breaking Bad", "tmdb://1396", 0),
            _show("a", "series_2", "Westworld", "tmdb://63247", 1),
        ])
        await db_session.commit()

        groups, total = await media_service.get_unified_list(
            db_session, "show", limit=200, offset=0,
        )
        assert total == 2
        keys = {g.key for g in groups}
        assert "tmdb://1396" in keys
        assert "tmdb://63247" in keys

    @pytest.mark.asyncio
    async def test_type_isolation_movie_filter_does_not_match_show(self, db_session):
        """get_unified_group must respect media_type — a show row must not be
        returned when media_type='movie' is requested for the same unification_id."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _show("a", "series_1", "Breaking Bad", "tmdb://1396", 0),
        ])
        await db_session.commit()

        # Asking for a MOVIE with the same unification_id used by a SHOW → None.
        result = await media_service.get_unified_group(
            db_session, "movie", "tmdb://1396",
        )
        assert result is None


# ─── CR-F05: split-identity convergence via the by-id endpoint ─────────────


class TestUnifiedGroupConvergence:
    """CR-F05: get_unified_group must return the SAME group `_converge` would
    build for the list endpoint — including "twin" rows whose own
    unification_id differs from the one requested because
    calculate_unification_id's imdb>tmdb>title priority split a single title
    across accounts (imdb+tmdb row -> `imdb://…`, tmdb-only row -> `tmdb://…`).
    """

    @pytest.mark.asyncio
    async def test_split_identity_twin_returns_all_versions_by_tmdb_id(
        self, db_session,
    ):
        """Row A carries BOTH imdb_id and tmdb_id (keys as imdb://…, higher
        priority). Row B (a different account's copy of the SAME film) only
        resolved tmdb_id, so it keys as tmdb://… — a "split identity" twin
        that the list endpoint's _converge folds into ONE group. Querying by
        the tmdb:// id (row B's own key) must return BOTH versions, not just
        row B alone."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _account("b", "Compte 2"),
            Media(
                rating_key="vod_1.mp4", server_id=build_server_id("a"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title="Alien (1979)", type="movie", year=1979,
                imdb_id="tt0078748", tmdb_id="218",
                unification_id="imdb://tt0078748",
                page_offset=0, is_in_allowed_categories=True, is_broken=False,
            ),
            Media(
                rating_key="vod_9.mp4", server_id=build_server_id("b"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title="Alien (1979) (HD)", type="movie", year=1979,
                imdb_id=None, tmdb_id="218",
                unification_id="tmdb://218",
                page_offset=0, is_in_allowed_categories=True, is_broken=False,
            ),
        ])
        await db_session.commit()

        group = await media_service.get_unified_group(db_session, "movie", "tmdb://218")

        assert group is not None
        assert len(group.members) == 2, (
            "CR-F05: the imdb-keyed twin must be folded in, not dropped"
        )
        assert {m.rating_key for m in group.members} == {"vod_1.mp4", "vod_9.mp4"}
        # After convergence the surviving key is the higher-priority imdb one
        # (same as the list endpoint would produce for this title).
        assert group.key == "imdb://tt0078748"

    @pytest.mark.asyncio
    async def test_split_identity_twin_returns_all_versions_by_imdb_id(
        self, db_session,
    ):
        """Symmetric case: querying by the imdb:// id (row A's own key) must
        also return both versions."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            _account("b", "Compte 2"),
            Media(
                rating_key="vod_1.mp4", server_id=build_server_id("a"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title="Alien (1979)", type="movie", year=1979,
                imdb_id="tt0078748", tmdb_id="218",
                unification_id="imdb://tt0078748",
                page_offset=0, is_in_allowed_categories=True, is_broken=False,
            ),
            Media(
                rating_key="vod_9.mp4", server_id=build_server_id("b"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title="Alien (1979) (HD)", type="movie", year=1979,
                imdb_id=None, tmdb_id="218",
                unification_id="tmdb://218",
                page_offset=0, is_in_allowed_categories=True, is_broken=False,
            ),
        ])
        await db_session.commit()

        group = await media_service.get_unified_group(
            db_session, "movie", "imdb://tt0078748",
        )

        assert group is not None
        assert len(group.members) == 2
        assert {m.rating_key for m in group.members} == {"vod_1.mp4", "vod_9.mp4"}

    @pytest.mark.asyncio
    async def test_unrelated_same_year_title_is_not_absorbed(self, db_session):
        """A same-year, unrelated movie must NOT show up as a spurious
        version — the bounded candidate widening (same year) must never
        override the exact _converge title-normalization check."""
        from app.services.media_service import media_service

        db_session.add_all([
            _account("a", "Compte 1"),
            Media(
                rating_key="vod_1.mp4", server_id=build_server_id("a"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title="Alien (1979)", type="movie", year=1979,
                imdb_id="tt0078748", tmdb_id="218",
                unification_id="imdb://tt0078748",
                page_offset=0, is_in_allowed_categories=True, is_broken=False,
            ),
            Media(
                rating_key="vod_2.mp4", server_id=build_server_id("a"),
                filter="all", sort_order="default", library_section_id="xtream_vod",
                title="Mad Max (1979)", type="movie", year=1979,
                imdb_id="tt0079501", tmdb_id="8455",
                unification_id="imdb://tt0079501",
                page_offset=1, is_in_allowed_categories=True, is_broken=False,
            ),
        ])
        await db_session.commit()

        group = await media_service.get_unified_group(
            db_session, "movie", "imdb://tt0078748",
        )

        assert group is not None
        assert len(group.members) == 1
        assert group.members[0].rating_key == "vod_1.mp4"
