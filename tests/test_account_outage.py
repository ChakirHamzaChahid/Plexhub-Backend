"""Provider-outage tracking, masking and automatic recovery (M025).

Context: before this feature the backend had no memory of an outage from one
validation run to the next — the circuit breaker tripped, skipped the account
and forgot. Worse, the cron entrypoint had no breaker at all, so a provider
answering 404 (a "definitive" failure) had its sampled streams flipped to
`is_broken=True` immediately, bypassing STREAM_BROKEN_THRESHOLD entirely.

The invariant these tests protect, in one line: an outage hides media from the
app's reads and NOTHING else — `is_active` and `is_broken` stay untouched, so
the generated Plex/Jellyfin library is never purged.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.database import Media, XtreamAccount
from app.services import account_outage_service as outage
from app.services.media_service import media_service
from app.utils.time import now_ms
from app.workers import health_check_worker as hc


# ─── helpers ──────────────────────────────────────────────────────────────


def _account(acc_id: str, max_conn: int = 20) -> XtreamAccount:
    return XtreamAccount(
        id=acc_id, label=acc_id, base_url=f"http://{acc_id}.test",
        username="u", password="p", max_connections=max_conn,
    )


def _streams(server_id: str, n: int, *, offset: int = 0) -> list[Media]:
    return [
        Media(
            rating_key=f"vod_{server_id}_{i}.mp4",
            server_id=server_id,
            library_section_id="1",
            title=f"Film {i}",
            type="movie",
            page_offset=offset + i,
            is_in_allowed_categories=True,
        )
        for i in range(n)
    ]


async def _strikes(db_factory, account_id: str) -> int:
    async with db_factory() as db:
        return (await db.execute(
            select(XtreamAccount.outage_strikes).where(XtreamAccount.id == account_id)
        )).scalar()


@pytest.fixture
def outage_db(monkeypatch, db_factory):
    """Point both the service's writers and the worker at the test DB."""
    monkeypatch.setattr(outage, "worker_session_factory", db_factory)
    monkeypatch.setattr(hc, "worker_session_factory", db_factory)
    return db_factory


# ─── service: strike bookkeeping ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_trip_increments_and_pins_outage_since(outage_db, db_factory):
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        await db.commit()

    before = now_ms()
    assert await outage.record_trip("aaaaaaaa") == 1
    async with db_factory() as db:
        first_since = (await db.execute(
            select(XtreamAccount.outage_since).where(XtreamAccount.id == "aaaaaaaa")
        )).scalar()
    assert first_since >= before

    assert await outage.record_trip("aaaaaaaa") == 2
    async with db_factory() as db:
        acc = (await db.execute(
            select(XtreamAccount).where(XtreamAccount.id == "aaaaaaaa")
        )).scalar_one()
    assert acc.outage_strikes == 2
    # "down since", not "last seen down": the second trip must not move it.
    assert acc.outage_since == first_since
    # The outage must never leak into the global kill switch.
    assert acc.is_active is True


@pytest.mark.asyncio
async def test_record_clear_resets_streak(outage_db, db_factory):
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        await db.commit()

    await outage.record_trip("aaaaaaaa")
    await outage.record_trip("aaaaaaaa")
    await outage.record_clear("aaaaaaaa")

    async with db_factory() as db:
        acc = (await db.execute(
            select(XtreamAccount).where(XtreamAccount.id == "aaaaaaaa")
        )).scalar_one()
    assert acc.outage_strikes == 0
    assert acc.outage_since is None


