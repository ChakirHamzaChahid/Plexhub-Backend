"""`app.services.download_service` — enqueue/list/cancel/retry/clear + the
serialization helpers (PH-DL-07 priority 3, `docs/40-testplan-media-download.md`
Test IDs DL-020..029b, DL-040..044, DL-096).

No FastAPI here — pure service-layer tests against a `db_session`/`db_engine`
from `tests/conftest.py`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.database import DownloadBatch, DownloadJob, Media, XtreamAccount
from app.services import download_service
from app.services.download_service import enqueue_selection, list_series_seasons
from app.utils.server_id import build_server_id
from app.utils.time import now_ms

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

ACCOUNT_ID = "acc1"
SERVER_ID = build_server_id(ACCOUNT_ID)


def _account(*, active: bool = True) -> XtreamAccount:
    return XtreamAccount(
        id=ACCOUNT_ID, label="Compte 1", base_url="http://provider.example", port=80,
        username="u", password="p", is_active=active, created_at=0,
    )


def _movie(rating_key: str, title: str = "Terminator", year: int = 1984) -> Media:
    return Media(
        rating_key=rating_key, server_id=SERVER_ID, filter="all", sort_order="default",
        library_section_id="xtream_vod", title=title, type="movie", year=year,
        unification_id=f"imdb://{rating_key}", is_in_allowed_categories=True, is_broken=False,
    )


def _show(rating_key: str, title: str = "Show A", *, page_offset: int = 0) -> Media:
    # Shares `library_section_id="xtream_series"` with `_episode` rows below —
    # `page_offset` must stay distinct across every show/episode row seeded
    # together (Media's composite unique index), hence the explicit param.
    return Media(
        rating_key=rating_key, server_id=SERVER_ID, filter="all", sort_order="default",
        library_section_id="xtream_series", title=title, type="show", year=2010,
        unification_id=f"tmdb://{rating_key}", page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


def _episode(
    rating_key: str, show_rk: str, *, season: int, episode: int, title: str = "Ep",
    page_offset: int = 0,
) -> Media:
    # `page_offset` participates in `Media`'s composite unique index
    # (server_id, library_section_id, filter, sort_order, page_offset) —
    # every episode row seeded in the same test needs a distinct value.
    return Media(
        rating_key=rating_key, server_id=SERVER_ID, filter="all", sort_order="default",
        library_section_id="xtream_series", title=title, type="episode",
        grandparent_rating_key=show_rk, parent_index=season, index=episode,
        page_offset=page_offset, is_in_allowed_categories=True, is_broken=False,
    )


def _job(**kwargs) -> DownloadJob:
    defaults = dict(
        id="job-1", batch_id=None, server_id=SERVER_ID, rating_key="vod_1.mkv",
        media_type="movie", unification_id=None, title="Some Film",
        season=None, episode=None, dest_path="Movies/Some Film (2020)/Some Film (2020).mkv",
        state="queued", bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )
    defaults.update(kwargs)
    return DownloadJob(**defaults)


# ─── DL-020/021/022: enqueue a movie ────────────────────────────────────────


class TestEnqueueMovie:
    async def test_creates_one_queued_job_with_relative_dest_path(
        self, db_session, download_dir,
    ):
        db_session.add_all([_account(), _movie("vod_1.mkv")])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="movie", unification_id="imdb://vod_1.mkv",
            server_id=SERVER_ID, rating_key="vod_1.mkv", scope="movie",
        )

        assert result.error is None
        assert result.batch_id is None
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job.state == "queued"
        assert job.batch_id is None
        assert not job.dest_path.startswith("/")
        assert job.dest_path.startswith("Movies/")

        rows = (await db_session.execute(select(DownloadJob))).scalars().all()
        assert len(rows) == 1

    async def test_dedup_returns_same_job_for_non_terminal_duplicate(
        self, db_session, download_dir,
    ):
        db_session.add_all([_account(), _movie("vod_1.mkv")])
        await db_session.commit()

        first = await enqueue_selection(
            db_session, media_type="movie", unification_id="x",
            server_id=SERVER_ID, rating_key="vod_1.mkv", scope="movie",
        )
        second = await enqueue_selection(
            db_session, media_type="movie", unification_id="x",
            server_id=SERVER_ID, rating_key="vod_1.mkv", scope="movie",
        )

        assert second.jobs[0].id == first.jobs[0].id
        rows = (await db_session.execute(select(DownloadJob))).scalars().all()
        assert len(rows) == 1, "a non-terminal duplicate must not create a second row"

    async def test_completed_job_is_re_enqueue_able(self, db_session, download_dir):
        """spec §3.2: NOT a unique constraint on (server_id, rating_key) — a
        `completed` job must remain re-enqueue-able (contrast with DL-021)."""
        db_session.add_all([
            _account(), _movie("vod_1.mkv"),
            _job(id="old-completed", state="completed", rating_key="vod_1.mkv"),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="movie", unification_id="x",
            server_id=SERVER_ID, rating_key="vod_1.mkv", scope="movie",
        )

        assert result.error is None
        assert result.jobs[0].id != "old-completed"
        rows = (await db_session.execute(select(DownloadJob))).scalars().all()
        assert len(rows) == 2

    async def test_media_not_found_returns_error_no_job(self, db_session, download_dir):
        db_session.add(_account())
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="movie", unification_id="x",
            server_id=SERVER_ID, rating_key="vod_missing.mkv", scope="movie",
        )
        assert result.jobs == []
        assert result.error == "Media introuvable"
        rows = (await db_session.execute(select(DownloadJob))).scalars().all()
        assert rows == []


# ─── Guards: DOWNLOAD_DIR empty / account missing or inactive ──────────────


class TestEnqueueGuards:
    async def test_download_dir_empty_returns_error_no_job(self, db_session, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "DOWNLOAD_DIR", "")
        db_session.add_all([_account(), _movie("vod_1.mkv")])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="movie", unification_id="x",
            server_id=SERVER_ID, rating_key="vod_1.mkv", scope="movie",
        )
        assert result.jobs == []
        assert result.error == "DOWNLOAD_DIR n'est pas défini"
        rows = (await db_session.execute(select(DownloadJob))).scalars().all()
        assert rows == []

    async def test_unknown_account_returns_error(self, db_session, download_dir):
        result = await enqueue_selection(
            db_session, media_type="movie", unification_id="x",
            server_id="xtream_does-not-exist", rating_key="vod_1.mkv", scope="movie",
        )
        assert result.jobs == []
        assert result.error == "Compte source introuvable ou inactif"

    async def test_inactive_account_returns_error(self, db_session, download_dir):
        db_session.add_all([_account(active=False), _movie("vod_1.mkv")])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="movie", unification_id="x",
            server_id=SERVER_ID, rating_key="vod_1.mkv", scope="movie",
        )
        assert result.jobs == []
        assert result.error == "Compte source introuvable ou inactif"

    async def test_unknown_scope_returns_error(self, db_session, download_dir):
        db_session.add_all([_account(), _movie("vod_1.mkv")])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="movie", unification_id="x",
            server_id=SERVER_ID, rating_key="vod_1.mkv", scope="bogus",
        )
        assert result.jobs == []
        assert "scope inconnu" in result.error


# ─── DL-023/024: enqueue a whole series ────────────────────────────────────


class TestEnqueueSeriesAll:
    async def test_creates_one_job_per_episode_and_one_batch(self, db_session, download_dir):
        db_session.add_all([
            _account(), _show("series_1", page_offset=0),
            _episode("ep_1.mkv", "series_1", season=1, episode=1, page_offset=1),
            _episode("ep_2.mkv", "series_1", season=1, episode=2, page_offset=2),
            _episode("ep_3.mkv", "series_1", season=2, episode=1, page_offset=3),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="tmdb://series_1",
            server_id=SERVER_ID, rating_key="series_1", scope="series_all",
        )

        assert result.error is None
        assert result.batch_id is not None
        assert len(result.jobs) == 3
        assert all(j.batch_id == result.batch_id for j in result.jobs)
        assert all(j.media_type == "episode" for j in result.jobs)
        assert all(j.dest_path.startswith("Series/") for j in result.jobs)

        batch = await db_session.get(DownloadBatch, result.batch_id)
        assert batch.total_jobs == 3
        assert batch.scope == "series_all"

        seasons_episodes = sorted((j.season, j.episode) for j in result.jobs)
        assert seasons_episodes == [(1, 1), (1, 2), (2, 1)]

    async def test_no_episodes_returns_error_and_creates_nothing(self, db_session, download_dir):
        db_session.add_all([_account(), _show("series_empty")])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            server_id=SERVER_ID, rating_key="series_empty", scope="series_all",
        )

        assert result.jobs == []
        assert result.error == "aucun épisode disponible"
        assert (await db_session.execute(select(DownloadJob))).scalars().all() == []
        assert (await db_session.execute(select(DownloadBatch))).scalars().all() == []

    async def test_show_not_found_returns_error(self, db_session, download_dir):
        db_session.add(_account())
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            server_id=SERVER_ID, rating_key="series_missing", scope="series_all",
        )
        assert result.jobs == []
        assert result.error == "Série introuvable"

    async def test_dedup_reuses_non_terminal_episode_job_but_not_completed(
        self, db_session, download_dir,
    ):
        db_session.add_all([
            _account(), _show("series_1", page_offset=0),
            _episode("ep_1.mkv", "series_1", season=1, episode=1, page_offset=1),
            _episode("ep_2.mkv", "series_1", season=1, episode=2, page_offset=2),
            _job(
                id="existing-ep1", rating_key="ep_1.mkv", media_type="episode",
                state="running", dest_path="Series/Show A/Season 01/x - S01E01.mkv",
            ),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            server_id=SERVER_ID, rating_key="series_1", scope="series_all",
        )

        ids = {j.id for j in result.jobs}
        assert "existing-ep1" in ids
        assert len(result.jobs) == 2  # ep1 reused + ep2 newly created
        batch = await db_session.get(DownloadBatch, result.batch_id)
        assert batch.total_jobs == 1, "reused (deduped) jobs are not counted as NEW for this batch"


# ─── DL-02: disk-space préflight (review fix #3, check_free_disk_space) ────


class TestCheckFreeDiskSpace:
    async def test_raises_when_free_space_below_threshold(self, download_dir, monkeypatch):
        from types import SimpleNamespace

        from app.config import settings

        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 2048)
        monkeypatch.setattr(
            download_service.shutil, "disk_usage",
            lambda path: SimpleNamespace(total=0, used=0, free=100 * 1024 * 1024),  # 100 MiB free
        )

        with pytest.raises(download_service.InsufficientDiskSpaceError) as excinfo:
            await download_service.check_free_disk_space()
        assert "insufficient free disk" in str(excinfo.value)
        # A subclass of DownloadPermanentError -> must NOT consume a retry.
        assert isinstance(excinfo.value, download_service.DownloadPermanentError)

    async def test_noop_when_free_space_above_threshold(self, download_dir, monkeypatch):
        from types import SimpleNamespace

        from app.config import settings

        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 100)
        monkeypatch.setattr(
            download_service.shutil, "disk_usage",
            lambda path: SimpleNamespace(total=0, used=0, free=5000 * 1024 * 1024),
        )
        await download_service.check_free_disk_space()  # must not raise

    async def test_disabled_when_threshold_non_positive(self, download_dir, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)

        def _boom(path):
            raise AssertionError("disk_usage must not be called when the préflight is disabled")

        monkeypatch.setattr(download_service.shutil, "disk_usage", _boom)
        await download_service.check_free_disk_space()  # must not raise / must not stat

    async def test_noop_when_download_dir_unset(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "DOWNLOAD_DIR", "")
        monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 2048)

        def _boom(path):
            raise AssertionError("disk_usage must not be called when DOWNLOAD_DIR is unset")

        monkeypatch.setattr(download_service.shutil, "disk_usage", _boom)
        await download_service.check_free_disk_space()  # must not raise / must not stat


# ─── DL-029/029b: compute_dest_path exact shape (spec §5.2) ────────────────


class TestComputeDestPathShape:
    def test_movie_shape(self, download_dir):
        dest = download_service.compute_dest_path(
            media_type="movie", title="Terminator", year=1984,
            season=None, episode=None, ext="mkv",
        )
        assert dest == "Movies/Terminator (1984)/Terminator (1984).mkv"

    def test_episode_shape_zero_padded(self, download_dir):
        dest = download_service.compute_dest_path(
            media_type="episode", title="Show A", year=2010,
            season=1, episode=2, ext="mkv",
        )
        assert dest == "Series/Show A/Season 01/Show A - S01E02.mkv"

    def test_episode_double_digit_season(self, download_dir):
        dest = download_service.compute_dest_path(
            media_type="episode", title="Show A", year=None,
            season=12, episode=34, ext="mp4",
        )
        assert dest == "Series/Show A/Season 12/Show A - S12E34.mp4"

    def test_ext_leading_dot_is_stripped(self, download_dir):
        dest = download_service.compute_dest_path(
            media_type="movie", title="X", year=2000,
            season=None, episode=None, ext=".mkv",
        )
        assert dest.endswith(".mkv")
        assert not dest.endswith("..mkv")

    def test_ext_from_rating_key_fallback_to_ts(self):
        assert download_service._ext_from_rating_key("vod_1") == "ts"
        assert download_service._ext_from_rating_key("vod_1.mkv") == "mkv"


# ─── list_jobs / get_job ────────────────────────────────────────────────────


class TestListAndGetJobs:
    async def test_list_all_ordered_by_created_desc_with_total(self, db_session):
        db_session.add_all([
            _job(id="a", created_at=100), _job(id="b", created_at=300), _job(id="c", created_at=200),
        ])
        await db_session.commit()

        jobs, total = await download_service.list_jobs(db_session)
        assert total == 3
        assert [j.id for j in jobs] == ["b", "c", "a"]

    async def test_list_filters_by_states(self, db_session):
        db_session.add_all([
            _job(id="q", state="queued"), _job(id="r", state="running"),
            _job(id="f", state="failed"), _job(id="d", state="completed"),
        ])
        await db_session.commit()

        jobs, total = await download_service.list_jobs(db_session, states=["queued", "running"])
        assert total == 2
        assert {j.id for j in jobs} == {"q", "r"}

    async def test_list_respects_limit_and_offset(self, db_session):
        db_session.add_all([_job(id=f"j{i}", created_at=i) for i in range(5)])
        await db_session.commit()

        jobs, total = await download_service.list_jobs(db_session, limit=2, offset=1)
        assert total == 5
        assert len(jobs) == 2

    async def test_get_job_found_and_not_found(self, db_session):
        db_session.add(_job(id="only"))
        await db_session.commit()

        assert (await download_service.get_job(db_session, "only")).id == "only"
        assert await download_service.get_job(db_session, "nope") is None


# ─── cancel_job ─────────────────────────────────────────────────────────────


class TestCancelJob:
    async def test_queued_becomes_canceled(self, db_session):
        db_session.add(_job(id="q", state="queued"))
        await db_session.commit()

        job = await download_service.cancel_job(db_session, "q")
        assert job.state == "canceled"
        assert job.finished_at is not None

    async def test_running_becomes_canceled(self, db_session):
        db_session.add(_job(id="r", state="running", started_at=now_ms()))
        await db_session.commit()

        job = await download_service.cancel_job(db_session, "r")
        assert job.state == "canceled"

    async def test_terminal_states_are_noop(self, db_session):
        db_session.add_all([
            _job(id="c1", state="completed"), _job(id="f1", state="failed"),
            _job(id="x1", state="canceled"),
        ])
        await db_session.commit()

        for job_id, expected in (("c1", "completed"), ("f1", "failed"), ("x1", "canceled")):
            job = await download_service.cancel_job(db_session, job_id)
            assert job.state == expected

    async def test_unknown_job_id_returns_none(self, db_session):
        assert await download_service.cancel_job(db_session, "nope") is None


# ─── retry_job ──────────────────────────────────────────────────────────────


class TestRetryJob:
    async def test_failed_becomes_queued_and_resets_attempts_and_error(self, db_session):
        db_session.add(_job(id="f", state="failed", attempts=3, error="upstream 500", finished_at=now_ms()))
        await db_session.commit()

        job = await download_service.retry_job(db_session, "f")
        assert job.state == "queued"
        assert job.attempts == 0
        assert job.error is None
        assert job.finished_at is None

    async def test_canceled_becomes_queued(self, db_session):
        db_session.add(_job(id="x", state="canceled", finished_at=now_ms()))
        await db_session.commit()

        job = await download_service.retry_job(db_session, "x")
        assert job.state == "queued"

    async def test_non_terminal_states_are_noop(self, db_session):
        db_session.add_all([_job(id="q", state="queued"), _job(id="r", state="running")])
        await db_session.commit()

        for job_id, expected in (("q", "queued"), ("r", "running")):
            job = await download_service.retry_job(db_session, job_id)
            assert job.state == expected

    async def test_completed_is_noop(self, db_session):
        db_session.add(_job(id="c", state="completed"))
        await db_session.commit()
        job = await download_service.retry_job(db_session, "c")
        assert job.state == "completed"

    async def test_unknown_job_id_returns_none(self, db_session):
        assert await download_service.retry_job(db_session, "nope") is None


# ─── clear_finished ─────────────────────────────────────────────────────────


class TestClearFinished:
    async def test_removes_only_terminal_jobs(self, db_session):
        db_session.add_all([
            _job(id="q", state="queued"), _job(id="r", state="running"),
            _job(id="c", state="completed"), _job(id="f", state="failed"),
            _job(id="x", state="canceled"),
        ])
        await db_session.commit()

        deleted = await download_service.clear_finished(db_session)
        assert deleted == 3

        remaining = {j.id for j in (await db_session.execute(select(DownloadJob))).scalars().all()}
        assert remaining == {"q", "r"}

    async def test_noop_when_nothing_finished(self, db_session):
        db_session.add(_job(id="q", state="queued"))
        await db_session.commit()
        assert await download_service.clear_finished(db_session) == 0


# ─── DL-041/042/043/096: serialization helpers ─────────────────────────────


class TestComputePercent:
    def test_none_when_no_total(self):
        assert download_service.compute_percent(_job(bytes_total=None, bytes_done=500)) is None

    def test_none_when_total_is_zero(self):
        assert download_service.compute_percent(_job(bytes_total=0, bytes_done=0)) is None

    def test_rounded_to_one_decimal(self):
        job = _job(bytes_total=1000, bytes_done=250)
        assert download_service.compute_percent(job) == 25.0

        job2 = _job(bytes_total=3, bytes_done=1)
        assert download_service.compute_percent(job2) == 33.3


class TestComputeSpeedBps:
    def test_none_when_not_running(self):
        job = _job(state="queued", started_at=now_ms())
        assert download_service.compute_speed_bps(job) is None

    def test_none_when_running_without_started_at(self):
        job = _job(state="running", started_at=None)
        assert download_service.compute_speed_bps(job) is None

    def test_average_bytes_per_second(self):
        started = 1_000_000
        job = _job(state="running", started_at=started, updated_at=started + 4000, bytes_done=800)
        assert download_service.compute_speed_bps(job) == 200.0

    def test_no_division_by_zero_when_elapsed_is_zero(self):
        started = 1_000_000
        job = _job(state="running", started_at=started, updated_at=started, bytes_done=50)
        # max(1, 0) -> 1s floor, never raises ZeroDivisionError.
        assert download_service.compute_speed_bps(job) == 50.0


class TestToDownloadResponse:
    def test_maps_fields_including_renamed_columns(self):
        job = _job(
            id="j1", batch_id="b1", media_type="episode", unification_id="tmdb://1",
            title="Ep 1", season=1, episode=2, server_id=SERVER_ID, rating_key="ep_1.mkv",
            state="running", bytes_done=500, bytes_total=1000, attempts=2,
            dest_path="Series/Show/Season 01/Show - S01E02.mkv", error=None,
            created_at=10, updated_at=20, started_at=15, finished_at=None,
        )
        resp = download_service.to_download_response(job)

        assert resp.job_id == "j1"
        assert resp.batch_id == "b1"
        assert resp.type == "episode"
        assert resp.bytes_downloaded == 500  # from DownloadJob.bytes_done
        assert resp.bytes_total == 1000
        assert resp.percent == 50.0
        assert resp.retries == 2  # from DownloadJob.attempts
        assert resp.dest_path == job.dest_path
        assert resp.created_at == 10
        assert resp.updated_at == 20
        assert resp.started_at == 15
        assert resp.finished_at is None

    def test_bytes_done_none_defaults_to_zero_in_response(self):
        job = _job(bytes_done=None, attempts=None)
        resp = download_service.to_download_response(job)
        assert resp.bytes_downloaded == 0
        assert resp.retries == 0


# ─── Feature: enqueue specific season(s) of a series ────────────────────────


class TestEnqueueSeriesSeasons:
    def _seed(self):
        return [
            _account(), _show("series_1", page_offset=0),
            _episode("ep_1.mkv", "series_1", season=1, episode=1, page_offset=1),
            _episode("ep_2.mkv", "series_1", season=1, episode=2, page_offset=2),
            _episode("ep_3.mkv", "series_1", season=2, episode=1, page_offset=3),
            _episode("ep_4.mkv", "series_1", season=3, episode=1, page_offset=4),
        ]

    async def test_single_season_enqueues_only_that_season(self, db_session, download_dir):
        db_session.add_all(self._seed())
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="tmdb://series_1",
            server_id=SERVER_ID, rating_key="series_1",
            scope="series_seasons", seasons=[1],
        )

        assert result.error is None
        assert len(result.jobs) == 2
        assert sorted((j.season, j.episode) for j in result.jobs) == [(1, 1), (1, 2)]
        batch = await db_session.get(DownloadBatch, result.batch_id)
        assert batch.scope == "series_seasons"
        assert batch.total_jobs == 2

    async def test_multiple_seasons_enqueues_union(self, db_session, download_dir):
        db_session.add_all(self._seed())
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="tmdb://series_1",
            server_id=SERVER_ID, rating_key="series_1",
            scope="series_seasons", seasons=[1, 3],
        )

        assert result.error is None
        assert sorted((j.season, j.episode) for j in result.jobs) == [(1, 1), (1, 2), (3, 1)]

    async def test_empty_seasons_is_an_error_and_creates_nothing(self, db_session, download_dir):
        db_session.add_all(self._seed())
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            server_id=SERVER_ID, rating_key="series_1",
            scope="series_seasons", seasons=[],
        )

        assert result.jobs == []
        assert result.error == "aucune saison sélectionnée"
        assert (await db_session.execute(select(DownloadJob))).scalars().all() == []
        assert (await db_session.execute(select(DownloadBatch))).scalars().all() == []

    async def test_unknown_season_returns_error(self, db_session, download_dir):
        db_session.add_all(self._seed())
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            server_id=SERVER_ID, rating_key="series_1",
            scope="series_seasons", seasons=[99],
        )
        assert result.jobs == []
        assert result.error == "aucun épisode pour les saisons sélectionnées"

    async def test_list_series_seasons_returns_sorted_distinct(self, db_session, download_dir):
        db_session.add_all(self._seed())
        await db_session.commit()

        seasons = await list_series_seasons(db_session, SERVER_ID, "series_1")
        assert seasons == [1, 2, 3]
