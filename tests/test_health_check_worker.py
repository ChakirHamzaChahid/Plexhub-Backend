"""Guard tests for two P2 clean-room findings in `health_check_worker`:

- CR-F08: the per-account circuit breaker used to evaluate the failure rate a
  single time, at exactly the 50th check — accounts with fewer than 50
  streams could never trip it (even at 100% failure), and an outage starting
  *after* the 50th check was invisible to it. It must now evaluate on a
  rolling basis once a (lower) minimum sample is reached.
- CR-P06: the cron sampler used `ORDER BY random()` (full scan + filesort of
  every candidate row). It must now sample via a random rowid anchor instead.

Also covers `_check_one`'s HTTP classification (CR-T05), mocked via respx —
the existing `test_health_check_concurrency.py` only exercises the
concurrency clamp with `_check_one` stubbed out, never the real logic.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.database import Media, XtreamAccount
from app.utils.time import now_ms
from app.workers import health_check_worker as hc


# ─── helpers ──────────────────────────────────────────────────────────────


def _account(acc_id: str, max_conn: int = 20) -> XtreamAccount:
    return XtreamAccount(
        id=acc_id,
        label=acc_id,
        base_url=f"http://{acc_id}.test",
        username="u",
        password="p",
        max_connections=max_conn,
    )


def _streams(server_id: str, n: int, *, last_check=None) -> list[Media]:
    return [
        Media(
            rating_key=f"vod_{server_id}_{i}.mp4",
            server_id=server_id,
            library_section_id="1",
            title=f"t{i}",
            type="movie",
            page_offset=i,  # keeps the pagination unique index happy
            last_stream_check=last_check,
            is_in_allowed_categories=True,
        )
        for i in range(n)
    ]


async def _rows_for(db_factory, server_id: str) -> list[Media]:
    async with db_factory() as db:
        result = await db.execute(select(Media).where(Media.server_id == server_id))
        return list(result.scalars().all())


# ─── CR-P06: rowid-anchored sampling replaces ORDER BY random() ──────────


@pytest.mark.asyncio
async def test_sample_stream_candidates_covers_all_eligible_rows(db_factory):
    """With batch_size >= eligible count every eligible row comes back once —
    no row lost, none duplicated — regardless of the rowid-anchor strategy."""
    async with db_factory() as db:
        for m in _streams("xtream_aaaaaaaa", 8):
            db.add(m)
        await db.commit()

    async with db_factory() as db:
        items = await hc._sample_stream_candidates(db, batch_size=100, cutoff=now_ms())

    assert {i.rating_key for i in items} == {
        f"vod_xtream_aaaaaaaa_{i}.mp4" for i in range(8)
    }


@pytest.mark.asyncio
async def test_sample_stream_candidates_respects_batch_size(db_factory):
    async with db_factory() as db:
        for m in _streams("xtream_aaaaaaaa", 20):
            db.add(m)
        await db.commit()

    async with db_factory() as db:
        items = await hc._sample_stream_candidates(db, batch_size=5, cutoff=now_ms())

    assert len(items) == 5


@pytest.mark.asyncio
async def test_sample_stream_candidates_excludes_fresh_checks(db_factory):
    cutoff = now_ms() - 3_600_000  # 1h ago
    async with db_factory() as db:
        db.add(Media(  # checked recently -> excluded
            rating_key="vod_fresh.mp4", server_id="xtream_aaaaaaaa",
            library_section_id="1", title="fresh", type="movie", page_offset=0,
            last_stream_check=now_ms(), is_in_allowed_categories=True,
        ))
        db.add(Media(  # never checked -> included
            rating_key="vod_new.mp4", server_id="xtream_aaaaaaaa",
            library_section_id="1", title="new", type="movie", page_offset=1,
            last_stream_check=None, is_in_allowed_categories=True,
        ))
        db.add(Media(  # stale -> included
            rating_key="vod_stale.mp4", server_id="xtream_aaaaaaaa",
            library_section_id="1", title="stale", type="movie", page_offset=2,
            last_stream_check=cutoff - 1000, is_in_allowed_categories=True,
        ))
        await db.commit()

    async with db_factory() as db:
        items = await hc._sample_stream_candidates(db, batch_size=100, cutoff=cutoff)

    assert {i.rating_key for i in items} == {"vod_new.mp4", "vod_stale.mp4"}


@pytest.mark.asyncio
async def test_sample_stream_candidates_wraps_around_when_anchor_is_late(
    db_factory, monkeypatch
):
    """Force the random anchor to the last rowid so the forward scan alone
    can't fill batch_size — the wrap-around query must supply the rest."""
    async with db_factory() as db:
        for m in _streams("xtream_aaaaaaaa", 5):
            db.add(m)
        await db.commit()

    monkeypatch.setattr(hc.random, "randint", lambda a, b: b)  # anchor = max(rowid)

    async with db_factory() as db:
        items = await hc._sample_stream_candidates(db, batch_size=3, cutoff=now_ms())

    # Forward scan (rowid >= anchor) yields at most the last row; wrap-around
    # (rowid < anchor) must fill the remainder up to batch_size.
    assert len(items) == 3