@pytest.mark.asyncio
async def test_record_clear_is_a_noop_when_nothing_to_clear(outage_db, db_factory):
    """Every healthy account gets a `record_clear` on every run — that must
    not turn into a write per account per run."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        await db.commit()

    await outage.record_clear("aaaaaaaa")

    assert await _strikes(db_factory, "aaaaaaaa") == 0


@pytest.mark.asyncio
async def test_masked_server_ids_respects_threshold_and_kill_switch(
    monkeypatch, outage_db, db_factory
):
    monkeypatch.setattr(settings, "ACCOUNT_OUTAGE_STRIKES", 3)
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        await db.commit()

    for expected in (frozenset(), frozenset(), frozenset({"xtream_aaaaaaaa"})):
        await outage.record_trip("aaaaaaaa")
        async with db_factory() as db:
            assert await outage.masked_server_ids(db) == expected

    monkeypatch.setattr(settings, "ACCOUNT_OUTAGE_MASK_ENABLED", False)
    outage.invalidate_mask_cache()
    async with db_factory() as db:
        assert await outage.masked_server_ids(db) == frozenset()


# ─── cron entrypoint: breaker + strikes ───────────────────────────────────


async def _run_cron(monkeypatch, *, broken_for):
    """Run the cron batch with `_check_one` stubbed by a per-item verdict."""
    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    async def _fake_check_one(client, item, account, semaphore):
        if broken_for(item):
            # 404 is a *definitive* failure: pre-M025 this alone flipped the
            # row to is_broken=True on the very first check.
            return item, True, "head_404", None
        return item, False, "head_ct_video", None

    monkeypatch.setattr(hc, "_check_one", _fake_check_one)
    await hc._run_health_check_batch()


@pytest.mark.asyncio
async def test_cron_breaker_marks_nothing_broken_and_records_a_strike(
    monkeypatch, outage_db, db_factory
):
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", 20):
            db.add(m)
        await db.commit()

    await _run_cron(monkeypatch, broken_for=lambda item: True)

    async with db_factory() as db:
        rows = (await db.execute(
            select(Media).where(Media.server_id == "xtream_aaaaaaaa")
        )).scalars().all()
    # Not one row marked broken — and no check timestamp advanced, so these
    # items stay candidates for the next run instead of being pushed out of
    # the recheck window by an outage they are not responsible for.
    assert not any(r.is_broken for r in rows)
    assert all(r.last_stream_check is None for r in rows)
    assert await _strikes(db_factory, "aaaaaaaa") == 1


@pytest.mark.asyncio
async def test_cron_below_breaker_threshold_still_marks_broken(
    monkeypatch, outage_db, db_factory
):
    """Ordinary attrition must behave exactly as before: half the streams
    failing is not an outage."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", 20):
            db.add(m)
        await db.commit()

    await _run_cron(
        monkeypatch, broken_for=lambda item: item.page_offset % 2 == 0,
    )

    async with db_factory() as db:
        rows = (await db.execute(
            select(Media).where(Media.server_id == "xtream_aaaaaaaa")
        )).scalars().all()
    assert sum(1 for r in rows if r.is_broken) == 10
    assert await _strikes(db_factory, "aaaaaaaa") == 0


@pytest.mark.asyncio
async def test_cron_breaker_needs_the_minimum_sample(
    monkeypatch, outage_db, db_factory
):
    """A tiny account failing 100% is below the sample floor — it is treated
    as ordinary breakage, not as an outage."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", hc.CIRCUIT_BREAKER_MIN_SAMPLE - 1):
            db.add(m)
        await db.commit()

    await _run_cron(monkeypatch, broken_for=lambda item: True)

    assert await _strikes(db_factory, "aaaaaaaa") == 0


@pytest.mark.asyncio
async def test_cron_isolates_accounts(monkeypatch, outage_db, db_factory):
    """One provider down must not shield (or damage) another's streams."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        db.add(_account("bbbbbbbb"))
        for m in _streams("xtream_aaaaaaaa", 20):
            db.add(m)
        for m in _streams("xtream_bbbbbbbb", 20, offset=100):
            db.add(m)
        await db.commit()

    await _run_cron(
        monkeypatch,
        broken_for=lambda item: item.server_id == "xtream_aaaaaaaa",
    )

    assert await _strikes(db_factory, "aaaaaaaa") == 1
    assert await _strikes(db_factory, "bbbbbbbb") == 0
    async with db_factory() as db:
        rows = (await db.execute(select(Media))).scalars().all()
    assert not any(r.is_broken for r in rows)


@pytest.mark.asyncio
async def test_cron_healthy_run_clears_an_existing_streak(
    monkeypatch, outage_db, db_factory
):
    """Automatic recovery: no operator action, no re-activation endpoint."""
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", 20):
            db.add(m)
        await db.commit()
    await outage.record_trip("aaaaaaaa")
    await outage.record_trip("aaaaaaaa")

    await _run_cron(monkeypatch, broken_for=lambda item: False)

    assert await _strikes(db_factory, "aaaaaaaa") == 0


# ─── pipeline entrypoint ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_breaker_records_a_strike_despite_the_rollback(
    monkeypatch, outage_db, db_factory
):
    """The pipeline records the trip immediately AFTER `db.rollback()` +
    `db.expunge_all()` on its long-lived shared session. Writing through that
    session would commit zero rows without raising (ADR 0004 Decision 4) — the
    strike surviving here is what proves the write goes through a fresh one.
    """
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", 30):
            db.add(m)
        await db.commit()

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    async def _fake_check_one(client, item, account, semaphore):
        return item, True, "head_404", None

    monkeypatch.setattr(hc, "_check_one", _fake_check_one)

    await hc._run_pipeline_validation_impl()

    assert await _strikes(db_factory, "aaaaaaaa") == 1
    async with db_factory() as db:
        rows = (await db.execute(select(Media))).scalars().all()
    assert not any(r.is_broken for r in rows)


