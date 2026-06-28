"""Tests for sync_worker: duration parsing, account lock, cleanup, server_id utility."""
import asyncio

import pytest

from sqlalchemy import select

from app.models.database import Media, EnrichmentQueue
from app.workers.sync_worker import (
    _parse_duration_ms,
    _safe_duration,
    _get_account_lock,
    _compute_dto_hash,
    _record_sync_job,
    get_sync_job,
    get_all_sync_jobs,
    cleanup_orphan_enrichment_queue,
)
from app.utils.server_id import parse_server_id, build_server_id


# ─── Duration Parsing ───────────────────────────────────────────


class TestParseDurationMs:
    def test_integer_seconds(self):
        assert _parse_duration_ms(3600) == 3600000

    def test_string_seconds(self):
        assert _parse_duration_ms("120") == 120000

    def test_hhmmss_format(self):
        assert _parse_duration_ms("01:23:45") == (1 * 3600 + 23 * 60 + 45) * 1000

    def test_mmss_format(self):
        assert _parse_duration_ms("45:30") == (45 * 60 + 30) * 1000

    def test_none(self):
        assert _parse_duration_ms(None) is None

    def test_empty_string(self):
        assert _parse_duration_ms("") is None

    def test_invalid_string(self):
        assert _parse_duration_ms("invalid") is None

    def test_zero(self):
        assert _parse_duration_ms(0) == 0

    def test_float_string(self):
        # "3600.5" can't be parsed by int(), but should not crash
        assert _parse_duration_ms("3600.5") is None

    def test_negative(self):
        # Negative values are parsed as int
        assert _parse_duration_ms("-100") == -100000

    def test_hhmmss_midnight(self):
        assert _parse_duration_ms("00:00:00") == 0


class TestSafeDuration:
    def test_minutes_to_ms(self):
        assert _safe_duration(45) == 45 * 60_000

    def test_string_minutes(self):
        assert _safe_duration("90") == 90 * 60_000

    def test_none(self):
        assert _safe_duration(None) is None

    def test_invalid(self):
        assert _safe_duration("bad") is None


# ─── Account Lock ───────────────────────────────────────────────


class TestAccountLock:
    def test_same_lock_returned(self):
        lock1 = _get_account_lock("test_account")
        lock2 = _get_account_lock("test_account")
        assert lock1 is lock2

    def test_different_accounts_different_locks(self):
        lock1 = _get_account_lock("account_a")
        lock2 = _get_account_lock("account_b")
        assert lock1 is not lock2


# ─── DTO Hash ───────────────────────────────────────────────────


class TestDtoHash:
    def test_same_input_same_hash(self):
        dto = {"name": "Test", "added": "123", "rating": "8.0",
               "category_id": "1", "stream_icon": "http://img", "container_extension": "mp4"}
        assert _compute_dto_hash(dto) == _compute_dto_hash(dto)

    def test_different_input_different_hash(self):
        dto1 = {"name": "Test1", "added": "123"}
        dto2 = {"name": "Test2", "added": "123"}
        assert _compute_dto_hash(dto1) != _compute_dto_hash(dto2)

    def test_missing_fields_handled(self):
        # Should not crash with empty dict
        h = _compute_dto_hash({})
        assert isinstance(h, str)
        assert len(h) == 32  # MD5 hex

    def test_volatile_fields_ignored(self):
        # Regression: some mirrors flap stream_icon (poster url vs "") and rating
        # (real vs 0) between identical requests. Those must NOT change the hash,
        # else most items look "changed" every sync (needless re-fetch + churn).
        base = {"name": "Test", "added": "123", "category_id": "1",
                "container_extension": "mp4"}
        a = {**base, "stream_icon": "https://image.tmdb.org/x.jpg", "rating": "8.2"}
        b = {**base, "stream_icon": "", "rating": "0"}
        assert _compute_dto_hash(a) == _compute_dto_hash(b)

    def test_real_change_still_detected(self):
        # A genuine availability/identity change must still flip the hash.
        base = {"name": "Test", "added": "123", "category_id": "1",
                "container_extension": "mp4"}
        assert _compute_dto_hash(base) != _compute_dto_hash({**base, "added": "999"})
        assert _compute_dto_hash(base) != _compute_dto_hash({**base, "container_extension": "mkv"})


