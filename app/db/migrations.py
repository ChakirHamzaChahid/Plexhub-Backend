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
    await _migration_007_add_stream_validation_index(engine)

    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)

    await _migration_009_create_tv_auth_sessions(engine)
    await _migration_010_scrape_cache(engine)
    await _migration_011_create_subtitle_cache(engine)
    await _migration_012_create_media_blurb(engine)

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


async def _migration_007_add_stream_validation_index(engine: AsyncEngine) -> None:
    """Add compound index for pipeline stream validation query performance."""
    logger.info("Migration 007: Adding stream validation index")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_media_stream_validation
                ON media(server_id, type, is_in_allowed_categories, last_stream_check)
            """))
            logger.info("Migration 007: stream validation index created")
        except Exception as e:
            logger.warning(f"Migration 007: Index may already exist: {e}")


async def _migration_009_create_tv_auth_sessions(engine: AsyncEngine) -> None:
    """Create tv_auth_sessions table for device-flow TV pairing (Mission 18).

    Sessions are short-lived (TTL 15 min), single-use, and the sensitive
    payload is encrypted at rest (Fernet). Idempotent: IF NOT EXISTS everywhere.
    """
    logger.info("Migration 009: Creating tv_auth_sessions table")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS tv_auth_sessions (
                    id TEXT PRIMARY KEY,
                    device_code TEXT NOT NULL,
                    user_code TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    payload_encrypted TEXT,
                    payload_delivered INTEGER NOT NULL DEFAULT 0,
                    device_name TEXT,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    approved_at INTEGER,
                    completed_at INTEGER
                )
            """))

            for idx_sql in [
                "CREATE UNIQUE INDEX IF NOT EXISTS uix_tv_auth_device_code ON tv_auth_sessions(device_code)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uix_tv_auth_user_code ON tv_auth_sessions(user_code)",
                "CREATE INDEX IF NOT EXISTS ix_tv_auth_expires ON tv_auth_sessions(expires_at)",
                "CREATE INDEX IF NOT EXISTS ix_tv_auth_status ON tv_auth_sessions(status)",
            ]:
                await conn.execute(text(idx_sql))

            logger.info("Migration 009: tv_auth_sessions table created")
        except Exception as e:
            logger.warning(f"Migration 009: Table may already exist: {e}")


async def _migration_010_scrape_cache(engine: AsyncEngine) -> None:
    """Persistent TMDB scrape cache + existing_summary on enrichment_queue.

    The scrape cache lets enrichment reuse a previous TMDB resolution for the
    same (media_type, normalized title, year) — across accounts AND restarts —
    so we never re-query TMDB for the same film/series. existing_summary carries
    the Xtream plot into the matcher for the summary-based tie-break.
    """
    logger.info("Migration 010: scrape cache + enrichment summary")

    # Separate transactions so a guarded ADD COLUMN failure (column already
    # present on a fresh DB created via create_all) can't abort the CREATE TABLE.
    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                ALTER TABLE enrichment_queue ADD COLUMN existing_summary TEXT
            """))
            logger.info("Migration 010: existing_summary column added")
        except Exception as e:
            logger.warning(f"Migration 010: existing_summary may already exist: {e}")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS tmdb_scrape_cache (
                    cache_key TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    result TEXT NOT NULL,
                    tmdb_id TEXT,
                    imdb_id TEXT,
                    confidence REAL,
                    payload TEXT,
                    fetched_at INTEGER NOT NULL
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_scrape_cache_fetched_at "
                "ON tmdb_scrape_cache(fetched_at)"
            ))
            logger.info("Migration 010: tmdb_scrape_cache table created")
        except Exception as e:
            logger.warning(f"Migration 010: Table may already exist: {e}")


async def _migration_011_create_subtitle_cache(engine: AsyncEngine) -> None:
    """Create ai_subtitle_cache table for AI subtitle translation (WP3).

    Caches translated subtitle content keyed by a deterministic hash of the
    source material so repeated translation requests are served from cache.
    Idempotent: every DDL uses IF NOT EXISTS.
    """
    logger.info("Migration 011: Creating ai_subtitle_cache table")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ai_subtitle_cache (
                    cache_key          TEXT PRIMARY KEY,
                    target_lang        TEXT    NOT NULL,
                    model              TEXT    NOT NULL,
                    source_format      TEXT    NOT NULL,
                    cue_count          INTEGER NOT NULL,
                    translated_content TEXT    NOT NULL,
                    created_at         INTEGER NOT NULL
                )
            """))

            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS ix_subtitle_cache_lang ON ai_subtitle_cache(target_lang)",
                "CREATE INDEX IF NOT EXISTS ix_subtitle_cache_created ON ai_subtitle_cache(created_at)",
            ]:
                await conn.execute(text(idx_sql))

            logger.info("Migration 011: ai_subtitle_cache table created")
        except Exception as e:
            logger.warning(f"Migration 011: Table may already exist: {e}")


async def _migration_012_create_media_blurb(engine: AsyncEngine) -> None:
    """Create ai_media_blurb table for AI-generated French synopsis + mood tags (F3).

    Keyed on (tmdb_id, media_type, lang) so the same title can have blurbs for
    multiple languages.  tags is stored as a JSON array string.
    Idempotent: every DDL uses IF NOT EXISTS.
    """
    logger.info("Migration 012: Creating ai_media_blurb table")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ai_media_blurb (
                    tmdb_id    INTEGER NOT NULL,
                    media_type TEXT    NOT NULL,
                    lang       TEXT    NOT NULL,
                    summary    TEXT    NOT NULL,
                    tags       TEXT    NOT NULL,
                    model      TEXT    NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (tmdb_id, media_type, lang)
                )
            """))

            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS ix_media_blurb_tmdb ON ai_media_blurb(tmdb_id)",
                "CREATE INDEX IF NOT EXISTS ix_media_blurb_lang ON ai_media_blurb(lang)",
            ]:
                await conn.execute(text(idx_sql))

            logger.info("Migration 012: ai_media_blurb table created")
        except Exception as e:
            logger.warning(f"Migration 012: Table may already exist: {e}")


async def _migration_008_ai_embeddings(conn) -> None:
    """Create ai_embeddings (sqlite-vec virtual table) and ai_tmdb_cache.

    Operates on a connection (not engine) so it can be reused by isolated
    test fixtures. Idempotent: every DDL uses IF NOT EXISTS.
    """
    logger.info("Migration 008: Creating AI embeddings tables")

    statements = [
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS ai_embeddings USING vec0(
            tmdb_id INTEGER PRIMARY KEY,
            embedding FLOAT[384]
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ai_tmdb_cache (
            tmdb_id INTEGER PRIMARY KEY,
            imdb_id TEXT,
            media_type TEXT NOT NULL CHECK(media_type IN ('movie','tv')),
            title TEXT,
            overview TEXT,
            genres TEXT,
            fetched_at INTEGER NOT NULL,
            embedded_at INTEGER
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_ai_tmdb_cache_imdb_id ON ai_tmdb_cache(imdb_id)",
        "CREATE INDEX IF NOT EXISTS ix_ai_tmdb_cache_embedded_at ON ai_tmdb_cache(embedded_at)",
    ]

    for stmt in statements:
        await conn.execute(text(stmt))

    logger.info("Migration 008: AI embeddings tables created")
