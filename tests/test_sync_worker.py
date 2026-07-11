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
    upsert_media_batch,
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


# ─── upsert_media_batch: pagination-slot eviction (CR-F02) ─────


class TestUpsertMediaBatchPaginationSlots:
    """CR-F02 regression: reordering a category must never delete a row that
    is still listed, only a row that is genuinely delisted.

    `uix_media_pagination` (server_id, library_section_id, filter, sort_order,
    page_offset) is a UNIQUE index distinct from the row's real identity/PK
    (rating_key, server_id, filter, sort_order). Before the fix, any incoming
    row simply deleted whatever OTHER rating_key occupied its target
    page_offset -- including a still-listed row whose content happened to be
    unchanged this sync (and therefore never itself re-upserted), silently
    dropping its enrichment (tmdb_id/unification_id) until re-scraped.
    """

    async def test_reorder_preserves_enrichment_but_still_evicts_delisted(self, db_session):
        sid = "xtream_acc1"

        # Still listed this sync, just shifted to a different page_offset --
        # its content is unchanged so it is never itself re-upserted.
        displaced = Media(
            rating_key="vod_keep.mp4", server_id=sid, library_section_id="xtream_vod",
            filter="5", sort_order="default", page_offset=7,
            title="Kept Movie", type="movie",
            tmdb_id="603", unification_id="tmdb://603", history_group_key="hg_keep",
            content_hash="old", dto_hash="old",
        )
        # Genuinely delisted by the provider this sync.
        gone = Media(
            rating_key="vod_gone.mp4", server_id=sid, library_section_id="xtream_vod",
            filter="5", sort_order="default", page_offset=9,
            title="Gone Movie", type="movie",
            content_hash="old", dto_hash="old",
        )
        db_session.add_all([displaced, gone])
        await db_session.commit()

        now = 1_000
        # Two changed/new items land exactly on the old slots of `displaced`
        # (reorder) and `gone` (replaced by a new listing at that position).
        mover = {
            "rating_key": "vod_mover.mp4", "server_id": sid, "library_section_id": "xtream_vod",
            "filter": "5", "sort_order": "default", "page_offset": 7,
            "title": "Mover Movie", "title_sortable": "mover movie", "type": "movie",
            "year": 2020, "unification_id": "title_movermovie_2020",
            "history_group_key": "hg_mover", "media_parts": "[]",
            "added_at": now, "updated_at": now,
        }
        replacement = {
            "rating_key": "vod_new.mp4", "server_id": sid, "library_section_id": "xtream_vod",
            "filter": "5", "sort_order": "default", "page_offset": 9,
            "title": "New Movie", "title_sortable": "new movie", "type": "movie",
            "year": 2021, "unification_id": "title_newmovie_2021",
            "history_group_key": "hg_new", "media_parts": "[]",
            "added_at": now, "updated_at": now,
        }

        # The current sync's full listing: vod_keep is still present (moved
        # elsewhere in the provider's list); vod_gone is not in it anymore.
        current_rating_keys = {"vod_mover.mp4", "vod_new.mp4", "vod_keep.mp4"}

        await upsert_media_batch(
            db_session, [mover, replacement], current_rating_keys=current_rating_keys
        )
        await db_session.commit()

        rows = (await db_session.execute(select(Media))).scalars().all()
        by_key = {r.rating_key: r for r in rows}

        # Still-listed row survives with its enrichment fully intact...
        assert "vod_keep.mp4" in by_key
        kept = by_key["vod_keep.mp4"]
        assert kept.tmdb_id == "603"
        assert kept.unification_id == "tmdb://603"
        # ...just relocated off the slot the mover now occupies.
        assert kept.page_offset != 7

        # Genuinely delisted row is removed.
        assert "vod_gone.mp4" not in by_key

        # Both incoming rows landed at their intended slots.
        assert by_key["vod_mover.mp4"].page_offset == 7
        assert by_key["vod_new.mp4"].page_offset == 9

    async def test_no_listing_scope_never_deletes_only_relocates(self, db_session):
        """Without a full-listing scope (current_rating_keys=None, e.g. the
        per-series episode batches), a slot collision must never delete the
        existing row -- only relocate it out of the way."""
        sid = "xtream_acc1"
        existing = Media(
            rating_key="ep_1.mp4", server_id=sid, library_section_id="xtream_series",
            filter="all", sort_order="default", page_offset=42,
            title="Ep 1", type="episode",
            tmdb_id="42", unification_id="tmdb://42",
        )
        db_session.add(existing)
        await db_session.commit()

        incoming = {
            "rating_key": "ep_2.mp4", "server_id": sid, "library_section_id": "xtream_series",
            "filter": "all", "sort_order": "default", "page_offset": 42,
            "title": "Ep 2", "title_sortable": "ep 2", "type": "episode",
            "media_parts": "[]", "added_at": 1, "updated_at": 1,
            "unification_id": "", "history_group_key": "",
        }

        await upsert_media_batch(db_session, [incoming])  # no current_rating_keys
        await db_session.commit()

        rows = (await db_session.execute(select(Media))).scalars().all()
        by_key = {r.rating_key: r for r in rows}

        assert "ep_1.mp4" in by_key
        assert by_key["ep_1.mp4"].tmdb_id == "42"
        assert by_key["ep_1.mp4"].unification_id == "tmdb://42"
        assert by_key["ep_1.mp4"].page_offset != 42  # relocated, not deleted
        assert by_key["ep_2.mp4"].page_offset == 42

    async def test_cross_sync_relocation_is_collision_free(self, db_session):
        """Regression: a FIXED additive sentinel (page_offset += 1_000_000_000)
        is NOT collision-free across repeated syncs.

        Reproduces the "provider prepends new content" pattern across two
        sequential `upsert_media_batch` calls (two syncs): each prepend
        relocates whatever unchanged row currently sits at offset 0. With a
        fixed additive sentinel, relocating a row that starts at offset 0
        always lands it at exactly `0 + SENTINEL` -- so a SECOND prepend, one
        sync later, relocates a DIFFERENT row that also started at 0 to the
        very same `SENTINEL` value, colliding with the first relocation and
        raising an IntegrityError on `uix_media_pagination` (rolling back the
        whole batch/savepoint -- new/changed content on that page would then
        persistently fail to sync).
        """
        sid = "xtream_acc1"

        # Pre-existing enriched row occupying slot 0 before "sync N".
        row_a = Media(
            rating_key="vod_A.mp4", server_id=sid, library_section_id="xtream_vod",
            filter="5", sort_order="default", page_offset=0,
            title="A", type="movie",
            tmdb_id="700", unification_id="tmdb://700", history_group_key="hg_a",
        )
        db_session.add(row_a)
        await db_session.commit()

        now = 1_000

        # --- Sync N: provider prepends a new item at offset 0. "A" is still
        # listed (unchanged this sync), so it must be relocated, not deleted.
        new1 = {
            "rating_key": "vod_new1.mp4", "server_id": sid, "library_section_id": "xtream_vod",
            "filter": "5", "sort_order": "default", "page_offset": 0,
            "title": "New1", "title_sortable": "new1", "type": "movie",
            "year": 2022, "unification_id": "title_new1_2022", "history_group_key": "hg_new1",
            "media_parts": "[]", "added_at": now, "updated_at": now,
            "tmdb_id": "800",
        }
        await upsert_media_batch(
            db_session, [new1], current_rating_keys={"vod_new1.mp4", "vod_A.mp4"}
        )
        await db_session.commit()

        # --- Sync N+1: provider prepends ANOTHER new item at offset 0. The
        # row now occupying slot 0 ("new1", unchanged since sync N) must again
        # be relocated -- and must NOT collide with wherever "A" landed.
        new2 = {
            "rating_key": "vod_new2.mp4", "server_id": sid, "library_section_id": "xtream_vod",
            "filter": "5", "sort_order": "default", "page_offset": 0,
            "title": "New2", "title_sortable": "new2", "type": "movie",
            "year": 2023, "unification_id": "title_new2_2023", "history_group_key": "hg_new2",
            "media_parts": "[]", "added_at": now, "updated_at": now,
        }
        # Must not raise IntegrityError.
        await upsert_media_batch(
            db_session, [new2],
            current_rating_keys={"vod_new2.mp4", "vod_new1.mp4", "vod_A.mp4"},
        )
        await db_session.commit()

        rows = (await db_session.execute(select(Media))).scalars().all()
        by_key = {r.rating_key: r for r in rows}

        # Both previously-relocated rows survive with their enrichment intact.
        assert "vod_A.mp4" in by_key
        assert by_key["vod_A.mp4"].tmdb_id == "700"
        assert by_key["vod_A.mp4"].unification_id == "tmdb://700"

        assert "vod_new1.mp4" in by_key
        assert by_key["vod_new1.mp4"].tmdb_id == "800"

        # New2 took slot 0; the two relocated rows are distinct from each
        # other and from slot 0.
        assert by_key["vod_new2.mp4"].page_offset == 0
        offsets = {by_key["vod_A.mp4"].page_offset, by_key["vod_new1.mp4"].page_offset}
        assert len(offsets) == 2
        assert 0 not in offsets