@pytest.mark.asyncio
async def test_sample_stream_candidates_empty_table_returns_empty(db_factory):
    async with db_factory() as db:
        items = await hc._sample_stream_candidates(db, batch_size=10, cutoff=now_ms())
    assert items == []


@pytest.mark.asyncio
async def test_run_health_check_batch_delegates_to_sampling_helper(monkeypatch, db_factory):
    """Regression guard: the cron entrypoint must go through
    `_sample_stream_candidates` (rowid anchor), not a fresh `ORDER BY
    random()` full-scan-and-sort inlined again later."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa", 20))
        for m in _streams("xtream_aaaaaaaa", 3):
            db.add(m)
        await db.commit()

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)
    monkeypatch.setattr(hc, "async_session_factory", db_factory)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    async def _fake_check_one(client, item, account, semaphore):
        return item, False, "head_ct_video"

    monkeypatch.setattr(hc, "_check_one", _fake_check_one)

    calls = {"n": 0}
    orig = hc._sample_stream_candidates

    async def _spy(db, batch_size, cutoff):
        calls["n"] += 1
        return await orig(db, batch_size, cutoff)

    monkeypatch.setattr(hc, "_sample_stream_candidates", _spy)

    await hc._run_health_check_batch()

    assert calls["n"] == 1


# ─── CR-F08: rolling circuit breaker ──────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_small_account_below_old_fixed_50(
    monkeypatch, db_factory
):
    """Under the old logic (`account_checked == 50`), an account with fewer
    than 50 streams could NEVER trip the breaker, no matter the failure rate.
    With the rolling check it must trip once the minimum sample is reached."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa", 20))
        for m in _streams("xtream_aaaaaaaa", 30):
            db.add(m)
        await db.commit()

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)
    monkeypatch.setattr(hc, "async_session_factory", db_factory)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    async def _always_fail(client, item, account, semaphore):
        return item, True, "timeout"  # transient reason, not a definitive one

    monkeypatch.setattr(hc, "_check_one", _always_fail)

    await hc._run_pipeline_validation_impl()

    rows = await _rows_for(db_factory, "xtream_aaaaaaaa")
    # Tripped: the account's uncommitted updates were rolled back — nothing
    # was ever marked checked or broken.
    assert all(r.last_stream_check is None for r in rows)
    assert all(r.is_broken is False for r in rows)


@pytest.mark.asyncio
async def test_circuit_breaker_does_not_trip_below_minimum_sample(
    monkeypatch, db_factory
):
    """A tiny account (below the minimum sample) must not be cut off by the
    breaker even at 100% failure — each stream is still checked/committed
    individually (per-item broken-marking is governed by
    STREAM_BROKEN_THRESHOLD / definitive-failure rules, not the breaker)."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa", 20))
        for m in _streams("xtream_aaaaaaaa", 5):
            db.add(m)
        await db.commit()

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)
    monkeypatch.setattr(hc, "async_session_factory", db_factory)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    async def _always_fail(client, item, account, semaphore):
        return item, True, "timeout"

    monkeypatch.setattr(hc, "_check_one", _always_fail)

    await hc._run_pipeline_validation_impl()

    rows = await _rows_for(db_factory, "xtream_aaaaaaaa")
    assert len(rows) == 5
    assert all(r.last_stream_check is not None for r in rows)


@pytest.mark.asyncio
async def test_circuit_breaker_does_not_trip_on_low_failure_rate(monkeypatch, db_factory):
    """A ~50% failure rate (well under the 90% threshold) must never trip
    the breaker, however many checks accumulate."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa", 20))
        for m in _streams("xtream_aaaaaaaa", 20):
            db.add(m)
        await db.commit()

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)
    monkeypatch.setattr(hc, "async_session_factory", db_factory)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    counter = {"n": 0}

    async def _mixed(client, item, account, semaphore):
        counter["n"] += 1
        is_broken = counter["n"] % 2 == 0
        return item, is_broken, ("timeout" if is_broken else "head_ct_video")

    monkeypatch.setattr(hc, "_check_one", _mixed)

    await hc._run_pipeline_validation_impl()

    rows = await _rows_for(db_factory, "xtream_aaaaaaaa")
    assert len(rows) == 20
    assert all(r.last_stream_check is not None for r in rows)


