"""Tests for Lot 7: targeted NFO/poster refresh and cache/edit retention."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base, MetadataEdit, ScrapeCacheEntry
from app.plex_generator.generator import PlexLibraryGenerator
from app.plex_generator.models import PlexMovie, PlexMovieVersion, PlexSeries
from app.plex_generator.storage import DryRunStorage, LocalStorage
from app.services import metadata_service, plex_generation_service, scrape_gateway_cache, startup_tasks


class _FakeSource:
    def __init__(self, movies: list[PlexMovie], series: list[PlexSeries] | None = None) -> None:
        self._movies = movies
        self._series = series or []

    async def get_movies(self):
        return list(self._movies)

    async def get_series(self):
        return list(self._series)


@pytest.mark.asyncio
async def test_targeted_nfo_and_poster_refresh_only_touches_dirty_item(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    dirty_movie = PlexMovie(
        source_id="tmdb://101",
        title="Edited Movie",
        year=2024,
        summary="New summary",
        poster_url="https://example.com/new.jpg",
        versions=[PlexMovieVersion(server_id="xt_1", source_id="vod_1", stream_url="http://x/1")],
    )
    clean_movie = PlexMovie(
        source_id="tmdb://202",
        title="Untouched Movie",
        year=2023,
        summary="New untouched summary",
        poster_url="https://example.com/untouched.jpg",
        versions=[PlexMovieVersion(server_id="xt_1", source_id="vod_2", stream_url="http://x/2")],
    )

    # Pre-seed existing NFOs and posters on disk (simulating hand-edited TMM files)
    dirty_dir = tmp_path / "Films" / "Edited Movie (2024)"
    clean_dir = tmp_path / "Films" / "Untouched Movie (2023)"
    dirty_dir.mkdir(parents=True)
    clean_dir.mkdir(parents=True)
    (dirty_dir / "movie.nfo").write_text("OLD_DIRTY_NFO", encoding="utf-8")
    (dirty_dir / "poster.jpg").write_bytes(b"OLD_DIRTY_IMG")
    (clean_dir / "movie.nfo").write_text("HAND_EDITED_NFO", encoding="utf-8")
    (clean_dir / "poster.jpg").write_bytes(b"HAND_EDITED_IMG")

    def _fake_download(full: Path, url: str) -> None:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"NEW_IMG:" + url.encode("utf-8"))

    from concurrent.futures import ThreadPoolExecutor

    import app.plex_generator.storage as storage_mod

    with (
        ThreadPoolExecutor(max_workers=2) as test_pool,
        patch.object(storage_mod, "_image_pool", test_pool),
        patch.object(LocalStorage, "_download_sync", side_effect=_fake_download),
    ):
        gen = PlexLibraryGenerator(
            _FakeSource([dirty_movie, clean_movie]),
            storage,
            tmp_path,
            force_refresh_metadata=False,
            force_refresh_keys={("xt_1", "vod_1")},
        )
        report = await gen.generate()

    assert "New summary" in (dirty_dir / "movie.nfo").read_text(encoding="utf-8")
    assert (dirty_dir / "poster.jpg").read_bytes() == b"NEW_IMG:https://example.com/new.jpg"
    assert (clean_dir / "movie.nfo").read_text(encoding="utf-8") == "HAND_EDITED_NFO"
    assert (clean_dir / "poster.jpg").read_bytes() == b"HAND_EDITED_IMG"
    assert report.updated >= 1


def test_mark_dirty_media_queues_and_dedups() -> None:
    plex_generation_service._dirty_media_keys.clear()
    plex_generation_service.mark_dirty_media([("xt_1", "vod_1"), ("xt_1", "vod_1"), "tmdb://101", ""])
    dirty = plex_generation_service.get_dirty_media()
    assert dirty == {("xt_1", "vod_1"), "tmdb://101"}
    plex_generation_service._dirty_media_keys.clear()


@pytest.mark.asyncio
async def test_scrape_cache_purge_retention_evicts_expired_and_excess(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    now = 1_000_000_000_000
    async with session_factory() as s:
        s.add_all([
            ScrapeCacheEntry(
                family="tmdb_match", key_version=1, schema_version=1, language="fr-FR",
                cache_key="expired", result="hit", payload="{}",
                fetched_at=now - 5000, expires_at=now - 1,
            ),
            ScrapeCacheEntry(
                family="tmdb_match", key_version=1, schema_version=1, language="fr-FR",
                cache_key="old", result="hit", payload="{}",
                fetched_at=now - 4000, expires_at=now + 100_000,
            ),
            ScrapeCacheEntry(
                family="tmdb_match", key_version=1, schema_version=1, language="fr-FR",
                cache_key="mid", result="hit", payload="{}",
                fetched_at=now - 3000, expires_at=now + 100_000,
            ),
            ScrapeCacheEntry(
                family="tmdb_match", key_version=1, schema_version=1, language="fr-FR",
                cache_key="new", result="hit", payload="{}",
                fetched_at=now - 1000, expires_at=now + 100_000,
            ),
        ])
        await s.commit()

    expired, evicted = await scrape_gateway_cache.purge_retention(
        max_entries=2, now=now, session_factory=session_factory
    )
    assert expired == 1
    assert evicted == 1

    async with session_factory() as s:
        keys = set((await s.execute(select(ScrapeCacheEntry.cache_key))).scalars().all())
    assert keys == {"mid", "new"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_metadata_edits_purge_old_edits_respects_retention_days(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'edits.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    now = 2_000_000_000_000
    day_ms = 86_400_000
    async with session_factory() as s:
        s.add_all([
            MetadataEdit(
                edit_id="01OLD", kind="patch", at_ms=now - 400 * day_ms,
                server_id="xt_1", rating_key="vod_1", item_type="movie",
                key_id="master", request_id="r1",
                revision_before="a", revision_after="b",
                before_json="{}", after_json="{}",
            ),
            MetadataEdit(
                edit_id="01NEW", kind="patch", at_ms=now - 10 * day_ms,
                server_id="xt_1", rating_key="vod_1", item_type="movie",
                key_id="master", request_id="r2",
                revision_before="b", revision_after="c",
                before_json="{}", after_json="{}",
            ),
        ])
        await s.commit()

    assert await metadata_service.purge_old_edits(0, now=now, session_factory=session_factory) == 0
    deleted = await metadata_service.purge_old_edits(365, now=now, session_factory=session_factory)
    assert deleted == 1

    async with session_factory() as s:
        remaining = (await s.execute(select(func.count()).select_from(MetadataEdit))).scalar_one()
        ids = (await s.execute(select(MetadataEdit.edit_id))).scalars().all()
    assert remaining == 1
    assert ids == ["01NEW"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_retention_caches_wires_both_purges() -> None:
    with (
        patch.object(scrape_gateway_cache, "purge_retention", new=AsyncMock(return_value=(3, 2))) as m_sc,
        patch.object(metadata_service, "purge_old_edits", new=AsyncMock(return_value=4)) as m_me,
    ):
        res = await startup_tasks.cleanup_retention_caches()

    assert res == {"expired": 3, "evicted": 2, "metadata_edits": 4}
    m_sc.assert_awaited_once()
    m_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_storage_with_force_refresh_keys(tmp_path: Path) -> None:
    movie = PlexMovie(
        source_id="tmdb://303",
        title="DryRun Movie",
        year=2025,
        poster_url="https://example.com/p.jpg",
        fanart_url="https://example.com/f.jpg",
        versions=[PlexMovieVersion(server_id="xt_1", source_id="vod_3", stream_url="http://x/3")],
    )
    gen = PlexLibraryGenerator(
        _FakeSource([movie]),
        DryRunStorage(),
        tmp_path,
        force_refresh_keys={("xt_1", "vod_3")},
    )
    report = await gen.generate()
    assert report.created == 1
