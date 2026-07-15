"""tests/test_plex_download_enqueue.py — Plex-sourced download enqueue
(feature "Télécharger Plex", ticket C5).

Covers `app.services.plex_download_service.enqueue_plex_selection`'s four
scopes (movie / series_all / seasons / episodes), non-terminal dedup,
the `DOWNLOAD_DIR`-disabled guard, missing item/server errors, and
`dest_path` correctness — all delegated to the SAME `compute_dest_path`
used by the Xtream enqueue path, so the on-disk layout is identical
regardless of source.

Fixtures insert `PlexServer`/`PlexMediaItem` rows directly (no Plex sync
worker involved yet — that's a different ticket).
"""
from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.models.database import DownloadJob, PlexMediaItem, PlexServer
from app.services import plex_download_service
from app.utils.server_id import build_plex_server_id
from app.utils.time import now_ms

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

CID = "abc123cid"
SERVER_ID = build_plex_server_id(CID)
CID2 = "def456cid"
SERVER_ID2 = build_plex_server_id(CID2)


def _server(cid: str = CID, *, access_token: str = "tok-xyz") -> PlexServer:
    return PlexServer(
        client_identifier=cid, name="Mon PMS", owned=True,
        access_token=access_token, base_uri="https://1-2-3-4.plex.direct:32400",
        is_reachable=True, created_at=now_ms(), updated_at=now_ms(),
    )


def _movie(server_id: str = SERVER_ID, rating_key: str = "1001", **kw) -> PlexMediaItem:
    defaults = dict(
        server_id=server_id, rating_key=rating_key, type="movie",
        title="Blade Runner", year=1982, container="mkv",
        part_key=f"/library/parts/{rating_key}/file.mkv",
        synced_at=now_ms(),
    )
    defaults.update(kw)
    return PlexMediaItem(**defaults)


def _show(server_id: str = SERVER_ID, rating_key: str = "2001", title: str = "Firefly", **kw) -> PlexMediaItem:
    defaults = dict(
        server_id=server_id, rating_key=rating_key, type="show",
        title=title, year=2002, synced_at=now_ms(),
    )
    defaults.update(kw)
    return PlexMediaItem(**defaults)


def _episode(server_id: str, show_rk: str, ep_rk: str, *, season: int, episode: int, **kw) -> PlexMediaItem:
    defaults = dict(
        server_id=server_id, rating_key=ep_rk, type="episode",
        title=f"Episode {episode}", grandparent_rating_key=show_rk,
        parent_index=season, index=episode, container="mkv",
        part_key=f"/library/parts/{ep_rk}/file.mkv",
        synced_at=now_ms(),
    )
    defaults.update(kw)
    return PlexMediaItem(**defaults)


class TestDownloadDirDisabled:
    async def test_dir_unset_returns_error_without_touching_db(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "DOWNLOAD_DIR", "")
        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="u1", scope="movie",
            server_id=SERVER_ID, rating_key="1001",
        )
        assert result.jobs == []
        assert result.batch_id is None
        assert result.error