@pytest.mark.asyncio
async def test_circuit_breaker_is_scoped_per_account(monkeypatch, db_factory):
    """One account tripping the breaker must not affect another account in
    the same run."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa", 20))
        db.add(_account("bbbbbbbb", 20))
        for m in _streams("xtream_aaaaaaaa", 30):
            db.add(m)
        for m in _streams("xtream_bbbbbbbb", 30):
            db.add(m)
        await db.commit()

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)
    monkeypatch.setattr(hc, "async_session_factory", db_factory)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    async def _by_account(client, item, account, semaphore):
        if item.server_id.endswith("aaaaaaaa"):
            return item, True, "timeout"
        return item, False, "head_ct_video"

    monkeypatch.setattr(hc, "_check_one", _by_account)

    await hc._run_pipeline_validation_impl()

    a_rows = await _rows_for(db_factory, "xtream_aaaaaaaa")
    b_rows = await _rows_for(db_factory, "xtream_bbbbbbbb")

    assert all(r.last_stream_check is None for r in a_rows)  # tripped, rolled back
    assert len(b_rows) == 30
    assert all(r.last_stream_check is not None for r in b_rows)  # unaffected


# ─── CR-T05: `_check_one` HTTP classification (respx-mocked) ─────────────


def _fake_account() -> SimpleNamespace:
    return SimpleNamespace(base_url="http://acct.test", port=80, username="u", password="p")


def _movie_item(stream_id: str = "1") -> SimpleNamespace:
    return SimpleNamespace(rating_key=f"vod_{stream_id}.mp4")


STREAM_URL = "http://acct.test/movie/u/p/1.mp4"


@pytest.mark.asyncio
async def test_check_one_head_video_content_type_is_ok(xtream_mock):
    xtream_mock.head(STREAM_URL).respond(
        200, headers={"content-type": "video/mp4", "content-length": "123456"}
    )

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is False
    assert reason == "head_ct_video"


@pytest.mark.asyncio
async def test_check_one_head_404_is_definitive_broken(xtream_mock):
    xtream_mock.head(STREAM_URL).respond(404)

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is True
    assert reason == "head_404"
    assert hc._is_definitive_failure(reason) is True


@pytest.mark.asyncio
async def test_check_one_head_403_is_definitive_broken(xtream_mock):
    xtream_mock.head(STREAM_URL).respond(403)

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is True
    assert reason == "head_403"
    assert hc._is_definitive_failure(reason) is True


@pytest.mark.asyncio
async def test_check_one_head_html_error_page_is_definitive_broken(xtream_mock):
    xtream_mock.head(STREAM_URL).respond(
        200, headers={"content-type": "text/html", "content-length": "512"}
    )

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is True
    assert reason == "head_ct_error:text/html"
    assert hc._is_definitive_failure(reason) is True


@pytest.mark.asyncio
async def test_check_one_ambiguous_head_falls_through_to_range_get_video(xtream_mock):
    # HEAD with no Content-Type at all is ambiguous -> Range GET decides.
    xtream_mock.head(STREAM_URL).respond(200)
    xtream_mock.get(STREAM_URL).respond(200, headers={"content-type": "video/mp2t"})

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is False
    assert reason == "get_ct_video"


@pytest.mark.asyncio
async def test_check_one_range_get_magic_bytes_ok(xtream_mock):
    xtream_mock.head(STREAM_URL).respond(200)
    xtream_mock.get(STREAM_URL).respond(200, content=b"\x47" + b"\x00" * 187)  # MPEG-TS sync

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is False
    assert reason == "get_magic_ok"


@pytest.mark.asyncio
async def test_check_one_range_get_empty_body_is_definitive_broken(xtream_mock):
    xtream_mock.head(STREAM_URL).respond(200)
    xtream_mock.get(STREAM_URL).respond(200, content=b"")

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is True
    assert reason == "get_empty"
    assert hc._is_definitive_failure(reason) is True


@pytest.mark.asyncio
async def test_check_one_range_get_garbage_bytes_is_definitive_broken(xtream_mock):
    xtream_mock.head(STREAM_URL).respond(200)
    xtream_mock.get(STREAM_URL).respond(200, content=b"not a video, definitely not")

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is True
    assert reason.startswith("get_magic_fail:")
    assert hc._is_definitive_failure(reason) is True


@pytest.mark.asyncio
async def test_check_one_timeout_is_transient_not_definitive(xtream_mock):
    xtream_mock.head(STREAM_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is True
    assert reason == "timeout"
    assert hc._is_definitive_failure(reason) is False


@pytest.mark.asyncio
async def test_check_one_connect_error_is_transient_not_definitive(xtream_mock):
    xtream_mock.head(STREAM_URL).mock(side_effect=httpx.ConnectError("refused"))

    async with httpx.AsyncClient() as client:
        _, is_broken, reason = await hc._check_one(
            client, _movie_item(), _fake_account(), asyncio.Semaphore(1)
        )

    assert is_broken is True
    assert reason == "connect_error"
    assert hc._is_definitive_failure(reason) is False
