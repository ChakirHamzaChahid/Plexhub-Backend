"""`download_nfo.render_media_nfo` — map a single downloaded `Media` row onto a
Jellyfin/Kodi `.nfo` (sidecar written next to the file by the download worker).

Pure/sync: no DB, no event loop — `Media` is instantiated in-memory just to
carry attributes.
"""
from __future__ import annotations

from app.models.database import Media, PlexMediaItem
from app.services.download_nfo import render_media_nfo, render_plex_media_nfo


def _movie() -> Media:
    return Media(
        rating_key="vod_1.mkv", server_id="xtream_acc", type="movie",
        title="Some Film", year=2021, summary="A plot.", genres="Action,Drame",
        content_rating="PG-13", duration=3_600_000, imdb_id="tt0000001",
        tmdb_id="12345", display_rating="7.5", cast="Alice,Bob",
        thumb_url="http://x/p.jpg", art_url="http://x/a.jpg", is_adult=False,
    )


def _episode() -> Media:
    return Media(
        rating_key="ep_5.mkv", server_id="xtream_acc", type="episode",
        grandparent_title="My Show", parent_index=2, index=5,
        title="The One With The Test", summary="Episode plot.",
        duration=1_800_000, thumb_url="http://x/thumb.jpg",
    )


class TestRenderMovieNfo:
    def test_contains_core_movie_fields(self):
        xml = render_media_nfo(_movie())
        assert xml is not None
        assert "<movie>" in xml
        assert "<title>Some Film</title>" in xml
        assert "<year>2021</year>" in xml
        assert "<plot>A plot.</plot>" in xml
        assert "<mpaa>PG-13</mpaa>" in xml
        assert "<runtime>60</runtime>" in xml           # 3.6e6 ms -> 60 min
        assert 'type="tmdb"' in xml and "12345" in xml
        assert "Action" in xml and "Drame" in xml


class TestRenderEpisodeNfo:
    def test_contains_core_episode_fields(self):
        xml = render_media_nfo(_episode())
        assert xml is not None
        assert "<episodedetails>" in xml
        assert "<showtitle>My Show</showtitle>" in xml
        assert "<season>2</season>" in xml
        assert "<episode>5</episode>" in xml
        assert "<plot>Episode plot.</plot>" in xml
        assert "<runtime>30</runtime>" in xml           # 1.8e6 ms -> 30 min

    def test_missing_season_episode_default_to_zero(self):
        ep = Media(
            rating_key="ep_x", server_id="s", type="episode",
            grandparent_title="S", title="E",
        )  # parent_index / index left None
        xml = render_media_nfo(ep)
        assert "<season>0</season>" in xml
        assert "<episode>0</episode>" in xml


class TestRenderUnsupported:
    def test_show_type_returns_none(self):
        show = Media(rating_key="series_1", server_id="s", type="show", title="S")
        assert render_media_nfo(show) is None


# --- Plex sidecar NFO (W0 — resolves board DL-PLEX-03) ----------------------


def _plex_movie() -> PlexMediaItem:
    return PlexMediaItem(
        server_id="plex_cid", rating_key="1001", type="movie",
        title="Plex Film", year=2019, imdb_id="tt0000009", tmdb_id="777",
        genres="Action, Thriller", duration_ms=5_400_000, synced_at=1,
    )


def _plex_episode() -> PlexMediaItem:
    return PlexMediaItem(
        server_id="plex_cid", rating_key="2002", type="episode",
        title="Plex Ep", parent_index=3, index=7, duration_ms=1_500_000, synced_at=1,
    )


class TestRenderPlexMovieNfo:
    def test_contains_core_fields(self):
        xml = render_plex_media_nfo(_plex_movie())
        assert xml is not None
        assert "<movie>" in xml
        assert "<title>Plex Film</title>" in xml
        assert "<year>2019</year>" in xml
        assert "<runtime>90</runtime>" in xml            # 5.4e6 ms -> 90 min
        assert 'type="tmdb"' in xml and "777" in xml
        assert "tt0000009" in xml
        assert "Action" in xml and "Thriller" in xml

    def test_no_thumb_or_summary_embedded(self):
        # thumb_url needs the per-server token to resolve -> never emitted.
        xml = render_plex_media_nfo(_plex_movie())
        assert "<thumb" not in xml


class TestRenderPlexEpisodeNfo:
    def test_contains_core_fields(self):
        xml = render_plex_media_nfo(_plex_episode())
        assert xml is not None
        assert "<episodedetails>" in xml
        assert "<season>3</season>" in xml
        assert "<episode>7</episode>" in xml
        assert "<runtime>25</runtime>" in xml            # 1.5e6 ms -> 25 min


class TestRenderPlexUnsupported:
    def test_show_type_returns_none(self):
        show = PlexMediaItem(
            server_id="plex_cid", rating_key="3003", type="show", title="S", synced_at=1,
        )
        assert render_plex_media_nfo(show) is None
