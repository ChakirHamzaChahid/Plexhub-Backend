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