class TestEnqueueMovie:
    async def test_enqueues_single_movie_job(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_movie())
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="tmdb://78", scope="movie",
            server_id=SERVER_ID, rating_key="1001",
        )
        assert result.error is None
        assert result.batch_id is None
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job.media_type == "movie"
        assert job.server_id == SERVER_ID
        assert job.rating_key == "1001"
        assert job.dest_path == "Movies/Blade Runner (1982)/Blade Runner (1982).mkv"
        assert job.state == "queued"

    async def test_ext_derived_from_container_default_mkv(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_movie(rating_key="1002", container=None))
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="", scope="movie",
            server_id=SERVER_ID, rating_key="1002",
        )
        assert result.error is None
        assert result.jobs[0].dest_path.endswith(".mkv")

    async def test_ext_derived_from_actual_container(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_movie(rating_key="1003", container="mp4"))
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="", scope="movie",
            server_id=SERVER_ID, rating_key="1003",
        )
        assert result.jobs[0].dest_path.endswith(".mp4")

    async def test_unknown_item_errors(self, db_session, download_dir):
        db_session.add(_server())
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="", scope="movie",
            server_id=SERVER_ID, rating_key="nope",
        )
        assert result.jobs == []
        assert result.error

    async def test_missing_selection_errors(self, db_session, download_dir):
        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="", scope="movie",
            server_id=None, rating_key=None,
        )
        assert result.jobs == []
        assert result.error

    async def test_dedup_returns_existing_non_terminal_job(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_movie())
        await db_session.commit()

        first = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="u1", scope="movie",
            server_id=SERVER_ID, rating_key="1001",
        )
        second = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="u1", scope="movie",
            server_id=SERVER_ID, rating_key="1001",
        )
        assert second.error is None
        assert len(second.jobs) == 1
        assert second.jobs[0].id == first.jobs[0].id

        rows = (await db_session.execute(select(DownloadJob))).scalars().all()
        assert len(rows) == 1

    async def test_no_url_or_token_field_on_download_job(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_movie())
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="", scope="movie",
            server_id=SERVER_ID, rating_key="1001",
        )
        job = result.jobs[0]
        column_names = {c.name for c in DownloadJob.__table__.columns}
        assert not any("url" in name.lower() or "token" in name.lower() for name in column_names)
        for column in DownloadJob.__table__.columns:
            value = getattr(job, column.name)
            if isinstance(value, str):
                assert "tok-xyz" not in value


class TestEnqueueSeriesAll:
    async def test_enqueues_one_job_per_episode_across_seasons(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_show())
        db_session.add(_episode(SERVER_ID, "2001", "e1", season=1, episode=1))
        db_session.add(_episode(SERVER_ID, "2001", "e2", season=1, episode=2))
        db_session.add(_episode(SERVER_ID, "2001", "e3", season=2, episode=1))
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="tmdb://tv/1", scope="series_all",
            server_id=SERVER_ID, rating_key="2001",
        )
        assert result.error is None
        assert result.batch_id is not None
        assert len(result.jobs) == 3
        dest_paths = sorted(j.dest_path for j in result.jobs)
        assert dest_paths == sorted([
            "Series/Firefly/Season 01/Firefly - S01E01.mkv",
            "Series/Firefly/Season 01/Firefly - S01E02.mkv",
            "Series/Firefly/Season 02/Firefly - S02E01.mkv",
        ])
        assert all(j.batch_id == result.batch_id for j in result.jobs)
        assert all(j.media_type == "episode" for j in result.jobs)
        assert all(j.unification_id == "tmdb://tv/1" for j in result.jobs)

    async def test_unknown_show_errors(self, db_session, download_dir):
        db_session.add(_server())
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="series_all",
            server_id=SERVER_ID, rating_key="nope",
        )
        assert result.jobs == []
        assert result.error

    async def test_show_with_no_episodes_errors(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_show(rating_key="2099"))
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="series_all",
            server_id=SERVER_ID, rating_key="2099",
        )
        assert result.jobs == []
        assert result.error

    async def test_dedup_reuses_non_terminal_episode_job(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_show())
        db_session.add(_episode(SERVER_ID, "2001", "e1", season=1, episode=1))
        await db_session.commit()

        first = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="series_all",
            server_id=SERVER_ID, rating_key="2001",
        )
        second = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="series_all",
            server_id=SERVER_ID, rating_key="2001",
        )
        assert len(second.jobs) == 1
        assert second.jobs[0].id == first.jobs[0].id
        # A NEW batch is still created (matches Xtream series_all behaviour),
        # but no new DownloadJob row for the already-queued episode.
        rows = (await db_session.execute(select(DownloadJob))).scalars().all()
        assert len(rows) == 1


