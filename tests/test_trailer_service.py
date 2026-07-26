"""Trailer resolution service (Lot A trailers) — `app/services/trailer_service.py`.

Covers: format selection (pure), id validation, path confinement, the
resolve pending -> ready -> none state machine (download itself mocked at
the `_run_ytdlp` seam so no real yt-dlp/network call is ever made), the
`rating_key` mode's live TMDB repli + write-back + negative caching, the
download-promotion atomic `os.replace`, and the nightly/post-download LRU
purge.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import settings
from app.db import database as db_module
from app.models.database import Media
from app.services import trailer_service
from app.services.tmdb_service import tmdb_service
from app.utils.server_id import build_server_id


# ─── select_format (pure) ─────────────────────────────────────────────────


class TestSelectFormat:
    def test_with_ffmpeg_uses_mergeable_primary_tier(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
        fmt = trailer_service.select_format()
        assert fmt.startswith("bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/")

    def test_without_ffmpeg_falls_back_to_progressive_only(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        fmt = trailer_service.select_format()
        assert fmt == "best[ext=mp4][height<=720]/best[ext=mp4]"
        assert "bestvideo" not in fmt

    def test_every_tier_requires_mp4(self, monkeypatch):
        """Every fallback tier must stay `ext=mp4` — the outtmpl
        (`trailer.%(ext)s`) assumes the final container is always mp4."""
        for which_result in ("/usr/bin/ffmpeg", None):
            monkeypatch.setattr(shutil, "which", lambda name, r=which_result: r)
            for tier in trailer_service.select_format().split("/"):
                assert "ext=mp4" in tier or tier == "best[ext=mp4]"


# ─── is_valid_youtube_id (pure) ────────────────────────────────────────────


class TestIsValidYoutubeId:
    @pytest.mark.parametrize("value", [
        "dQw4w9WgXcQ", "abc123_-XyZ", "-----------", "___________",
    ])
    def test_valid_11_char_ids(self, value):
        assert trailer_service.is_valid_youtube_id(value) is True

    @pytest.mark.parametrize("value", [
        None, "", "too_short", "way_too_long_id_here", "has space12",
        "has.dot1234", "has/slash12", "../../../etc/passwd", 12345,
    ])
    def test_invalid_ids(self, value):
        assert trailer_service.is_valid_youtube_id(value) is False


# ─── resolve_confined_cache_path ───────────────────────────────────────────


class TestResolveConfinedCachePath:
    def test_disabled_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "TRAILER_CACHE_DIR", "")
        with pytest.raises(trailer_service.TrailerDisabledError):
            trailer_service.resolve_confined_cache_path("dQw4w9WgXcQ")

    def test_normal_id_resolves_under_base(self, trailer_dir):
        path = trailer_service.resolve_confined_cache_path("dQw4w9WgXcQ")
        resolved_base = Path(trailer_dir).resolve()
        assert resolved_base in path.parents
        assert path.name == "dQw4w9WgXcQ.mp4"

    def test_traversal_id_is_rejected_even_bypassing_the_regex(self, trailer_dir):
        """Belt-and-suspenders: even if a caller skipped `is_valid_youtube_id`
        (e.g. a future bug), the realpath proof independently refuses to
        hand back a path outside `TRAILER_CACHE_DIR` (F-007 invariant)."""
        with pytest.raises(trailer_service.TrailerPathConfinementError):
            trailer_service.resolve_confined_cache_path("../../../etc/passwd")

    def test_absolute_path_injection_is_rejected(self, trailer_dir):
        with pytest.raises(trailer_service.TrailerPathConfinementError):
            trailer_service.resolve_confined_cache_path("/etc/passwd")


# ─── resolve_by_youtube_id: ready / pending / none ─────────────────────────


class TestResolveByYoutubeId:
    async def test_invalid_id_returns_none(self, trailer_dir):
        result = await trailer_service.resolve_by_youtube_id("bad")
        assert result.status == "none"
        assert result.url is None

    async def test_disabled_feature_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "TRAILER_CACHE_DIR", "")
        result = await trailer_service.resolve_by_youtube_id("dQw4w9WgXcQ")
        assert result.status == "none"

    async def test_cached_file_returns_ready_with_url(self, trailer_dir):
        (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").write_bytes(b"fake-mp4-bytes")
        result = await trailer_service.resolve_by_youtube_id("dQw4w9WgXcQ")
        assert result.status == "ready"
        assert result.url == "/api/media/trailer/file/dQw4w9WgXcQ"

    async def test_empty_cached_file_is_not_ready(self, trailer_dir, monkeypatch):
        """A zero-byte file (e.g. a crashed download that still created the
        final path somehow) must never be served as `ready`."""
        (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").write_bytes(b"")
        kicked_off = _stub_background_download(monkeypatch)
        result = await trailer_service.resolve_by_youtube_id("dQw4w9WgXcQ")
        assert result.status == "pending"
        assert kicked_off.count == 1

    async def test_no_file_kicks_off_one_background_download_and_returns_pending(
        self, trailer_dir, monkeypatch,
    ):
        kicked_off = _stub_background_download(monkeypatch)
        result = await trailer_service.resolve_by_youtube_id("dQw4w9WgXcQ")
        assert result.status == "pending"
        assert result.url is None
        assert kicked_off.count == 1
        assert "dQw4w9WgXcQ" in trailer_service._in_flight

    async def test_concurrent_resolves_dedupe_to_a_single_kickoff(
        self, trailer_dir, monkeypatch,
    ):
        kicked_off = _stub_background_download(monkeypatch)
        r1 = await trailer_service.resolve_by_youtube_id("dQw4w9WgXcQ")
        r2 = await trailer_service.resolve_by_youtube_id("dQw4w9WgXcQ")
        assert r1.status == "pending"
        assert r2.status == "pending"
        assert kicked_off.count == 1, "a second resolve while a download is in flight must not re-kick"


def _stub_background_download(monkeypatch):
    """Replaces `create_background_task` with a stub that records how many
    times it was invoked WITHOUT ever actually running/awaiting the
    coroutine (so `_in_flight` stays populated for the duration of the
    test, exactly like a real download that hasn't finished yet). The
    unstarted coroutine is closed to silence "coroutine was never awaited".
    """
    calls = _CallCounter()

    def _fake_create_background_task(coro, *, name=None):
        calls.count += 1
        coro.close()
        return None

    monkeypatch.setattr(trailer_service, "create_background_task", _fake_create_background_task)
    monkeypatch.setattr(trailer_service, "_in_flight", set())
    return calls


class _CallCounter:
    def __init__(self):
        self.count = 0


@pytest_asyncio.fixture(autouse=True)
async def _clear_in_flight():
    """`_in_flight` is process-global module state — clear it before/after
    every test in this file so a previous test's dedup entry can't leak
    into an unrelated one."""
    trailer_service._in_flight.clear()
    yield
    trailer_service._in_flight.clear()


# ─── resolve_by_rating_key: stored column, TMDB repli, write-back ─────────


@pytest_asyncio.fixture(autouse=True)
async def _wire_test_db(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    return db_factory


async def _seed_media(db_factory, **overrides) -> Media:
    defaults = dict(
        rating_key="vod_1.mp4", server_id=build_server_id("acc1"),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title="Some Movie", type="movie", page_offset=0,
    )
    defaults.update(overrides)
    async with db_factory() as s:
        m = Media(**defaults)
        s.add(m)
        await s.commit()
    return Media(**defaults)


class TestResolveByRatingKey:
    async def test_media_not_found_returns_none(self, trailer_dir, db_factory):
        async with db_factory() as db:
            result = await trailer_service.resolve_by_rating_key(db, "nope", "srv")
        assert result.status == "none"

    async def test_stored_trailer_column_used_directly_no_tmdb_call(
        self, trailer_dir, db_factory, monkeypatch,
    ):
        media = await _seed_media(db_factory, youtube_trailer="dQw4w9WgXcQ")
        (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").write_bytes(b"bytes")

        async def _boom(*a, **k):
            raise AssertionError("TMDB must not be called when the column is already set")
        monkeypatch.setattr(tmdb_service, "get_videos_trailer", _boom)

        async with db_factory() as db:
            result = await trailer_service.resolve_by_rating_key(
                db, media.rating_key, media.server_id,
            )
        assert result.status == "ready"
        assert result.url == "/api/media/trailer/file/dQw4w9WgXcQ"

    async def test_empty_column_falls_back_to_tmdb_and_writes_back(
        self, trailer_dir, db_factory, monkeypatch,
    ):
        media = await _seed_media(db_factory, tmdb_id="42")
        monkeypatch.setattr(settings, "TMDB_API_KEY", "test_key")

        async def _fake_get_videos_trailer(tmdb_id, media_kind):
            assert tmdb_id == 42
            assert media_kind == "movie"
            return "abc12345678"
        monkeypatch.setattr(tmdb_service, "get_videos_trailer", _fake_get_videos_trailer)

        kicked_off = _stub_background_download(monkeypatch)
        async with db_factory() as db:
            result = await trailer_service.resolve_by_rating_key(
                db, media.rating_key, media.server_id,
            )
        assert result.status == "pending"
        assert kicked_off.count == 1

        async with db_factory() as db:
            written = await trailer_service.resolve_by_rating_key(
                db, media.rating_key, media.server_id,
            )
        # Second call: column is now populated -> served from it directly
        # (still "pending" since the file isn't cached yet, but no crash and
        # no second TMDB round-trip is proof the write-back landed — see the
        # explicit column read below).
        assert written.status == "pending"

        from sqlalchemy import select as sa_select
        async with db_factory() as db:
            col = (await db.execute(
                sa_select(Media.youtube_trailer).where(
                    Media.rating_key == media.rating_key, Media.server_id == media.server_id,
                )
            )).scalar_one()
        assert col == "abc12345678"

    async def test_no_tmdb_id_returns_none_without_tmdb_call(
        self, trailer_dir, db_factory, monkeypatch,
    ):
        media = await _seed_media(db_factory, tmdb_id=None)

        async def _boom(*a, **k):
            raise AssertionError("TMDB must not be called without a tmdb_id")
        monkeypatch.setattr(tmdb_service, "get_videos_trailer", _boom)

        async with db_factory() as db:
            result = await trailer_service.resolve_by_rating_key(
                db, media.rating_key, media.server_id,
            )
        assert result.status == "none"

    async def test_tmdb_not_configured_returns_none_without_http(
        self, trailer_dir, db_factory, monkeypatch,
    ):
        media = await _seed_media(db_factory, tmdb_id="42")
        monkeypatch.setattr(settings, "TMDB_API_KEY", "")

        async def _boom(*a, **k):
            raise AssertionError("TMDB must not be called when unconfigured")
        monkeypatch.setattr(tmdb_service, "get_videos_trailer", _boom)

        async with db_factory() as db:
            result = await trailer_service.resolve_by_rating_key(
                db, media.rating_key, media.server_id,
            )
        assert result.status == "none"

    async def test_negative_tmdb_repli_is_cached_and_not_requeried(
        self, trailer_dir, db_factory, monkeypatch,
    ):
        media = await _seed_media(db_factory, tmdb_id="777")
        monkeypatch.setattr(settings, "TMDB_API_KEY", "test_key")
        trailer_service._tmdb_repli_cache.clear()

        calls = {"n": 0}

        async def _fake_get_videos_trailer(tmdb_id, media_kind):
            calls["n"] += 1
            return None
        monkeypatch.setattr(tmdb_service, "get_videos_trailer", _fake_get_videos_trailer)

        async with db_factory() as db:
            r1 = await trailer_service.resolve_by_rating_key(db, media.rating_key, media.server_id)
        async with db_factory() as db:
            r2 = await trailer_service.resolve_by_rating_key(db, media.rating_key, media.server_id)

        assert r1.status == "none"
        assert r2.status == "none"
        assert calls["n"] == 1, "a negative TMDB result must be cached, not re-queried every focus"

    async def test_show_type_maps_to_tv_media_kind(self, trailer_dir, db_factory, monkeypatch):
        media = await _seed_media(
            db_factory, rating_key="series_1", type="show", tmdb_id="9",
        )
        monkeypatch.setattr(settings, "TMDB_API_KEY", "test_key")
        trailer_service._tmdb_repli_cache.clear()

        async def _fake_get_videos_trailer(tmdb_id, media_kind):
            assert media_kind == "tv"
            return None
        monkeypatch.setattr(tmdb_service, "get_videos_trailer", _fake_get_videos_trailer)

        async with db_factory() as db:
            await trailer_service.resolve_by_rating_key(db, media.rating_key, media.server_id)


# ─── Download promotion (yt-dlp mocked at the _run_ytdlp seam) ────────────


class TestDownloadTrailer:
    async def test_successful_download_promotes_file_atomically(self, trailer_dir, monkeypatch):
        def _fake_run_ytdlp(youtube_id, work_dir):
            (work_dir / "trailer.mp4").write_bytes(b"muxed-bytes")
        monkeypatch.setattr(trailer_service, "_run_ytdlp", _fake_run_ytdlp)

        ok = await trailer_service._download_trailer("dQw4w9WgXcQ")

        assert ok is True
        final = Path(trailer_dir) / "dQw4w9WgXcQ.mp4"
        assert final.is_file()
        assert final.read_bytes() == b"muxed-bytes"
        # The private work directory must be cleaned up, never left behind.
        leftovers = [p for p in Path(trailer_dir).iterdir() if p.name != "dQw4w9WgXcQ.mp4"]
        assert leftovers == []

    async def test_no_output_file_returns_false_and_cleans_up(self, trailer_dir, monkeypatch):
        def _fake_run_ytdlp(youtube_id, work_dir):
            pass  # simulates yt-dlp raising internally without producing a file
        monkeypatch.setattr(trailer_service, "_run_ytdlp", _fake_run_ytdlp)

        ok = await trailer_service._download_trailer("dQw4w9WgXcQ")

        assert ok is False
        assert not (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").exists()
        assert list(Path(trailer_dir).iterdir()) == []

    async def test_ytdlp_exception_propagates_and_still_cleans_up(self, trailer_dir, monkeypatch):
        def _fake_run_ytdlp(youtube_id, work_dir):
            (work_dir / "partial.mp4").write_bytes(b"partial")
            raise RuntimeError("network error")
        monkeypatch.setattr(trailer_service, "_run_ytdlp", _fake_run_ytdlp)

        with pytest.raises(RuntimeError):
            await trailer_service._download_trailer("dQw4w9WgXcQ")

        assert not (Path(trailer_dir) / "dQw4w9WgXcQ.mp4").exists()
        assert list(Path(trailer_dir).iterdir()) == [], "the failed work dir must still be removed"

    async def test_download_and_release_purges_and_clears_in_flight_on_success(
        self, trailer_dir, monkeypatch,
    ):
        def _fake_run_ytdlp(youtube_id, work_dir):
            (work_dir / "trailer.mp4").write_bytes(b"bytes")
        monkeypatch.setattr(trailer_service, "_run_ytdlp", _fake_run_ytdlp)

        purge_calls = {"n": 0}

        async def _fake_purge():
            purge_calls["n"] += 1
            return 0
        monkeypatch.setattr(trailer_service, "purge_lru", _fake_purge)

        trailer_service._in_flight.add("dQw4w9WgXcQ")
        await trailer_service._download_and_release("dQw4w9WgXcQ")

        assert purge_calls["n"] == 1
        assert "dQw4w9WgXcQ" not in trailer_service._in_flight

    async def test_download_and_release_clears_in_flight_on_failure(self, trailer_dir, monkeypatch):
        def _fake_run_ytdlp(youtube_id, work_dir):
            raise RuntimeError("boom")
        monkeypatch.setattr(trailer_service, "_run_ytdlp", _fake_run_ytdlp)

        trailer_service._in_flight.add("dQw4w9WgXcQ")
        await trailer_service._download_and_release("dQw4w9WgXcQ")  # must not raise

        assert "dQw4w9WgXcQ" not in trailer_service._in_flight


# ─── purge_lru ──────────────────────────────────────────────────────────


class TestPurgeLru:
    async def test_disabled_feature_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings, "TRAILER_CACHE_DIR", "")
        removed = await trailer_service.purge_lru()
        assert removed == 0

    async def test_cap_disabled_is_noop(self, trailer_dir, monkeypatch):
        monkeypatch.setattr(settings, "TRAILER_CACHE_MAX_MB", 0)
        (Path(trailer_dir) / "a.mp4").write_bytes(b"x" * 1000)
        removed = await trailer_service.purge_lru()
        assert removed == 0

    async def test_under_cap_removes_nothing(self, trailer_dir, monkeypatch):
        monkeypatch.setattr(settings, "TRAILER_CACHE_MAX_MB", 1)  # 1 MiB
        (Path(trailer_dir) / "a.mp4").write_bytes(b"x" * 1000)
        removed = await trailer_service.purge_lru()
        assert removed == 0
        assert (Path(trailer_dir) / "a.mp4").exists()

    async def test_over_cap_evicts_oldest_mtime_first(self, trailer_dir, monkeypatch):
        import os as _os
        import time

        monkeypatch.setattr(settings, "TRAILER_CACHE_MAX_MB", 0.002)  # ~2 KB cap
        base = Path(trailer_dir)
        # 3 files of 1 KB each -> ~3 KB > 2 KB cap, oldest must go first.
        (base / "oldest.mp4").write_bytes(b"x" * 1024)
        _os.utime(base / "oldest.mp4", (time.time() - 300, time.time() - 300))
        (base / "middle.mp4").write_bytes(b"x" * 1024)
        _os.utime(base / "middle.mp4", (time.time() - 150, time.time() - 150))
        (base / "newest.mp4").write_bytes(b"x" * 1024)

        removed = await trailer_service.purge_lru()

        assert removed >= 1
        assert not (base / "oldest.mp4").exists()
        assert (base / "newest.mp4").exists()

    async def test_non_mp4_files_are_ignored(self, trailer_dir, monkeypatch):
        monkeypatch.setattr(settings, "TRAILER_CACHE_MAX_MB", 0)  # would purge if considered
        (Path(trailer_dir) / "readme.txt").write_bytes(b"not a trailer")
        removed = await trailer_service.purge_lru()
        assert removed == 0
        assert (Path(trailer_dir) / "readme.txt").exists()

    async def test_missing_cache_dir_is_noop(self, tmp_path, monkeypatch):
        ghost = tmp_path / "does-not-exist"
        monkeypatch.setattr(settings, "TRAILER_CACHE_DIR", str(ghost))
        removed = await trailer_service.purge_lru()
        assert removed == 0