@pytest.mark.asyncio
async def test_pipeline_healthy_pass_clears_the_streak(
    monkeypatch, outage_db, db_factory
):
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", 30):
            db.add(m)
        await db.commit()
    await outage.record_trip("aaaaaaaa")

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    async def _fake_check_one(client, item, account, semaphore):
        return item, False, "head_ct_video", None

    monkeypatch.setattr(hc, "_check_one", _fake_check_one)

    await hc._run_pipeline_validation_impl()

    assert await _strikes(db_factory, "aaaaaaaa") == 0


# ─── read-path masking ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_masking_hides_media_only_at_the_threshold(
    monkeypatch, outage_db, db_factory
):
    monkeypatch.setattr(settings, "ACCOUNT_OUTAGE_STRIKES", 3)
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", 5):
            db.add(m)
        await db.commit()

    async def _visible() -> int:
        outage.invalidate_mask_cache()
        async with db_factory() as db:
            _items, total = await media_service.get_media_list(db, media_type="movie")
        return total

    assert await _visible() == 5
    await outage.record_trip("aaaaaaaa")
    await outage.record_trip("aaaaaaaa")
    assert await _visible() == 5  # 2 strikes: still below the threshold
    await outage.record_trip("aaaaaaaa")
    assert await _visible() == 0
    await outage.record_clear("aaaaaaaa")
    assert await _visible() == 5  # recovery is immediate on the next read


@pytest.mark.asyncio
async def test_admin_still_sees_media_of_a_masked_account(
    monkeypatch, outage_db, db_factory
):
    """The operator's diagnostic view must show what the outage is hiding."""
    monkeypatch.setattr(settings, "ACCOUNT_OUTAGE_STRIKES", 1)
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", 5):
            db.add(m)
        await db.commit()
    await outage.record_trip("aaaaaaaa")

    async with db_factory() as db:
        _items, total = await media_service.get_media_list(
            db, media_type="movie", apply_outage_mask=False,
        )
    assert total == 5


@pytest.mark.asyncio
async def test_unified_list_masks_on_both_snapshot_and_live_paths(
    monkeypatch, outage_db, db_factory
):
    """The default browse pages over the `media_group` snapshot, which carries
    no outage awareness of its own — the mask has to bite at hydration too, or
    the whole feature is bypassed on the app's normal path."""
    from app.services import unified_group_service

    monkeypatch.setattr(settings, "ACCOUNT_OUTAGE_STRIKES", 1)
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        db.add(_account("bbbbbbbb"))
        for i, m in enumerate(_streams("xtream_aaaaaaaa", 3)):
            m.title = f"Down {i}"
            m.unification_id = f"imdb://tt000{i}"
            db.add(m)
        for i, m in enumerate(_streams("xtream_bbbbbbbb", 2, offset=100)):
            m.title = f"Alive {i}"
            m.unification_id = f"imdb://tt999{i}"
            db.add(m)
        await db.commit()

    await unified_group_service.rebuild_all(db_factory)

    async def _titles(**kwargs) -> set[str]:
        outage.invalidate_mask_cache()
        async with db_factory() as db:
            groups, _total = await media_service.get_unified_list(
                db, "movie", **kwargs,
            )
        return {g.best.title for g in groups}

    # search= forces the live aggregation path; no filter uses the snapshot.
    assert len(await _titles()) == 5
    assert await _titles(search="Down") == {"Down 0", "Down 1", "Down 2"}

    await outage.record_trip("aaaaaaaa")

    assert await _titles() == {"Alive 0", "Alive 1"}
    assert await _titles(search="Down") == set()


@pytest.mark.asyncio
async def test_masking_never_touches_the_generated_library(
    monkeypatch, outage_db, db_factory
):
    """The whole point of not reusing `is_active`/`is_broken`: a provider
    outage must not make the Plex/Jellyfin generator delete anything."""
    monkeypatch.setattr(settings, "ACCOUNT_OUTAGE_STRIKES", 1)
    async with db_factory() as db:
        db.add(_account("aaaaaaaa"))
        for m in _streams("xtream_aaaaaaaa", 3):
            db.add(m)
        await db.commit()
    await outage.record_trip("aaaaaaaa")

    async with db_factory() as db:
        acc = (await db.execute(
            select(XtreamAccount).where(XtreamAccount.id == "aaaaaaaa")
        )).scalar_one()
        rows = (await db.execute(select(Media))).scalars().all()

    assert acc.is_active is True           # generator still loads the account
    assert not any(r.is_broken for r in rows)  # and still publishes its versions