class TestEnqueueSeasons:
    async def test_enqueues_episodes_for_selected_seasons_across_servers(self, db_session, download_dir):
        db_session.add(_server(CID))
        db_session.add(_server(CID2))
        db_session.add(_show(server_id=SERVER_ID, rating_key="2001", title="Firefly"))
        db_session.add(_show(server_id=SERVER_ID2, rating_key="3001", title="Firefly HD"))
        db_session.add(_episode(SERVER_ID, "2001", "e1", season=1, episode=1))
        db_session.add(_episode(SERVER_ID, "2001", "e2", season=2, episode=1))  # not selected (season 2)
        db_session.add(_episode(SERVER_ID2, "3001", "f1", season=1, episode=1))
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="u1", scope="seasons",
            season_picks=[(1, SERVER_ID, "2001"), (1, SERVER_ID2, "3001")],
        )
        assert result.error is None
        assert result.batch_id is not None
        assert len(result.jobs) == 2
        server_ids = {j.server_id for j in result.jobs}
        assert server_ids == {SERVER_ID, SERVER_ID2}
        assert all(j.season == 1 for j in result.jobs)
        dest_paths = {j.dest_path for j in result.jobs}
        assert "Series/Firefly/Season 01/Firefly - S01E01.mkv" in dest_paths
        assert "Series/Firefly HD/Season 01/Firefly HD - S01E01.mkv" in dest_paths

    async def test_empty_selection_errors(self, db_session, download_dir):
        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="seasons",
            season_picks=[],
        )
        assert result.jobs == []
        assert result.error

    async def test_none_selection_errors(self, db_session, download_dir):
        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="seasons",
            season_picks=None,
        )
        assert result.jobs == []
        assert result.error


class TestEnqueueEpisodes:
    async def test_enqueues_selected_episodes_across_servers(self, db_session, download_dir):
        db_session.add(_server(CID))
        db_session.add(_server(CID2))
        db_session.add(_show(server_id=SERVER_ID, rating_key="2001", title="Firefly"))
        db_session.add(_show(server_id=SERVER_ID2, rating_key="3001", title="Firefly HD"))
        db_session.add(_episode(SERVER_ID, "2001", "e1", season=1, episode=1))
        db_session.add(_episode(SERVER_ID2, "3001", "f2", season=1, episode=2))
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="u1", scope="episodes",
            episode_picks=[(SERVER_ID, "e1"), (SERVER_ID2, "f2")],
        )
        assert result.error is None
        assert result.batch_id is not None
        assert len(result.jobs) == 2
        dest_paths = {j.dest_path for j in result.jobs}
        assert "Series/Firefly/Season 01/Firefly - S01E01.mkv" in dest_paths
        assert "Series/Firefly HD/Season 01/Firefly HD - S01E02.mkv" in dest_paths

    async def test_unknown_episode_is_skipped_not_fatal(self, db_session, download_dir):
        db_session.add(_server())
        db_session.add(_show())
        db_session.add(_episode(SERVER_ID, "2001", "e1", season=1, episode=1))
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="episodes",
            episode_picks=[(SERVER_ID, "e1"), (SERVER_ID, "nope")],
        )
        assert result.error is None
        assert len(result.jobs) == 1
        assert result.jobs[0].rating_key == "e1"

    async def test_empty_selection_errors(self, db_session, download_dir):
        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="episodes",
            episode_picks=[],
        )
        assert result.jobs == []
        assert result.error

    async def test_all_unknown_episodes_errors(self, db_session, download_dir):
        db_session.add(_server())
        await db_session.commit()

        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="show", unification_id="", scope="episodes",
            episode_picks=[(SERVER_ID, "nope1"), (SERVER_ID, "nope2")],
        )
        assert result.jobs == []
        assert result.error


class TestUnknownScope:
    async def test_unknown_scope_errors(self, db_session, download_dir):
        result = await plex_download_service.enqueue_plex_selection(
            db_session, media_type="movie", unification_id="", scope="bogus",
        )
        assert result.jobs == []
        assert result.error
