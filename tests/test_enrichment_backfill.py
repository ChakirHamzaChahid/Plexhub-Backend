"""tests/test_enrichment_backfill.py -- manual OMDb ratings backfill (Wave 3
of the dual-provider enrichment refacto,
docs/plans/2026-07-20-omdb-rating-enrichment-design.md §C4).

Two layers:
  - HTTP/router (`TestRouterAuth`, `TestPostAndGet`, `TestSingleRunGuard`):
    exercises `POST /api/admin/enrichment/omdb-backfill` +
    `GET /api/admin/enrichment/jobs/{jobId}` through `api_client`, same
    master-key-only convention as `tests/test_download_security.py`
    (`TestApiAdminDownloadsRequiresMasterKey`) / `tests/test_plex_downloads_json.py`.
  - Worker unit tests (`TestPhaseASelection`, `TestBlendCorrectness`,
    `TestPhaseBRecompute`, `TestBudgetFailOpen`): call
    `enrichment_backfill_worker.run_backfill(job_id, db_factory, ...)`
    DIRECTLY (awaited, deterministic) instead of racing the fire-and-forget
    background task — same "awaitable-controllable" discipline as
    `tests/test_embedding_worker.py::test_rebuild_processes_pending_rows`.

Fakes OMDb via a call-counting double (no `AsyncMock`), same style as
`tests/test_enrichment_scraping.py::_FakeOmdbSvc`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import database as db_module
from app.models.database import Media
from app.services import api_key_service
from app.services.omdb_service import OMDbData
from app.utils.rating_blend import blend_rating
from app.workers import enrichment_backfill_worker as backfill

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.

MASTER_KEY = "master-secret-enrichment-backfill"
SERVER_ID = "xtream_acc1"


def _media(
    rating_key: str, *, server_id: str = SERVER_ID, title: str = "Film",
    type: str = "movie", imdb_id: str | None = "tt0000001",
    imdb_rating: float | None = None, imdb_votes: int | None = None,
    tmdb_rating: float | None = None, display_rating: float = 0.0,
    page_offset: int = 0, library_section_id: str = "xtream_vod",
) -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id, filter="all", sort_order="default",
        library_section_id=library_section_id, title=title, type=type,
        page_offset=page_offset,
        unification_id=f"imdb://{imdb_id}" if imdb_id else f"title_{rating_key}",
        imdb_id=imdb_id, imdb_rating=imdb_rating, imdb_votes=imdb_votes,
        tmdb_rating=tmdb_rating, display_rating=display_rating,
        is_in_allowed_categories=True, is_broken=False,
    )


class _FakeOmdbSvc:
    """Call-counting double for `omdb_service` — no AsyncMock (house style,
    see `tests/test_enrichment_scraping.py::_FakeOmdbSvc`)."""

    def __init__(
        self, ratings: dict[str, tuple[float | None, int | None]] | None = None,
        *, configured: bool = True,
    ):
        self._ratings = ratings or {}
        self._configured = configured
        self._count = 0
        self.calls: list[str] = []

    @property
    def is_configured(self) -> bool:
        return self._configured

    def get_request_count(self) -> int:
        return self._count

    def reset_request_count(self) -> None:
        self._count = 0

    async def get_by_imdb_id(self, imdb_id: str) -> OMDbData | None:
        self.calls.append(imdb_id)
        self._count += 1
        entry = self._ratings.get(imdb_id)
        if entry is None:
            return None
        rating, votes = entry
        return OMDbData(
            title="Some Title", year="2000", runtime_minutes=100, genre=None,
            director=None, actors=None, plot=None,
            imdb_rating=rating, imdb_votes=votes, type="movie", imdb_id=imdb_id,
        )


@pytest.fixture(autouse=True)
def _reset_backfill_state():
    """Mirrors `test_embedding_worker.py::_reset_jobs` — the process-local
    job store + single-run guard must not leak across tests."""
    backfill._jobs.clear()
    backfill._running = False
    yield
    backfill._jobs.clear()
    backfill._running = False


def _configure_master(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER_KEY)


def _wire_router_db(monkeypatch, db_factory):
    """Router-level tests fire a real (fire-and-forget) background task via
    `enqueue_backfill` -> `create_background_task(run_backfill(...))`, which
    binds `async_session_factory` at import time inside
    `enrichment_backfill_worker` (a local import, NOT `app.db.database`'s
    module attribute) — must be patched on that specific module, same
    gotcha noted in `tests/test_download_security.py`."""
    monkeypatch.setattr(backfill, "async_session_factory", db_factory)
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)


# ─── Router: auth ────────────────────────────────────────────────────────


class TestRouterAuth:
    async def test_post_401_without_key(self, api_client, monkeypatch):
        _configure_master(monkeypatch)
        resp = await api_client.post("/api/admin/enrichment/omdb-backfill", json={})
        assert resp.status_code == 401

    async def test_post_401_with_wrong_key(self, api_client, monkeypatch):
        _configure_master(monkeypatch)
        resp = await api_client.post(
            "/api/admin/enrichment/omdb-backfill", json={},
            headers={"X-API-Key": "definitely-wrong"},
        )
        assert resp.status_code == 401

    async def test_get_401_without_key(self, api_client, monkeypatch):
        _configure_master(monkeypatch)
        resp = await api_client.get("/api/admin/enrichment/jobs/whatever")
        assert resp.status_code == 401

    async def test_post_401_with_a_valid_but_non_master_per_user_key(
        self, api_client, monkeypatch, db_factory,
    ):
        """A genuinely active per-user key (accepted by `verify_backend_secret`
        on /api/media, /api/accounts, etc.) must still be REJECTED here —
        same master-only design as the download JSON mirrors (this endpoint
        spends the OMDb budget + mutates the whole catalog)."""
        _configure_master(monkeypatch)
        _wire_router_db(monkeypatch, db_factory)
        monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
        async with db_factory() as s:
            _row, plaintext = await api_key_service.create_key(s, label="Per-user")

        resp_ok = await api_client.get("/api/accounts", headers={"X-API-Key": plaintext})
        assert resp_ok.status_code != 401

        resp = await api_client.post(
            "/api/admin/enrichment/omdb-backfill", json={},
            headers={"X-API-Key": plaintext},
        )
        assert resp.status_code == 401

    async def test_post_not_401_with_master_key(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_router_db(monkeypatch, db_factory)
        resp = await api_client.post(
            "/api/admin/enrichment/omdb-backfill", json={},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code != 401


# ─── Router: POST 202 + jobId, GET job status ───────────────────────────


class TestPostAndGet:
    async def test_post_returns_202_and_job_id(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_router_db(monkeypatch, db_factory)
        resp = await api_client.post(
            "/api/admin/enrichment/omdb-backfill", json={},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["jobId"].startswith("omdb_backfill_")
        assert body["status"] in ("queued", "running", "completed", "failed")
        assert "scanned" in body
        assert "omdbFetched" in body
        assert "imdbFilled" in body
        assert "displayRecomputed" in body
        assert "errors" in body
        assert "lastError" in body
        assert "startedAt" in body
        assert "finishedAt" in body

    async def test_post_accepts_request_body_overrides(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_master(monkeypatch)
        _wire_router_db(monkeypatch, db_factory)
        resp = await api_client.post(
            "/api/admin/enrichment/omdb-backfill",
            json={"mediaType": "movie", "recomputeDisplayRating": False, "limit": 5},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 202

    async def test_get_unknown_job_404(self, api_client, monkeypatch):
        _configure_master(monkeypatch)
        resp = await api_client.get(
            "/api/admin/enrichment/jobs/does-not-exist",
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 404

    async def test_get_job_status_returns_record(self, api_client, monkeypatch, db_factory):
        _configure_master(monkeypatch)
        _wire_router_db(monkeypatch, db_factory)
        post_resp = await api_client.post(
            "/api/admin/enrichment/omdb-backfill", json={},
            headers={"X-API-Key": MASTER_KEY},
        )
        job_id = post_resp.json()["jobId"]

        resp = await api_client.get(
            f"/api/admin/enrichment/jobs/{job_id}",
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["jobId"] == job_id


# ─── Router: single-run guard (409) ──────────────────────────────────────


class TestSingleRunGuard:
    async def test_second_post_conflicts_while_running(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_master(monkeypatch)
        _wire_router_db(monkeypatch, db_factory)
        # Drive the guard deterministically instead of racing the real
        # fire-and-forget background task (see module docstring).
        monkeypatch.setattr(backfill, "_running", True)

        resp = await api_client.post(
            "/api/admin/enrichment/omdb-backfill", json={},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"].lower()

    async def test_post_succeeds_once_guard_released(
        self, api_client, monkeypatch, db_factory,
    ):
        _configure_master(monkeypatch)
        _wire_router_db(monkeypatch, db_factory)
        assert backfill.is_running() is False
        resp = await api_client.post(
            "/api/admin/enrichment/omdb-backfill", json={},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert resp.status_code == 202


# ─── Worker: Phase A selection + fill-missing (no clobber) ──────────────


class TestPhaseASelection:
    async def test_only_eligible_rows_get_filled(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc(ratings={"tt0000001": (8.1, 900000), "tt0000002": (7.0, 5000)})
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add_all([
                # Eligible: imdb_id set, imdb_rating NULL.
                _media("rk1", imdb_id="tt0000001", imdb_rating=None, page_offset=0),
                _media("rk2", imdb_id="tt0000002", imdb_rating=None, page_offset=1),
                # Already complete: imdb_rating already set -> must NOT be clobbered.
                _media("rk3", imdb_id="tt0000003", imdb_rating=5.5, imdb_votes=10, page_offset=2),
                # No imdb_id at all -> excluded from the eligible set.
                _media("rk4", imdb_id=None, imdb_rating=None, page_offset=3),
                # Episode -> excluded by the movie/show type filter.
                _media(
                    "rk5", imdb_id="tt0000005", imdb_rating=None, type="episode",
                    library_section_id="xtream_series", page_offset=4,
                ),
            ])
            await s.commit()

        job_id = "job-selection"
        await backfill.run_backfill(job_id, db_factory, media_type="all", limit=None)

        job = backfill.get_job(job_id)
        assert job["status"] == "completed"
        assert job["errors"] == 0
        assert job["scanned"] == 2  # only rk1 + rk2 were eligible
        assert job["omdbFetched"] == 2
        assert job["imdbFilled"] == 2

        async with db_factory() as s:
            rows = {
                m.rating_key: m
                for m in (await s.execute(select(Media))).scalars().all()
            }

        assert rows["rk1"].imdb_rating == 8.1
        assert rows["rk1"].imdb_votes == 900000
        assert rows["rk2"].imdb_rating == 7.0
        # Already-complete row untouched (fill-missing, never clobbers) —
        # and never even fetched.
        assert rows["rk3"].imdb_rating == 5.5
        assert rows["rk3"].imdb_votes == 10
        assert "tt0000003" not in fake.calls
        # No imdb_id -> never fetched, still NULL.
        assert rows["rk4"].imdb_rating is None
        # Episode excluded entirely.
        assert "tt0000005" not in fake.calls
        assert rows["rk5"].imdb_rating is None

    async def test_media_type_filter_movie_only(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc(ratings={"tt0000006": (8.0, 1), "tt0000007": (9.0, 1)})
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add_all([
                _media("rk6", imdb_id="tt0000006", imdb_rating=None, type="movie", page_offset=0),
                _media(
                    "rk7", imdb_id="tt0000007", imdb_rating=None, type="show",
                    library_section_id="xtream_series", page_offset=0,
                ),
            ])
            await s.commit()

        await backfill.run_backfill("job-movie-only", db_factory, media_type="movie")

        async with db_factory() as s:
            rows = {
                m.rating_key: m
                for m in (await s.execute(select(Media))).scalars().all()
            }
        assert rows["rk6"].imdb_rating == 8.0
        assert rows["rk7"].imdb_rating is None  # show excluded by media_type="movie"
        assert "tt0000007" not in fake.calls

    async def test_limit_caps_rows_processed(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc(ratings={
            "tt0000101": (7.1, 1), "tt0000102": (7.2, 1), "tt0000103": (7.3, 1),
        })
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add_all([
                _media(f"rk10{i}", imdb_id=f"tt00001{i:02d}", imdb_rating=None, page_offset=i)
                for i in (1, 2, 3)
            ])
            await s.commit()

        job_id = "job-limit"
        await backfill.run_backfill(job_id, db_factory, limit=2)

        job = backfill.get_job(job_id)
        assert job["scanned"] == 2
        assert job["imdbFilled"] == 2


# ─── Worker: display_rating blend correctness ────────────────────────────


class TestBlendCorrectness:
    async def test_display_rating_matches_pure_blend_fn(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc(ratings={"tt0000010": (9.0, 100)})
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add(_media(
                "rk10x", imdb_id="tt0000010", imdb_rating=None,
                tmdb_rating=7.0, display_rating=0.0,
            ))
            await s.commit()

        await backfill.run_backfill("job-blend", db_factory, media_type="movie")

        async with db_factory() as s:
            row = (await s.execute(
                select(Media).where(Media.rating_key == "rk10x")
            )).scalar_one()

        expected = blend_rating(9.0, 7.0)
        assert expected == pytest.approx(8.0)
        assert row.display_rating == pytest.approx(expected)

    async def test_display_rating_single_side_when_no_tmdb_rating(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc(ratings={"tt0000011": (6.5, 20)})
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add(_media(
                "rk11x", imdb_id="tt0000011", imdb_rating=None,
                tmdb_rating=None, display_rating=0.0,
            ))
            await s.commit()

        await backfill.run_backfill("job-blend-2", db_factory)

        async with db_factory() as s:
            row = (await s.execute(
                select(Media).where(Media.rating_key == "rk11x")
            )).scalar_one()
        assert row.display_rating == pytest.approx(blend_rating(6.5, None))
        assert row.display_rating == pytest.approx(6.5)


# ─── Worker: Phase B recompute (SQL-only heal, idempotent) ──────────────


class TestPhaseBRecompute:
    async def test_heals_stale_display_rating_even_if_already_complete(
        self, monkeypatch, db_factory,
    ):
        """A row that's already 'complete' (imdb_rating set) but whose
        display_rating drifted (e.g. a sync content_hash-flip clobber, see
        design doc "Risks") is healed by Phase B even though Phase A finds
        nothing to fill for it."""
        fake = _FakeOmdbSvc()  # nothing eligible for Phase A
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add(_media(
                "rk20", imdb_id="tt0000020", imdb_rating=8.0, imdb_votes=500,
                tmdb_rating=6.0, display_rating=0.0,  # stale/clobbered
            ))
            await s.commit()

        job_id = "job-recompute"
        await backfill.run_backfill(job_id, db_factory, media_type="all")

        job = backfill.get_job(job_id)
        assert job["status"] == "completed"
        assert job["scanned"] == 0       # nothing eligible for Phase A
        assert fake.calls == []          # no OMDb call at all
        assert job["displayRecomputed"] >= 1

        async with db_factory() as s:
            row = (await s.execute(
                select(Media).where(Media.rating_key == "rk20")
            )).scalar_one()
        assert row.display_rating == pytest.approx(blend_rating(8.0, 6.0))

    async def test_recompute_is_idempotent(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc()
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add(_media(
                "rk21", imdb_id="tt0000021", imdb_rating=8.0, imdb_votes=500,
                tmdb_rating=6.0, display_rating=0.0,
            ))
            await s.commit()

        await backfill.run_backfill("job-a", db_factory, media_type="all")
        async with db_factory() as s:
            first = (await s.execute(
                select(Media.display_rating).where(Media.rating_key == "rk21")
            )).scalar_one()

        # Run again — same inputs, same output (SQL-only recompute is a
        # pure function of the persisted columns).
        await backfill.run_backfill("job-b", db_factory, media_type="all")
        async with db_factory() as s:
            second = (await s.execute(
                select(Media.display_rating).where(Media.rating_key == "rk21")
            )).scalar_one()

        assert first == pytest.approx(blend_rating(8.0, 6.0))
        assert second == pytest.approx(first)

    async def test_recompute_disabled_leaves_stale_value(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc()
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add(_media(
                "rk22", imdb_id="tt0000022", imdb_rating=8.0, tmdb_rating=6.0,
                display_rating=0.0,
            ))
            await s.commit()

        await backfill.run_backfill("job-c", db_factory, recompute_display_rating=False)

        async with db_factory() as s:
            row = (await s.execute(
                select(Media).where(Media.rating_key == "rk22")
            )).scalar_one()
        assert row.display_rating == 0.0  # untouched — Phase B skipped


# ─── Worker: OMDb unconfigured / budget exhausted -> fail-open ──────────


class TestBudgetFailOpen:
    async def test_unconfigured_omdb_completes_without_crash(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc(configured=False)
        monkeypatch.setattr(backfill, "omdb_service", fake)

        async with db_factory() as s:
            s.add(_media("rk30", imdb_id="tt0000030", imdb_rating=None))
            await s.commit()

        job_id = "job-unconfigured"
        await backfill.run_backfill(job_id, db_factory)

        job = backfill.get_job(job_id)
        assert job["status"] == "completed"
        assert job["imdbFilled"] == 0
        assert job["omdbFetched"] == 0
        assert job["errors"] == 0
        assert fake.calls == []

        async with db_factory() as s:
            row = (await s.execute(
                select(Media).where(Media.rating_key == "rk30")
            )).scalar_one()
        assert row.imdb_rating is None

    async def test_budget_exhausted_completes_without_crash(self, monkeypatch, db_factory):
        fake = _FakeOmdbSvc(ratings={"tt0000031": (8.0, 100)})
        monkeypatch.setattr(backfill, "omdb_service", fake)
        monkeypatch.setattr(settings, "OMDB_DAILY_LIMIT", 0)

        async with db_factory() as s:
            s.add(_media("rk31", imdb_id="tt0000031", imdb_rating=None))
            await s.commit()

        job_id = "job-budget"
        await backfill.run_backfill(job_id, db_factory)

        job = backfill.get_job(job_id)
        assert job["status"] == "completed"
        assert job["imdbFilled"] == 0
        assert fake.calls == []

        async with db_factory() as s:
            row = (await s.execute(
                select(Media).where(Media.rating_key == "rk31")
            )).scalar_one()
        assert row.imdb_rating is None
