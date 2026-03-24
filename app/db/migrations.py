"""
Database migrations for PlexHub Backend.
"""
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


async def run_migrations(engine: AsyncEngine) -> None:
    """
    Run all database migrations in order.

    Args:
        engine: SQLAlchemy async engine
    """
    logger.info("Running database migrations...")

    await _migration_001_add_xtream_categories(engine)
    await _migration_002_add_category_filter_mode(engine)
    await _migration_003_add_media_category_visibility(engine)
    await _migration_004_add_enrichment_existing_ids(engine)
    await _migration_005_add_media_cast(engine)
    await _migration_006_create_live_tables(engine)

    logger.info("All migrations completed successfully")


async def _migration_001_add_xtream_categories(engine: AsyncEngine) -> None:
    """Create xtream_categories table."""
    logger.info("Migration 001: Creating xtream_categories table")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS xtream_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    category_type TEXT NOT NULL,
                    category_name TEXT NOT NULL,
                    is_allowed INTEGER NOT NULL DEFAULT 1,
                    last_fetched_at INTEGER NOT NULL,
                    UNIQUE(account_id, category_id, category_type)
                )
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_xtream_categories_account
                ON xtream_categories(account_id)
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_xtream_categories_type
                ON xtream_categories(category_type)
            """))

            logger.info("Migration 001: xtream_categories table created")
        except Exception as e:
            logger.warning(f"Migration 001: Table may already exist: {e}")


async def _migration_002_add_category_filter_mode(engine: AsyncEngine) -> None:
    """Add category_filter_mode to xtream_accounts table."""
    logger.info("Migration 002: Adding category_filter_mode to xtream_accounts")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                ALTER TABLE xtream_accounts
                ADD COLUMN category_filter_mode TEXT NOT NULL DEFAULT 'all'
            """))
            logger.info("Migration 002: category_filter_mode column added")
        except Exception as e:
            logger.warning(f"Migration 002: Column may already exist: {e}")


async def _migration_003_add_media_category_visibility(engine: AsyncEngine) -> None:
    """Add is_in_allowed_categories to media table."""
    logger.info("Migration 003: Adding is_in_allowed_categories to media")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                ALTER TABLE media
                ADD COLUMN is_in_allowed_categories INTEGER NOT NULL DEFAULT 1
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_media_category_visible
                ON media(is_in_allowed_categories)
            """))

            logger.info("Migration 003: is_in_allowed_categories column added")
        except Exception as e:
            logger.warning(f"Migration 003: Column may already exist: {e}")


async def _migration_004_add_enrichment_existing_ids(engine: AsyncEngine) -> None:
    """Add existing_tmdb_id and existing_imdb_id to enrichment_queue table."""
    logger.info("Migration 004: Adding existing ID columns to enrichment_queue")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                ALTER TABLE enrichment_queue
                ADD COLUMN existing_tmdb_id TEXT
            """))
            logger.info("Migration 004: existing_tmdb_id column added")
        except Exception as e:
            logger.warning(f"Migration 004: existing_tmdb_id may already exist: {e}")

        try:
            await conn.execute(text("""
                ALTER TABLE enrichment_queue
                ADD COLUMN existing_imdb_id TEXT
            """))
            logger.info("Migration 004: existing_imdb_id column added")
        except Exception as e:
            logger.warning(f"Migration 004: existing_imdb_id may already exist: {e}")


async def _migration_005_add_media_cast(engine: AsyncEngine) -> None:
    """Add cast column to media table."""
    logger.info("Migration 005: Adding cast to media")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                ALTER TABLE media
                ADD COLUMN "cast" TEXT
            """))
            logger.info("Migration 005: cast column added")
        except Exception as e:
            logger.warning(f"Migration 005: Column may already exist: {e}")


async def _migration_006_create_live_tables(engine: AsyncEngine) -> None:
    """Create live_channels and epg_entries tables for Live IPTV support."""
    logger.info("Migration 006: Creating live_channels and epg_entries tables")

    async with engine.begin() as conn:
        # --- live_channels ---
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS live_channels (
                    stream_id INTEGER NOT NULL,
                    server_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    name_sortable TEXT NOT NULL DEFAULT '',
                    stream_icon TEXT,
                    epg_channel_id TEXT,
                    category_id TEXT,
                    container_extension TEXT DEFAULT 'ts',
                    custom_sid TEXT,
                    tv_archive INTEGER NOT NULL DEFAULT 0,
                    tv_archive_duration INTEGER NOT NULL DEFAULT 0,
                    is_adult INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    is_in_allowed_categories INTEGER NOT NULL DEFAULT 1,
                    added_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    dto_hash TEXT,
                    PRIMARY KEY (stream_id, server_id)
                )
            """))

            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS ix_live_channels_server ON live_channels(server_id)",
                "CREATE INDEX IF NOT EXISTS ix_live_channels_category ON live_channels(category_id)",
                "CREATE INDEX IF NOT EXISTS ix_live_channels_epg ON live_channels(epg_channel_id)",
                "CREATE INDEX IF NOT EXISTS ix_live_channels_name ON live_channels(name_sortable)",
                "CREATE INDEX IF NOT EXISTS ix_live_channels_visible ON live_channels(is_in_allowed_categories)",
            ]:
                await conn.execute(text(idx_sql))

            logger.info("Migration 006: live_channels table created")
        except Exception as e:
            logger.warning(f"Migration 006: live_channels may already exist: {e}")

        # --- epg_entries ---
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS epg_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT NOT NULL,
                    epg_channel_id TEXT NOT NULL,
                    stream_id INTEGER,
                    title TEXT NOT NULL,
                    description TEXT,
                    start_time INTEGER NOT NULL,
                    end_time INTEGER NOT NULL,
                    lang TEXT,
                    fetched_at INTEGER NOT NULL DEFAULT 0
                )
            """))

            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS ix_epg_channel ON epg_entries(epg_channel_id)",
                "CREATE INDEX IF NOT EXISTS ix_epg_server ON epg_entries(server_id)",
                "CREATE INDEX IF NOT EXISTS ix_epg_time ON epg_entries(start_time, end_time)",
                "CREATE INDEX IF NOT EXISTS ix_epg_stream ON epg_entries(stream_id)",
            ]:
                await conn.execute(text(idx_sql))

            logger.info("Migration 006: epg_entries table created")
        except Exception as e:
            logger.warning(f"Migration 006: epg_entries may already exist: {e}")
