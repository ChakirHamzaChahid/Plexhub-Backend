"""Integration tests for app.scripts.strip_titles_pollution.

Validates:
- Folder & file renaming on disk preserves .nfo / .jpg untouched (same content + same name)
- DB rows (Media + episodes' grandparent_title) get cleaned
- .plex_mapping.json paths get rewritten
- EnrichmentQueue gets reset to 'pending' for cleaned media missing IDs
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from app.models.database import Base, EnrichmentQueue, Media, XtreamAccount
from app.plex_generator.mapping import MappingStore
from app.scripts import strip_titles_pollution as mig
from app.utils.server_id import build_server_id


# ───────────────────────── fixtures ─────────────────────────


def _make_db():
    """Spin up an in-memory async SQLite DB with all tables."""
    async def _setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        return factory, engine

    return asyncio.run(_setup())


@pytest.fixture
def db_factory(monkeypatch):
    factory, _engine = _make_db()
    # Patch the module-level session factory used by the migration script
    monkeypatch.setattr(mig, "async_session_factory", factory)
    return factory


@pytest.fixture
def account_dir(tmp_path):
    """Pre-populated library structure with one polluted movie + one polluted series."""
    aid = "abc123"
    base = tmp_path / aid

    # Movie: FR - Better Man (2024) → should become Better Man (2024)
    movie_dir = base / "Films" / "FR - Better Man (2024)"
    movie_dir.mkdir(parents=True)
    (movie_dir / "FR - Better Man (2024).strm").write_text("http://stream/movie.mp4")
    (movie_dir / "movie.nfo").write_text("<movie><title>Better Man</title></movie>")
    (movie_dir / "poster.jpg").write_bytes(b"POSTER_BYTES")
    (movie_dir / "fanart.jpg").write_bytes(b"FANART_BYTES")

    # Series: FR - Chernobyl → should become Chernobyl
    series_dir = base / "Series" / "FR - Chernobyl"
    season_dir = series_dir / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "FR - Chernobyl S01E01.strm").write_text("http://stream/ep1.mp4")
    (season_dir / "FR - Chernobyl S01E01.nfo").write_text("<episodedetails/>")
    (season_dir / "FR - Chernobyl S01E02.strm").write_text("http://stream/ep2.mp4")
    (series_dir / "tvshow.nfo").write_text("<tvshow><title>Chernobyl</title></tvshow>")
    (series_dir / "poster.jpg").write_bytes(b"SERIES_POSTER")

    # Pre-existing mapping.json that points at the OLD paths
    mapping = MappingStore(base)
    mapping.set("vod_111.mp4", "Films/FR - Better Man (2024)/FR - Better Man (2024).strm",
                "http://stream/movie.mp4")
    mapping.set("ep_201.mp4", "Series/FR - Chernobyl/Season 01/FR - Chernobyl S01E01.strm",
                "http://stream/ep1.mp4")
    mapping.set("ep_202.mp4", "Series/FR - Chernobyl/Season 01/FR - Chernobyl S01E02.strm",
                "http://stream/ep2.mp4")
    mapping.save()

    return aid, base


@pytest.fixture
def seeded_db(db_factory, account_dir):
    """Insert account + media rows matching the on-disk fixture."""
    aid, _ = account_dir
    server_id = build_server_id(aid)

    async def _seed():
        async with db_factory() as db:
            db.add(XtreamAccount(
                id=aid, label="test", base_url="http://x", port=80,
                username="u", password="p", is_active=True, created_at=0,
            ))
            # Movie — DB title has year extracted out (matches sync_worker behavior)
            db.add(Media(
                rating_key="vod_111.mp4", server_id=server_id,
                library_section_id="xtream_vod", title="FR - Better Man",
                title_sortable="fr - better man", type="movie", year=2024,
                page_offset=0,
            ))
            # Series + 1 episode that uses grandparent_title="FR - Chernobyl"
            db.add(Media(
                rating_key="series_222", server_id=server_id,
                library_section_id="xtream_series", title="FR - Chernobyl",
                title_sortable="fr - chernobyl", type="show",
                page_offset=0,
            ))
            db.add(Media(
                rating_key="ep_201.mp4", server_id=server_id,
                library_section_id="xtream_series", title="Episode 1",
                title_sortable="episode 1", type="episode",
                grandparent_title="FR - Chernobyl",
                grandparent_rating_key="series_222",
                parent_index=1, index=1, page_offset=1,
            ))
            await db.commit()

    asyncio.run(_seed())
    return aid


# ───────────────────────── pure unit ─────────────────────────


class TestPlanRename:
    def test_movie_rename(self):
        # DB stores the title with year already extracted into the year column.
        r = mig._build_movie_rename("vod_1", "xtream_x", "FR - Foo", 2020)
        assert r is not None
        assert r.old_folder == "FR - Foo (2020)"
        assert r.new_folder == "Foo (2020)"
        assert r.old_rel_dir == "Films/FR - Foo (2020)"
        assert r.new_rel_dir == "Films/Foo (2020)"

    def test_movie_no_rename_when_clean(self):
        assert mig._build_movie_rename("vod_1", "xtream_x", "Foo", 2020) is None

    def test_series_rename(self):
        r = mig._build_series_rename("series_1", "xtream_x", "NF - Chernobyl")
        assert r is not None
        assert r.old_folder == "NF - Chernobyl"
        assert r.new_folder == "Chernobyl"

    def test_series_no_rename_when_clean(self):
        assert mig._build_series_rename("s_1", "xtream_x", "Chernobyl") is None

    def test_movie_with_quality_suffix(self):
        # Stored title may itself contain a quality suffix that older parse missed.
        r = mig._build_movie_rename("vod_1", "xtream_x", "FR - Aquaman LQ", 2023)
        assert r is not None
        assert r.new_folder == "Aquaman (2023)"


# ───────────────────────── filesystem ─────────────────────────


class TestApplyRenames:
    def test_movie_folder_and_strm_renamed_metadata_preserved(self, account_dir):
        aid, base = account_dir
        report = mig.MigrationReport()
        rename = mig._build_movie_rename(
            "vod_111.mp4", build_server_id(aid),
            "FR - Better Man", 2024,
        )
        mig.apply_movie_rename(rename, base, report)

        new_dir = base / "Films" / "Better Man (2024)"
        assert new_dir.is_dir()
        assert not (base / "Films" / "FR - Better Man (2024)").exists()
        assert (new_dir / "Better Man (2024).strm").read_text() == "http://stream/movie.mp4"
        # Metadata files preserved with same name AND same content
        assert (new_dir / "movie.nfo").read_text() == "<movie><title>Better Man</title></movie>"
        assert (new_dir / "poster.jpg").read_bytes() == b"POSTER_BYTES"
        assert (new_dir / "fanart.jpg").read_bytes() == b"FANART_BYTES"
        assert report.movies_renamed == 1

    def test_series_folder_and_episode_files_renamed(self, account_dir):
        aid, base = account_dir
        report = mig.MigrationReport()
        rename = mig._build_series_rename(
            "series_222", build_server_id(aid), "FR - Chernobyl",
        )
        mig.apply_series_rename(rename, base, report)

        new_dir = base / "Series" / "Chernobyl"
        season_dir = new_dir / "Season 01"
        assert new_dir.is_dir()
        assert not (base / "Series" / "FR - Chernobyl").exists()
        assert (season_dir / "Chernobyl S01E01.strm").exists()
        assert (season_dir / "Chernobyl S01E01.nfo").exists()
        assert (season_dir / "Chernobyl S01E02.strm").exists()
        # Series-level metadata preserved
        assert (new_dir / "tvshow.nfo").read_text() == "<tvshow><title>Chernobyl</title></tvshow>"
        assert (new_dir / "poster.jpg").read_bytes() == b"SERIES_POSTER"
        assert report.series_renamed == 1
        assert report.episode_files_renamed == 3

    def test_conflict_skipped(self, tmp_path):
        aid = "x"
        base = tmp_path / aid
        # Both the old AND a distinct new dir exist — must skip.
        (base / "Films" / "FR - Foo (2020)").mkdir(parents=True)
        (base / "Films" / "Foo (2020)").mkdir(parents=True)
        report = mig.MigrationReport()
        rename = mig._build_movie_rename("vod_1", "xtream_x", "FR - Foo", 2020)
        mig.apply_movie_rename(rename, base, report)
        assert report.conflicts_skipped == 1
        assert report.movies_renamed == 0
        assert (base / "Films" / "FR - Foo (2020)").exists()  # untouched


# ───────────────────────── mapping ─────────────────────────


class TestMappingUpdate:
    def test_paths_rewritten(self, account_dir):
        aid, base = account_dir
        mapping = MappingStore(base)
        mapping.load()
        renames = [
            mig._build_movie_rename("vod_111.mp4", build_server_id(aid),
                                    "FR - Better Man", 2024),
            mig._build_series_rename("series_222", build_server_id(aid),
                                     "FR - Chernobyl"),
        ]
        report = mig.MigrationReport()
        mig.update_mapping_paths(mapping, renames, report)

        assert mapping._data["vod_111.mp4"].path == \
            "Films/Better Man (2024)/Better Man (2024).strm"
        assert mapping._data["ep_201.mp4"].path == \
            "Series/Chernobyl/Season 01/Chernobyl S01E01.strm"
        assert mapping._data["ep_202.mp4"].path == \
            "Series/Chernobyl/Season 01/Chernobyl S01E02.strm"
        assert report.mapping_entries_updated == 3


# ───────────────────────── end-to-end ─────────────────────────


class TestRunMigrationForAccount:
    def test_full_migration(self, monkeypatch, db_factory, account_dir, seeded_db, tmp_path):
        aid, base = account_dir
        library_dir = tmp_path  # account_dir = tmp_path / aid

        report = asyncio.run(
            mig.run_migration_for_account(aid, library_dir, dry_run=False)
        )

        # Filesystem
        assert (base / "Films" / "Better Man (2024)" / "Better Man (2024).strm").exists()
        assert (base / "Films" / "Better Man (2024)" / "movie.nfo").exists()
        assert (base / "Films" / "Better Man (2024)" / "poster.jpg").read_bytes() == b"POSTER_BYTES"
        assert not (base / "Films" / "FR - Better Man (2024)").exists()
        assert (base / "Series" / "Chernobyl" / "Season 01" / "Chernobyl S01E01.strm").exists()
        assert (base / "Series" / "Chernobyl" / "tvshow.nfo").exists()
        assert not (base / "Series" / "FR - Chernobyl").exists()

        # Mapping
        mapping = MappingStore(base)
        mapping.load()
        assert "FR - " not in mapping._data["vod_111.mp4"].path
        assert "FR - " not in mapping._data["ep_201.mp4"].path

        # DB
        async def _check_db():
            async with db_factory() as db:
                rows = await db.execute(select(Media.rating_key, Media.title, Media.grandparent_title))
                by_key = {rk: (t, gp) for rk, t, gp in rows.all()}
            return by_key

        by_key = asyncio.run(_check_db())
        assert by_key["vod_111.mp4"][0] == "Better Man"
        assert by_key["series_222"][0] == "Chernobyl"
        assert by_key["ep_201.mp4"][1] == "Chernobyl"  # grandparent_title cleaned

        # EnrichmentQueue
        async def _check_queue():
            async with db_factory() as db:
                rows = await db.execute(select(
                    EnrichmentQueue.rating_key, EnrichmentQueue.status,
                    EnrichmentQueue.title, EnrichmentQueue.attempts,
                ))
                return list(rows.all())

        queue = asyncio.run(_check_queue())
        keys = {rk for rk, *_ in queue}
        assert "vod_111.mp4" in keys
        assert "series_222" in keys
        for rk, status, title, attempts in queue:
            assert status == "pending"
            assert attempts == 0
            assert "FR - " not in title

        assert report.movies_renamed == 1
        assert report.series_renamed == 1
        assert report.db_rows_updated >= 3  # movie + show + episode grandparent

    def test_idempotent_second_run_noop(self, db_factory, account_dir, seeded_db, tmp_path):
        aid, _ = account_dir
        library_dir = tmp_path

        asyncio.run(mig.run_migration_for_account(aid, library_dir, dry_run=False))
        report2 = asyncio.run(mig.run_migration_for_account(aid, library_dir, dry_run=False))

        assert report2.movies_renamed == 0
        assert report2.series_renamed == 0
        assert report2.db_rows_updated == 0

    def test_dry_run_no_changes(self, db_factory, account_dir, seeded_db, tmp_path):
        aid, base = account_dir
        library_dir = tmp_path

        report = asyncio.run(mig.run_migration_for_account(aid, library_dir, dry_run=True))

        # Nothing on disk renamed
        assert (base / "Films" / "FR - Better Man (2024)").exists()
        assert (base / "Series" / "FR - Chernobyl").exists()
        # No DB updates
        assert report.movies_renamed == 0
        assert report.db_rows_updated == 0
        # Mapping unchanged
        mapping = MappingStore(base)
        mapping.load()
        assert "FR - " in mapping._data["vod_111.mp4"].path


class TestResolveDisambiguators:
    def test_singleton_no_decoration(self):
        from app.scripts.strip_titles_pollution import _resolve_disambiguators
        result = _resolve_disambiguators([("vod_1", "Les Experts (2000)", 2000)])
        assert result["vod_1"] == (None, None)

    def test_us_vs_hd_keep_distinct(self):
        from app.scripts.strip_titles_pollution import _resolve_disambiguators
        result = _resolve_disambiguators([
            ("vod_a", "Les Experts (2000) (US)", 2000),
            ("vod_b", "Les Experts (2000) (HD)", 2000),
        ])
        assert result["vod_a"] == ("US", None)
        assert result["vod_b"] == ("HD", None)

    def test_collision_no_suffix_uses_fallback(self):
        from app.scripts.strip_titles_pollution import _resolve_disambiguators
        result = _resolve_disambiguators([
            ("vod_1.mkv", "Les Experts (2000)", 2000),
            ("vod_2.mkv", "Les Experts (2000)", 2000),
        ])
        assert result["vod_1.mkv"] == (None, "vod_1")
        assert result["vod_2.mkv"] == (None, "vod_2")


class TestMigrationWithSuffix:
    def test_les_experts_us_singleton_strips_suffix(self):
        """When only one 'Les Experts (2000) (US)' exists, drop the (US)."""
        from app.scripts.strip_titles_pollution import _build_movie_rename
        r = _build_movie_rename(
            "vod_1", "xtream_x", "Les Experts (2000) (US)", 2000,
            suffix=None, fallback_id=None,
        )
        assert r is not None
        assert r.old_folder == "Les Experts (2000) (US) (2000)"  # naive concat of original
        assert r.new_folder == "Les Experts (2000)"

    def test_les_experts_us_collision_keeps_suffix(self):
        """When the resolver decides to keep (US), the new folder retains it."""
        from app.scripts.strip_titles_pollution import _build_movie_rename
        r = _build_movie_rename(
            "vod_1", "xtream_x", "Les Experts (2000) (US)", 2000,
            suffix="US", fallback_id=None,
        )
        assert r is not None
        assert r.new_folder == "Les Experts (2000) (US)"


class TestEpisodeTitleCleanup:
    """Episodes can have FR- pollution on title, parent_title and grandparent_title.
    All three must be cleaned, not just grandparent_title (regression #ep-pollution)."""

    def test_episode_title_and_parent_cleaned(self, db_factory, tmp_path):
        from app.models.database import Media, XtreamAccount
        from app.scripts.strip_titles_pollution import run_migration_for_account
        from app.utils.server_id import build_server_id

        aid = "epacc"
        server_id = build_server_id(aid)

        async def _seed():
            async with db_factory() as session:
                session.add(XtreamAccount(
                    id=aid, label="t", base_url="http://x", port=80,
                    username="u", password="p", is_active=True, created_at=0,
                ))
                # Show + 1 polluted episode
                session.add(Media(
                    rating_key="series_1", server_id=server_id,
                    library_section_id="lib", title="Foo", title_sortable="foo",
                    type="show", page_offset=0,
                ))
                session.add(Media(
                    rating_key="ep_1", server_id=server_id,
                    library_section_id="lib",
                    title="FR - Episode 1",
                    title_sortable="fr - episode 1",
                    type="episode",
                    parent_title="FR - Season 1",
                    grandparent_title="FR - Foo",
                    grandparent_rating_key="series_1",
                    parent_index=1, index=1, page_offset=1,
                ))
                await session.commit()

        asyncio.run(_seed())
        # Account dir doesn't even need to exist on disk for this DB-only check.
        asyncio.run(run_migration_for_account(aid, tmp_path, dry_run=False))

        async def _check():
            async with db_factory() as session:
                result = await session.execute(select(
                    Media.title, Media.parent_title, Media.grandparent_title,
                ).where(Media.rating_key == "ep_1", Media.server_id == server_id))
                return result.first()

        title, parent_title, gp_title = asyncio.run(_check())
        assert title == "Episode 1"
        assert parent_title == "Season 1"
        assert gp_title == "Foo"
