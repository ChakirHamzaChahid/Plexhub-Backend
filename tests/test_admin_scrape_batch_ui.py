"""ADR 0005 D8/D10 (Wave W5) — the batch + "À vérifier" admin routes:
`/admin/scrape-batch{,/status,/{job}/cancel}` and
`/admin/review{,/{sid}/{rk}/apply,/dismiss}`.

HTTP-level against the real Jinja fragments: a template that stops rendering
the Apply form, or that keeps polling a finished job forever, is a real
regression the service-level tests cannot see.
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

from app.config import settings
from app.db import database as db_module
from app.models.database import Media
from app.services import manual_scrape_service as mss
from app.services import unified_group_service
from app.workers import manual_scrape_batch_worker as worker
from tests.test_manual_scrape_apply import FakeOMDb, FakeTMDB, _details

pytestmark = pytest.mark.asyncio

ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-pass-scrape-batch"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)

_page_offset = itertools.count()


@pytest.fixture(autouse=True)
def _admin_creds(monkeypatch, db_factory):
    from app.api import admin as admin_module

    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(admin_module, "async_session_factory", db_factory)
    yield
    worker._jobs.clear()
    worker._running = False
    unified_group_service._pending_rebuild_tasks.clear()
    unified_group_service._pending_session_factories.clear()
    unified_group_service._running_rebuild_tasks.clear()


async def _seed(db_factory, rating_key="vod_1.mp4", title="The Matrix"):
    async with db_factory() as s:
        s.add(Media(
            rating_key=rating_key, server_id="xtream_a", library_section_id="1",
            title=title, type="movie", year=1999, page_offset=next(_page_offset),
        ))
        await s.commit()


async def _queue_review(db_factory, *, rating_key="vod_1.mp4", candidates=None):
    await mss.upsert_review(
        server_id="xtream_a", rating_key=rating_key, media_type="movie",
        title="The Matrix", year=1999, reason="no_identical_poster",
        candidates=candidates or [], session_factory=db_factory,
    )


def _candidate(tmdb_id=603, imdb_id="tt0133093") -> mss.Candidate:
    return mss.Candidate(
        provider="tmdb", tmdb_id=tmdb_id, imdb_id=imdb_id, media_type="movie",
        title="The Matrix", original_title=None, year=1999,
        overview="A computer hacker.", poster_url="http://img/a.jpg",
        title_score=1.0, text_confidence=0.95, poster=None,
        combined_score=0.95, text_safe=True, recommended=True,
    )


# ─── review list ───────────────────────────────────────────────────────────


async def test_review_list_renders_stored_candidates_without_network(
    api_client, db_factory, monkeypatch,
):
    await _seed(db_factory)
    await _queue_review(db_factory, candidates=[_candidate()])

    class Exploding:
        def __getattr__(self, name):
            raise AssertionError("the review list must make ZERO network calls")

    monkeypatch.setattr(mss, "tmdb_service", Exploding())
    monkeypatch.setattr(mss, "omdb_service", Exploding())

    resp = await api_client.get("/admin/review", auth=ADMIN_AUTH)
    assert resp.status_code == 200, resp.text
    assert "The Matrix" in resp.text
    assert "no_identical_poster" in resp.text
    # The three actions of ADR 0005 D10.
    assert "Appliquer" in resp.text
    assert "Autres" in resp.text
    assert "Ignorer" in resp.text
    assert "/admin/review/xtream_a/vod_1.mp4/apply" in resp.text


async def test_review_list_is_empty_when_nothing_is_queued(api_client, db_factory):
    resp = await api_client.get("/admin/review", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert "Rien à vérifier" in resp.text


async def test_review_apply_uses_the_stored_candidate_and_clears_the_row(
    api_client, db_factory, monkeypatch,
):
    await _seed(db_factory)
    await _queue_review(db_factory, candidates=[_candidate()])
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB(details_by_id={603: _details()}))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    resp = await api_client.post(
        "/admin/review/xtream_a/vod_1.mp4/apply",
        data={"candidate_index": "0"}, auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.text.strip() == ""  # the entry disappears from the list
    assert resp.headers.get("HX-Trigger") == "refresh-stats, refresh-review"

    row = await mss.load_row("xtream_a", "vod_1.mp4", session_factory=db_factory)
    assert (row.tmdb_id, row.imdb_id) == ("603", "tt0133093")
    review = await mss.load_review(
        "xtream_a", "vod_1.mp4", session_factory=db_factory,
    )
    assert review.status == "resolved"


async def test_review_apply_rejects_an_out_of_range_index(api_client, db_factory):
    await _seed(db_factory)
    await _queue_review(db_factory, candidates=[_candidate()])
    resp = await api_client.post(
        "/admin/review/xtream_a/vod_1.mp4/apply",
        data={"candidate_index": "7"}, auth=ADMIN_AUTH,
    )
    assert resp.status_code == 422


async def test_review_apply_on_unknown_review_is_404(api_client):
    resp = await api_client.post(
        "/admin/review/xtream_a/nope.mp4/apply",
        data={"candidate_index": "0"}, auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404


async def test_review_dismiss_clears_the_row(api_client, db_factory):
    await _seed(db_factory)
    await _queue_review(db_factory)

    resp = await api_client.post(
        "/admin/review/xtream_a/vod_1.mp4/dismiss", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "refresh-stats, refresh-review"
    review = await mss.load_review(
        "xtream_a", "vod_1.mp4", session_factory=db_factory,
    )
    assert review.status == "dismissed"


async def test_review_dismiss_unknown_is_404(api_client):
    resp = await api_client.post(
        "/admin/review/xtream_a/nope.mp4/dismiss", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404


# ─── batch job routes ──────────────────────────────────────────────────────


async def _drain(job_id: str) -> dict:
    for _ in range(100):
        job = worker.get(job_id)
        if job and job["status"] in ("completed", "failed", "canceled"):
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("batch job never finished")


async def test_start_batch_returns_202_and_a_polling_fragment(
    api_client, db_factory, monkeypatch,
):
    monkeypatch.setattr(mss, "tmdb_service", FakeTMDB())
    monkeypatch.setattr(mss, "omdb_service", FakeOMDb())

    resp = await api_client.post(
        "/admin/scrape-batch",
        data={"type": "movie", "dry_run": "1"}, auth=ADMIN_AUTH,
    )
    assert resp.status_code == 202, resp.text
    assert "scrape_batch_" in resp.text
    job_id = worker.get_latest()["jobId"]
    await _drain(job_id)


async def test_second_start_while_running_returns_409(api_client, db_factory):
    """The guard is synchronous, so a second POST can't slip in — simulate the
    in-flight state directly rather than racing a real run."""
    worker._register(
        "scrape_batch_busy",
        worker._new_job("scrape_batch_busy", media_type="all", dry_run=False),
    )
    worker._jobs["scrape_batch_busy"]["status"] = "running"
    worker._running = True

    resp = await api_client.post(
        "/admin/scrape-batch", data={"type": "movie"}, auth=ADMIN_AUTH,
    )
    assert resp.status_code == 409
    assert "déjà en cours" in resp.text


async def test_status_fragment_polls_only_while_running(api_client):
    worker._register(
        "scrape_batch_x", worker._new_job("scrape_batch_x", media_type="all", dry_run=False),
    )
    worker._jobs["scrape_batch_x"]["status"] = "running"

    running = await api_client.get(
        "/admin/scrape-batch/status?job_id=scrape_batch_x", auth=ADMIN_AUTH,
    )
    assert running.status_code == 200
    assert "every 2s" in running.text

    worker._jobs["scrape_batch_x"]["status"] = "completed"
    done = await api_client.get(
        "/admin/scrape-batch/status?job_id=scrape_batch_x", auth=ADMIN_AUTH,
    )
    assert done.status_code == 200
    assert "every 2s" not in done.text


async def test_status_without_job_id_shows_the_latest(api_client):
    resp = await api_client.get("/admin/scrape-batch/status", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert "Aucun lot" in resp.text


async def test_cancel_marks_the_job_and_unknown_job_is_404(api_client):
    worker._register(
        "scrape_batch_c", worker._new_job("scrape_batch_c", media_type="all", dry_run=False),
    )
    worker._jobs["scrape_batch_c"]["status"] = "running"

    resp = await api_client.post(
        "/admin/scrape-batch/scrape_batch_c/cancel", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert worker._jobs["scrape_batch_c"]["cancelRequested"] is True

    missing = await api_client.post(
        "/admin/scrape-batch/nope/cancel", auth=ADMIN_AUTH,
    )
    assert missing.status_code == 404


# ─── stats / filter wiring (W4 left these stubbed at 0) ────────────────────


async def test_stats_fragment_shows_the_review_counter(api_client, db_factory):
    await _seed(db_factory)
    await _queue_review(db_factory)

    resp = await api_client.get("/admin/stats", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert "À vérifier" in resp.text


async def test_catalogue_review_filter_lists_only_flagged_items(
    api_client, db_factory,
):
    await _seed(db_factory, rating_key="vod_flagged.mp4", title="Flagged Film")
    await _seed(db_factory, rating_key="vod_clean.mp4", title="Clean Film")
    await _queue_review(db_factory, rating_key="vod_flagged.mp4")

    resp = await api_client.get(
        "/admin/catalogue?type=movie&ids=review", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert "Flagged Film" in resp.text
    assert "Clean Film" not in resp.text
