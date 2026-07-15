"""Xtream download granularity (per-season / per-episode, with source-size
helpers) — ticket X2, `docs/31-board.md`.

Additive to `app.services.download_service`:
  - `enqueue_selection(scope='seasons'|'episodes', season_picks=..., episode_picks=...)`
    — a NEW granular picker alongside the existing `movie`/`series_all`/
    `series_seasons` scopes (those are exercised, unchanged, in
    `tests/test_download_service.py` — this file never touches them).
  - `list_series_seasons_with_sources` / `list_series_episodes_with_sources`
    — read-only per-source size/estimate breakdown.
  - `estimate_media_size` — the pure `(size_bytes, is_estimated)` fallback
    chain the two list helpers (and, later, X3's version picker) share.

No FastAPI here — pure service-layer tests against `db_session`/`db_engine`
from `tests/conftest.py`. pytest-asyncio auto mode (pyproject.toml) — async
tests need no decorator.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from app.models.database import DownloadBatch, DownloadJob, Media, XtreamAccount
from app.services import download_service
from app.services.download_service import (
    enqueue_selection,
    estimate_media_size,
    list_series_episodes_with_sources,
    list_series_seasons_with_sources,
)
from app.utils.server_id import build_server_id
from app.utils.time import now_ms

ACCOUNT_A = "accA"
ACCOUNT_B = "accB"
SERVER_A = build_server_id(ACCOUNT_A)
SERVER_B = build_server_id(ACCOUNT_B)

UNI_ID = "tmdb://series-unified"


def _account(account_id: str, *, active: bool = True) -> XtreamAccount:
    return XtreamAccount(
        id=account_id, label=f"Compte {account_id}", base_url="http://provider.example",
        port=80, username="u", password="p", is_active=active, created_at=0,
    )


def _show(
    rating_key: str, server_id: str, *, title: str = "Show", unification_id: str = "",
    page_offset: int = 0,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id, filter="all", sort_order="default",
        library_section_id="xtream_series", title=title, type="show", year=2010,
        unification_id=unification_id or f"tmdb://{rating_key}", page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


def _episode(
    rating_key: str, show_rk: str, server_id: str, *, season: int, episode: int,
    title: str = "Ep", page_offset: int = 0, grandparent_title: str | None = None,
    file_size: int | None = None, media_parts: str = "[]", duration: int | None = None,
) -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id, filter="all", sort_order="default",
        library_section_id="xtream_series", title=title, type="episode",
        grandparent_rating_key=show_rk, grandparent_title=grandparent_title,
        parent_index=season, index=episode, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
        file_size=file_size, media_parts=media_parts, duration=duration,
    )


def _video_media_parts(bitrate_bps: int) -> str:
    """Mirror the JSON shape `sync_worker._build_media_parts` writes for an
    episode/VOD row: a list with one "part" holding a `streams[]` list."""
    return json.dumps([{
        "id": "x", "key": "/stream/x", "duration": None, "file": None, "size": None,
        "container": "mkv",
        "streams": [{
            "type": "VideoStream", "id": "0", "index": 0, "codec": "h264",
            "width": 1920, "height": 1080, "bitrate": bitrate_bps,
            "selected": True, "hasHDR": False,
        }],
    }])


def _job(**kwargs) -> DownloadJob:
    defaults = dict(
        id="job-1", batch_id=None, server_id=SERVER_A, rating_key="ep_a1.mkv",
        media_type="episode", unification_id=None, title="Some Show",
        season=1, episode=1, dest_path="Series/Some Show/Season 01/x - S01E01.mkv",
        state="queued", bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )
    defaults.update(kwargs)
    return DownloadJob(**defaults)


# ─── enqueue_selection(scope='seasons') ─────────────────────────────────────


class TestEnqueueSeasonsGranular:
    async def test_multi_season_multi_account_creates_one_batch(self, db_session, download_dir):
        db_session.add_all([
            _account(ACCOUNT_A), _account(ACCOUNT_B),
            _show("series_a", SERVER_A, title="Show A", page_offset=0),
            _episode("ep_a1.mkv", "series_a", SERVER_A, season=1, episode=1, page_offset=1),
            _episode("ep_a2.mkv", "series_a", SERVER_A, season=1, episode=2, page_offset=2),
            _show("series_b", SERVER_B, title="Show B", page_offset=0),
            _episode("ep_b1.mkv", "series_b", SERVER_B, season=2, episode=1, page_offset=1),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="unified-x",
            scope="seasons",
            season_picks=[(1, SERVER_A, "series_a"), (2, SERVER_B, "series_b")],
        )

        assert result.error is None
        assert result.batch_id is not None
        assert len(result.jobs) == 3
        assert all(j.media_type == "episode" for j in result.jobs)
        assert all(j.batch_id == result.batch_id for j in result.jobs)

        batch = await db_session.get(DownloadBatch, result.batch_id)
        assert batch.scope == "seasons"
        assert batch.total_jobs == 3
        assert batch.media_type == "show"

        by_rk = {j.rating_key: j for j in result.jobs}
        assert by_rk["ep_a1.mkv"].dest_path == "Series/Show A/Season 01/Show A - S01E01.mkv"
        assert by_rk["ep_a2.mkv"].dest_path == "Series/Show A/Season 01/Show A - S01E02.mkv"
        assert by_rk["ep_b1.mkv"].dest_path == "Series/Show B/Season 02/Show B - S02E01.mkv"

    async def test_dedup_reuses_existing_non_terminal_job(self, db_session, download_dir):
        db_session.add_all([
            _account(ACCOUNT_A),
            _show("series_a", SERVER_A, title="Show A", page_offset=0),
            _episode("ep_a1.mkv", "series_a", SERVER_A, season=1, episode=1, page_offset=1),
            _episode("ep_a2.mkv", "series_a", SERVER_A, season=1, episode=2, page_offset=2),
            _job(id="existing", rating_key="ep_a1.mkv", state="running"),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            scope="seasons", season_picks=[(1, SERVER_A, "series_a")],
        )

        ids = {j.id for j in result.jobs}
        assert "existing" in ids
        assert len(result.jobs) == 2  # ep_a1 reused + ep_a2 newly created
        batch = await db_session.get(DownloadBatch, result.batch_id)
        assert batch.total_jobs == 1, "reused (deduped) jobs are not counted as NEW for this batch"

    async def test_empty_or_missing_picks_returns_error_and_creates_nothing(
        self, db_session, download_dir,
    ):
        for picks in (None, []):
            result = await enqueue_selection(
                db_session, media_type="show", unification_id="x",
                scope="seasons", season_picks=picks,
            )
            assert result.jobs == []
            assert result.error == "aucune sélection"
        assert (await db_session.execute(select(DownloadJob))).scalars().all() == []
        assert (await db_session.execute(select(DownloadBatch))).scalars().all() == []

    async def test_no_matching_episodes_returns_error_and_creates_nothing(
        self, db_session, download_dir,
    ):
        db_session.add_all([
            _account(ACCOUNT_A), _show("series_a", SERVER_A, title="Show A", page_offset=0),
            _episode("ep_a1.mkv", "series_a", SERVER_A, season=1, episode=1, page_offset=1),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            scope="seasons", season_picks=[(99, SERVER_A, "series_a")],
        )
        assert result.jobs == []
        assert result.error == "aucun épisode pour les saisons sélectionnées"
        assert (await db_session.execute(select(DownloadJob))).scalars().all() == []
        assert (await db_session.execute(select(DownloadBatch))).scalars().all() == []

    async def test_title_falls_back_to_episode_grandparent_title_when_show_row_missing(
        self, db_session, download_dir,
    ):
        db_session.add_all([
            _account(ACCOUNT_A),
            _episode(
                "ep_a1.mkv", "series_missing", SERVER_A, season=1, episode=1,
                page_offset=1, grandparent_title="Orphan Show",
            ),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            scope="seasons", season_picks=[(1, SERVER_A, "series_missing")],
        )
        assert result.error is None
        assert len(result.jobs) == 1
        assert result.jobs[0].dest_path.startswith("Series/Orphan Show/")

    async def test_download_dir_empty_returns_error(self, db_session, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "DOWNLOAD_DIR", "")
        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x",
            scope="seasons", season_picks=[(1, SERVER_A, "series_a")],
        )
        assert result.jobs == []
        assert result.error == "DOWNLOAD_DIR n'est pas défini"


# ─── enqueue_selection(scope='episodes') ────────────────────────────────────


class TestEnqueueEpisodesGranular:
    async def test_multi_episode_multi_account_creates_one_batch(self, db_session, download_dir):
        db_session.add_all([
            _account(ACCOUNT_A), _account(ACCOUNT_B),
            _show("series_a", SERVER_A, title="Show A", page_offset=0),
            _episode("ep_a1.mkv", "series_a", SERVER_A, season=1, episode=1, page_offset=1),
            _show("series_b", SERVER_B, title="Show B", page_offset=0),
            _episode("ep_b3.mkv", "series_b", SERVER_B, season=3, episode=4, page_offset=1),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x", scope="episodes",
            episode_picks=[(SERVER_A, "ep_a1.mkv"), (SERVER_B, "ep_b3.mkv")],
        )

        assert result.error is None
        assert len(result.jobs) == 2
        batch = await db_session.get(DownloadBatch, result.batch_id)
        assert batch.scope == "episodes"
        assert batch.total_jobs == 2

        by_rk = {j.rating_key: j for j in result.jobs}
        assert by_rk["ep_a1.mkv"].season == 1 and by_rk["ep_a1.mkv"].episode == 1
        assert by_rk["ep_b3.mkv"].season == 3 and by_rk["ep_b3.mkv"].episode == 4
        assert by_rk["ep_a1.mkv"].dest_path == "Series/Show A/Season 01/Show A - S01E01.mkv"
        assert by_rk["ep_b3.mkv"].dest_path == "Series/Show B/Season 03/Show B - S03E04.mkv"

    async def test_dedup_reuses_existing_non_terminal_job(self, db_session, download_dir):
        db_session.add_all([
            _account(ACCOUNT_A), _show("series_a", SERVER_A, title="Show A", page_offset=0),
            _episode("ep_a1.mkv", "series_a", SERVER_A, season=1, episode=1, page_offset=1),
            _episode("ep_a2.mkv", "series_a", SERVER_A, season=1, episode=2, page_offset=2),
            _job(id="existing", rating_key="ep_a1.mkv", state="queued"),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x", scope="episodes",
            episode_picks=[(SERVER_A, "ep_a1.mkv"), (SERVER_A, "ep_a2.mkv")],
        )
        ids = {j.id for j in result.jobs}
        assert "existing" in ids
        assert len(result.jobs) == 2
        batch = await db_session.get(DownloadBatch, result.batch_id)
        assert batch.total_jobs == 1

    async def test_unknown_episode_is_skipped_non_fatal(self, db_session, download_dir):
        db_session.add_all([
            _account(ACCOUNT_A), _show("series_a", SERVER_A, title="Show A", page_offset=0),
            _episode("ep_a1.mkv", "series_a", SERVER_A, season=1, episode=1, page_offset=1),
        ])
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x", scope="episodes",
            episode_picks=[(SERVER_A, "ep_a1.mkv"), (SERVER_A, "ep_missing.mkv")],
        )
        assert result.error is None
        assert len(result.jobs) == 1
        assert result.jobs[0].rating_key == "ep_a1.mkv"

    async def test_all_unknown_returns_error_and_creates_nothing(self, db_session, download_dir):
        db_session.add(_account(ACCOUNT_A))
        await db_session.commit()

        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x", scope="episodes",
            episode_picks=[(SERVER_A, "ep_missing.mkv")],
        )
        assert result.jobs == []
        assert result.error == "aucun épisode disponible"
        assert (await db_session.execute(select(DownloadJob))).scalars().all() == []
        assert (await db_session.execute(select(DownloadBatch))).scalars().all() == []

    async def test_empty_or_missing_picks_returns_error(self, db_session, download_dir):
        for picks in (None, []):
            result = await enqueue_selection(
                db_session, media_type="show", unification_id="x",
                scope="episodes", episode_picks=picks,
            )
            assert result.jobs == []
            assert result.error == "aucune sélection"

    async def test_download_dir_empty_returns_error(self, db_session, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "DOWNLOAD_DIR", "")
        result = await enqueue_selection(
            db_session, media_type="show", unification_id="x", scope="episodes",
            episode_picks=[(SERVER_A, "ep_a1.mkv")],
        )
        assert result.jobs == []
        assert result.error == "DOWNLOAD_DIR n'est pas défini"


# ─── list_series_seasons_with_sources / list_series_episodes_with_sources ──


async def _seed_unified_series(db_session):
    """Two accounts' shows converged under one `unification_id`, season 1
    only. Show A has EXACT sizes on both episodes; show B has an ESTIMATE
    (bitrate x duration) on ep1 and NO size info at all on ep2."""
    db_session.add_all([
        _account(ACCOUNT_A), _account(ACCOUNT_B),
        _show("series_a", SERVER_A, title="Show", unification_id=UNI_ID, page_offset=0),
        _show("series_b", SERVER_B, title="Show", unification_id=UNI_ID, page_offset=0),
        _episode(
            "ep_a1.mkv", "series_a", SERVER_A, season=1, episode=1, page_offset=1,
            title="Pilot", file_size=500_000_000,
        ),
        _episode(
            "ep_a2.mkv", "series_a", SERVER_A, season=1, episode=2, page_offset=2,
            title="Ep2", file_size=600_000_000,
        ),
        _episode(
            "ep_b1.mkv", "series_b", SERVER_B, season=1, episode=1, page_offset=1,
            title="Pilot", media_parts=_video_media_parts(4_000_000), duration=1_200_000,
        ),
        _episode(
            "ep_b2.mkv", "series_b", SERVER_B, season=1, episode=2, page_offset=2,
            title="Ep2",
        ),
    ])
    await db_session.commit()


class TestListSeriesSeasonsWithSources:
    async def test_grouped_by_source_with_summed_size_and_estimate_flag(self, db_session):
        await _seed_unified_series(db_session)

        seasons = await list_series_seasons_with_sources(db_session, UNI_ID)
        assert [s.season for s in seasons] == [1]
        by_server = {s["server_id"]: s for s in seasons[0].sources}

        src_a = by_server[SERVER_A]
        assert src_a["series_rating_key"] == "series_a"
        assert src_a["episode_count"] == 2
        assert src_a["size_bytes"] == 1_100_000_000  # 500M + 600M, both exact
        assert src_a["size_estimated"] is False

        src_b = by_server[SERVER_B]
        assert src_b["series_rating_key"] == "series_b"
        assert src_b["episode_count"] == 2
        assert src_b["size_bytes"] == 600_000_000   # only ep_b1's estimate; ep_b2 unknown
        assert src_b["size_estimated"] is True       # partial sum still flags estimated

    async def test_sources_sorted_by_size_descending_none_last(self, db_session):
        await _seed_unified_series(db_session)
        seasons = await list_series_seasons_with_sources(db_session, UNI_ID)
        sizes = [s["size_bytes"] for s in seasons[0].sources]
        assert sizes[0] == 1_100_000_000  # bigger source (A) first
        assert sizes == sorted(sizes, key=lambda v: (v is None, -(v or 0)))

    async def test_unknown_unification_id_returns_empty_list(self, db_session):
        assert await list_series_seasons_with_sources(db_session, "does-not-exist") == []


class TestListSeriesEpisodesWithSources:
    async def test_per_episode_per_source_size_and_sorting(self, db_session):
        await _seed_unified_series(db_session)

        episodes = await list_series_episodes_with_sources(db_session, UNI_ID, 1)
        assert [(e.season, e.episode) for e in episodes] == [(1, 1), (1, 2)]

        ep1 = episodes[0]
        assert ep1.title == "Pilot"
        by_server = {s["server_id"]: s for s in ep1.sources}
        assert by_server[SERVER_A]["episode_rating_key"] == "ep_a1.mkv"
        assert by_server[SERVER_A]["size_bytes"] == 500_000_000
        assert by_server[SERVER_A]["size_estimated"] is False
        assert by_server[SERVER_B]["episode_rating_key"] == "ep_b1.mkv"
        assert by_server[SERVER_B]["size_bytes"] == 600_000_000
        assert by_server[SERVER_B]["size_estimated"] is True
        # sorted by size desc -> B's 600M estimate before A's 500M exact.
        assert ep1.sources[0]["server_id"] == SERVER_B

        ep2 = episodes[1]
        by_server2 = {s["server_id"]: s for s in ep2.sources}
        assert by_server2[SERVER_A]["size_bytes"] == 600_000_000
        assert by_server2[SERVER_B]["size_bytes"] is None
        # known size sorts before an unknown (None) one.
        assert ep2.sources[0]["server_id"] == SERVER_A

    async def test_season_with_no_episodes_returns_empty(self, db_session):
        await _seed_unified_series(db_session)
        assert await list_series_episodes_with_sources(db_session, UNI_ID, 99) == []

    async def test_unknown_unification_id_returns_empty_list(self, db_session):
        assert await list_series_episodes_with_sources(db_session, "does-not-exist", 1) == []


# ─── estimate_media_size ─────────────────────────────────────────────────────


class TestEstimateMediaSize:
    def test_exact_file_size_wins_regardless_of_media_parts(self):
        row = _episode(
            "ep.mkv", "series_a", SERVER_A, season=1, episode=1,
            file_size=123_456, media_parts=_video_media_parts(9_999_999), duration=1_000_000,
        )
        size, estimated = estimate_media_size(row)
        assert size == 123_456
        assert estimated is False

    def test_estimated_from_bitrate_and_duration(self):
        row = _episode(
            "ep.mkv", "series_a", SERVER_A, season=1, episode=1,
            media_parts=_video_media_parts(4_000_000), duration=1_200_000,  # 4Mbps, 20min
        )
        size, estimated = estimate_media_size(row)
        assert size == 600_000_000
        assert estimated is True

    def test_unknown_when_no_file_size_and_no_media_parts_info(self):
        row = _episode("ep.mkv", "series_a", SERVER_A, season=1, episode=1)
        size, estimated = estimate_media_size(row)
        assert size is None
        assert estimated is False

    def test_unknown_when_bitrate_present_but_no_duration(self):
        row = _episode(
            "ep.mkv", "series_a", SERVER_A, season=1, episode=1,
            media_parts=_video_media_parts(4_000_000), duration=None,
        )
        size, estimated = estimate_media_size(row)
        assert size is None
        assert estimated is False

    def test_malformed_media_parts_json_is_ignored_not_raised(self):
        row = _episode(
            "ep.mkv", "series_a", SERVER_A, season=1, episode=1,
            media_parts="{not json", duration=1_200_000,
        )
        size, estimated = estimate_media_size(row)
        assert size is None
        assert estimated is False

    def test_bit_rate_key_variant_is_also_accepted(self):
        parts = json.dumps([{"streams": [{"type": "VideoStream", "bit_rate": 4_000_000}]}])
        row = _episode(
            "ep.mkv", "series_a", SERVER_A, season=1, episode=1,
            media_parts=parts, duration=1_200_000,
        )
        size, estimated = estimate_media_size(row)
        assert size == 600_000_000
        assert estimated is True

    def test_zero_file_size_is_treated_as_unknown_not_exact(self):
        """`file_size=0` is falsy -> falls through to the estimate/unknown
        chain rather than being reported as an exact 0-byte file."""
        row = _episode(
            "ep.mkv", "series_a", SERVER_A, season=1, episode=1,
            file_size=0, media_parts=_video_media_parts(4_000_000), duration=1_200_000,
        )
        size, estimated = estimate_media_size(row)
        assert size == 600_000_000
        assert estimated is True


# ─── Import surface sanity: `estimate_media_size` importable from module ───


def test_estimate_media_size_is_importable_from_download_service_module():
    """X3 (film/whole-series version picker) imports this directly off the
    module rather than via a named import — guard the public surface."""
    assert download_service.estimate_media_size is estimate_media_size
