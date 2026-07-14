"""`download_nfo.render_media_nfo` — map a single downloaded `Media` row onto a
Jellyfin/Kodi `.nfo` (sidecar written next to the file by the download worker).

Pure/sync: no DB, no event loop — `Media` is instantiated in-memory just to
carry attributes.
"""
from __future__ import annotations

from app.models.database import Media
from app.services.download_nfo import render_media_nfo


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
