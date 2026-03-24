"""Tests for DB layer: upsert batches, media service search, enrichment queue."""
import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.models.database import Base, Media, EnrichmentQueue, LiveChannel, EpgEntry


@pytest.fixture
def db_session():
    """Create an in-memory SQLite DB with all tables for testing."""
    async def _setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        return session_factory, engine

    factory, engine = asyncio.run(_setup())

    async def _get_session():
        async with factory() as session:
            yield session

    # Return factory for tests to use
    return factory


class TestMediaUpsert:
    """Test the upsert_media_batch function with real SQLite."""

    def test_insert_new_media(self, db_session):
        async def _test():
            from app.workers.sync_worker import upsert_media_batch
            async with db_session() as db:
                rows = [{
                    "rating_key": "vod_123",
                    "server_id": "xtream_test",
                    "filter": "all",
                    "sort_order": "default",
                    "library_section_id": "xtream_vod",
                    "title": "Test Movie",
                    "title_sortable": "test movie",
                    "page_offset": 0,
                    "type": "movie",
                    "added_at": 1000,
                    "updated_at": 1000,
                }]
                await upsert_media_batch(db, rows)
                await db.commit()

                result = await db.execute(
                    select(Media).where(Media.rating_key == "vod_123")
                )
                item = result.scalars().first()
                assert item is not None
                assert item.title == "Test Movie"

        asyncio.run(_test())

    def test_upsert_updates_existing(self, db_session):
        async def _test():
            from app.workers.sync_worker import upsert_media_batch
            async with db_session() as db:
                row = {
                    "rating_key": "vod_456",
                    "server_id": "xtream_test",
                    "filter": "all",
                    "sort_order": "default",
                    "library_section_id": "xtream_vod",
                    "title": "Old Title",
                    "title_sortable": "old title",
                    "page_offset": 0,
                    "type": "movie",
                    "added_at": 1000,
                    "updated_at": 1000,
                    "content_hash": "hash1",
                }
                await upsert_media_batch(db, [row])
                await db.commit()

                # Update with different hash
                row_updated = {**row, "title": "New Title", "title_sortable": "new title",
                               "content_hash": "hash2", "updated_at": 2000}
                await upsert_media_batch(db, [row_updated])
                await db.commit()

                result = await db.execute(
                    select(Media).where(Media.rating_key == "vod_456")
                )
                item = result.scalars().first()
                assert item.title == "New Title"

        asyncio.run(_test())

    def test_upsert_skip_unchanged(self, db_session):
        async def _test():
            from app.workers.sync_worker import upsert_media_batch
            async with db_session() as db:
                row = {
                    "rating_key": "vod_789",
                    "server_id": "xtream_test",
                    "filter": "all",
                    "sort_order": "default",
                    "library_section_id": "xtream_vod",
                    "title": "Same Title",
                    "title_sortable": "same title",
                    "page_offset": 0,
                    "type": "movie",
                    "added_at": 1000,
                    "updated_at": 1000,
                    "content_hash": "same_hash",
                }
                await upsert_media_batch(db, [row])
                await db.commit()

                # Same hash — should not update
                await upsert_media_batch(db, [row])
                await db.commit()

                result = await db.execute(
                    select(Media).where(Media.rating_key == "vod_789")
                )
                item = result.scalars().first()
                assert item.title == "Same Title"

        asyncio.run(_test())