# ─── Sync Job Tracking ─────────────────────────────────────────


class TestSyncJobTracking:
    def test_record_and_get(self):
        _record_sync_job("test_job_1", {"status": "processing"})
        job = get_sync_job("test_job_1")
        assert job is not None
        assert job["status"] == "processing"

    def test_get_nonexistent(self):
        assert get_sync_job("nonexistent_job_xyz") is None

    def test_get_all_jobs(self):
        _record_sync_job("list_test_1", {"status": "done"})
        _record_sync_job("list_test_2", {"status": "processing"})
        jobs = get_all_sync_jobs()
        assert isinstance(jobs, list)
        ids = [j["job_id"] for j in jobs]
        assert "list_test_1" in ids
        assert "list_test_2" in ids


# ─── Server ID Utility ─────────────────────────────────────────


class TestServerIdUtility:
    def test_parse_valid(self):
        assert parse_server_id("xtream_abc123") == "abc123"

    def test_parse_invalid_prefix(self):
        assert parse_server_id("plex_abc") is None

    def test_parse_empty(self):
        assert parse_server_id("") is None

    def test_parse_none(self):
        assert parse_server_id(None) is None

    def test_build(self):
        assert build_server_id("abc123") == "xtream_abc123"

    def test_roundtrip(self):
        account_id = "my_account"
        assert parse_server_id(build_server_id(account_id)) == account_id


# ─── Orphan enrichment_queue cleanup ───────────────────────────


def _media(rating_key: str, server_id: str, title: str = "X") -> Media:
    return Media(
        rating_key=rating_key, server_id=server_id, library_section_id="1",
        title=title, type="movie",
    )


def _queue(rating_key: str, server_id: str, title: str = "X") -> EnrichmentQueue:
    return EnrichmentQueue(
        rating_key=rating_key, server_id=server_id, media_type="movie",
        title=title, status="skipped", attempts=3, created_at=0,
    )


class TestOrphanEnrichmentQueueCleanup:
    async def test_purges_only_orphans(self, db_session):
        """A queue row whose media was deleted is removed; a live one survives."""
        sid = "xtream_acc1"
        db_session.add_all([
            _media("vod_live.mkv", sid),       # media present
            _queue("vod_live.mkv", sid),       # → keep
            _queue("vod_gone.mkv", sid),       # no media → orphan, drop
        ])
        await db_session.commit()

        removed = await cleanup_orphan_enrichment_queue(db_session, sid)
        await db_session.commit()

        assert removed == 1
        remaining = (await db_session.execute(
            select(EnrichmentQueue.rating_key)
        )).scalars().all()
        assert remaining == ["vod_live.mkv"]

    async def test_scoped_to_server(self, db_session):
        """The sweep must not touch another server's orphan rows."""
        db_session.add_all([
            _queue("vod_gone.mkv", "xtream_a"),   # orphan on server A
            _queue("vod_gone.mkv", "xtream_b"),   # orphan on server B
        ])
        await db_session.commit()

        removed = await cleanup_orphan_enrichment_queue(db_session, "xtream_a")
        await db_session.commit()

        assert removed == 1
        servers = (await db_session.execute(
            select(EnrichmentQueue.server_id)
        )).scalars().all()
        assert servers == ["xtream_b"]

    async def test_idempotent(self, db_session):
        """A second sweep on a clean server removes nothing."""
        sid = "xtream_acc1"
        db_session.add_all([_media("vod_1.mkv", sid), _queue("vod_1.mkv", sid)])
        await db_session.commit()

        assert await cleanup_orphan_enrichment_queue(db_session, sid) == 0