class TestMediaServiceSearch:
    """Test search/filter functionality in media_service."""

    def _seed_data(self, db_session):
        """Insert test media data."""
        async def _seed():
            async with db_session() as db:
                for i, (title, genre, year) in enumerate([
                    ("The Matrix", "Action, Sci-Fi", 1999),
                    ("The Matrix Reloaded", "Action, Sci-Fi", 2003),
                    ("Inception", "Sci-Fi, Thriller", 2010),
                    ("The Godfather", "Crime, Drama", 1972),
                    ("Pulp Fiction", "Crime, Drama", 1994),
                ]):
                    db.add(Media(
                        rating_key=f"vod_{i}",
                        server_id="xtream_test",
                        filter="all",
                        sort_order="default",
                        library_section_id="xtream_vod",
                        title=title,
                        title_sortable=title.lower(),
                        page_offset=i,
                        type="movie",
                        genres=genre,
                        year=year,
                        added_at=1000 + i,
                        updated_at=1000 + i,
                        is_in_allowed_categories=True,
                    ))
                await db.commit()
        asyncio.run(_seed())

    def test_search_by_title(self, db_session):
        self._seed_data(db_session)

        async def _test():
            from app.services.media_service import MediaService
            svc = MediaService()
            async with db_session() as db:
                items, total = await svc.get_media_list(
                    db, media_type="movie", search="Matrix"
                )
                assert total == 2
                assert all("Matrix" in i.title for i in items)

        asyncio.run(_test())

    def test_filter_by_genre(self, db_session):
        self._seed_data(db_session)

        async def _test():
            from app.services.media_service import MediaService
            svc = MediaService()
            async with db_session() as db:
                items, total = await svc.get_media_list(
                    db, media_type="movie", genre="Crime"
                )
                assert total == 2
                assert all("Crime" in i.genres for i in items)

        asyncio.run(_test())

    def test_filter_by_year(self, db_session):
        self._seed_data(db_session)

        async def _test():
            from app.services.media_service import MediaService
            svc = MediaService()
            async with db_session() as db:
                items, total = await svc.get_media_list(
                    db, media_type="movie", year=1999
                )
                assert total == 1
                assert items[0].title == "The Matrix"

        asyncio.run(_test())

    def test_combined_search_and_genre(self, db_session):
        self._seed_data(db_session)

        async def _test():
            from app.services.media_service import MediaService
            svc = MediaService()
            async with db_session() as db:
                items, total = await svc.get_media_list(
                    db, media_type="movie", search="The", genre="Crime"
                )
                assert total == 1
                assert items[0].title == "The Godfather"

        asyncio.run(_test())

    def test_search_no_results(self, db_session):
        self._seed_data(db_session)

        async def _test():
            from app.services.media_service import MediaService
            svc = MediaService()
            async with db_session() as db:
                items, total = await svc.get_media_list(
                    db, media_type="movie", search="Nonexistent"
                )
                assert total == 0
                assert items == []

        asyncio.run(_test())

    def test_sort_by_year_desc(self, db_session):
        self._seed_data(db_session)

        async def _test():
            from app.services.media_service import MediaService
            svc = MediaService()
            async with db_session() as db:
                items, total = await svc.get_media_list(
                    db, media_type="movie", sort="year_desc"
                )
                years = [i.year for i in items]
                assert years == sorted(years, reverse=True)

        asyncio.run(_test())


class TestEnrichmentQueue:
    """Test enrichment queue query with retry logic."""

    def test_pending_and_retryable_skipped(self, db_session):
        async def _test():
            from sqlalchemy import or_
            async with db_session() as db:
                # Add items in various states
                for i, (status, attempts) in enumerate([
                    ("pending", 0),      # should be selected
                    ("skipped", 1),      # should be selected (attempts < 3)
                    ("skipped", 3),      # should NOT be selected (max attempts)
                    ("done", 0),         # should NOT be selected
                ]):
                    db.add(EnrichmentQueue(
                        rating_key=f"vod_{i}",
                        server_id="xtream_test",
                        media_type="movie",
                        title=f"Movie {i}",
                        status=status,
                        attempts=attempts,
                        created_at=1000 + i,
                    ))
                await db.commit()

                MAX_ATTEMPTS = 3
                result = await db.execute(
                    select(EnrichmentQueue)
                    .where(
                        or_(
                            EnrichmentQueue.status == "pending",
                            (EnrichmentQueue.status == "skipped") & (EnrichmentQueue.attempts < MAX_ATTEMPTS),
                        ),
                        EnrichmentQueue.media_type == "movie",
                    )
                )
                items = list(result.scalars().all())
                assert len(items) == 2
                keys = {i.rating_key for i in items}
                assert "vod_0" in keys  # pending
                assert "vod_1" in keys  # skipped, attempts=1

        asyncio.run(_test())
