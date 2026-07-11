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
    await _migration_013_add_media_is_adult(engine)
    await _migration_014_add_nfo_metadata(engine)
    await _migration_015_add_missing_media_indexes(engine)

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


async def _migration_013_add_media_is_adult(engine: AsyncEngine) -> None:
    """Add is_adult flag to media table (adult/X-rated tagging).

    Set per-sync by category_service.update_media_adult_flags based on the
    Xtream category name/id. Idempotent: ADD COLUMN guarded by try/except.
    """
    logger.info("Migration 013: Adding is_adult to media")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                ALTER TABLE media
                ADD COLUMN is_adult INTEGER NOT NULL DEFAULT 0
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_media_adult
                ON media(is_adult)
            """))

            logger.info("Migration 013: is_adult column added")
        except Exception as e:
            logger.warning(f"Migration 013: Column may already exist: {e}")


async def _migration_014_add_nfo_metadata(engine: AsyncEngine) -> None:
    """Add tinyMediaManager NFO metadata columns to the media table.

    Populated only by services/nfo_import_service (never by the Xtream sync):
    - identity / descriptive: original_title, tagline, premiered, status,
      studio, country
    - external IDs: tvdb_id, wikidata_id
    - per-source ratings + vote counts: imdb_rating/imdb_votes,
      tmdb_rating/tmdb_votes (feed the IMDb/TMDb badges)
    - structured cast: cast_json (the legacy `cast` CSV is kept untouched)

    Idempotent: each ADD COLUMN is guarded individually so a column already
    present (fresh DB via create_all) can't abort the others.
    """
    logger.info("Migration 014: Adding NFO metadata columns to media")

    columns = [
        ("original_title", "TEXT"),
        ("tagline", "TEXT"),
        ("premiered", "TEXT"),
        ("status", "TEXT"),
        ("studio", "TEXT"),
        ("country", "TEXT"),
        ("tvdb_id", "TEXT"),
        ("wikidata_id", "TEXT"),
        ("imdb_rating", "REAL"),
        ("imdb_votes", "INTEGER"),
        ("tmdb_rating", "REAL"),
        ("tmdb_votes", "INTEGER"),
        ("cast_json", "TEXT"),
    ]

    for name, sql_type in columns:
        async with engine.begin() as conn:
            try:
                await conn.execute(text(
                    f"ALTER TABLE media ADD COLUMN {name} {sql_type}"
                ))
                logger.info("Migration 014: %s column added", name)
            except Exception as e:
                logger.warning("Migration 014: %s may already exist: %s", name, e)

    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_media_tvdb ON media(tvdb_id)"
            ))
            logger.info("Migration 014: ix_media_tvdb index created")
        except Exception as e:
            logger.warning("Migration 014: index may already exist: %s", e)


async def _migration_015_add_missing_media_indexes(engine: AsyncEngine) -> None:
    """Backfill the media indexes declared on the ORM model (CR-P02).

    On a FRESH database, ``Base.metadata.create_all`` (db/database.py:92)
    creates EVERY ``Index(...)`` declared on ``Media`` (models/database.py)
    in one shot. But this hand-rolled migration chain only ever issued
    ``CREATE INDEX`` for three of them (ix_media_category_visible/003,
    ix_media_adult/013, ix_media_tvdb/014) — any database that existed
    before those migrations landed silently lacks the rest, causing full
    scans / filesorts on the hot list/sort/filter queries (invisible on a
    fresh DB because create_all builds them all).

    This migration creates every remaining ORM-declared index, with the
    exact same name and column tuple as ``Media.__table_args__``, so
    create_all (fresh DB) and this chain (upgraded DB) converge on the
    identical index set — no divergence, no duplicate-name conflict.

    Idempotent: every statement uses ``IF NOT EXISTS`` and is individually
    guarded so one failure can't block the others.
    """
    logger.info("Migration 015: Backfilling missing media indexes")

    # Non-unique indexes: safe to (re)create even if the table already
    # holds rows that would violate a uniqueness constraint.
    index_statements = [
        ("ix_media_guid", "CREATE INDEX IF NOT EXISTS ix_media_guid ON media(guid)"),
        ("ix_media_type_added", "CREATE INDEX IF NOT EXISTS ix_media_type_added ON media(type, added_at)"),
        ("ix_media_imdb", "CREATE INDEX IF NOT EXISTS ix_media_imdb ON media(imdb_id)"),
        ("ix_media_tmdb", "CREATE INDEX IF NOT EXISTS ix_media_tmdb ON media(tmdb_id)"),
        ("ix_media_server_lib", "CREATE INDEX IF NOT EXISTS ix_media_server_lib ON media(server_id, library_section_id)"),
        ("ix_media_unification", "CREATE INDEX IF NOT EXISTS ix_media_unification ON media(unification_id)"),
        ("ix_media_type_rating", "CREATE INDEX IF NOT EXISTS ix_media_type_rating ON media(type, display_rating)"),
        ("ix_media_parent", "CREATE INDEX IF NOT EXISTS ix_media_parent ON media(parent_rating_key)"),
        ("ix_media_title_sort", "CREATE INDEX IF NOT EXISTS ix_media_title_sort ON media(title_sortable)"),
        ("ix_media_broken", "CREATE INDEX IF NOT EXISTS ix_media_broken ON media(is_broken)"),
        ("ix_media_updated", "CREATE INDEX IF NOT EXISTS ix_media_updated ON media(updated_at)"),
        ("ix_media_server_type", "CREATE INDEX IF NOT EXISTS ix_media_server_type ON media(server_id, type)"),
        ("ix_media_server_visible", "CREATE INDEX IF NOT EXISTS ix_media_server_visible ON media(server_id, is_in_allowed_categories)"),
        ("ix_media_parent_visible", "CREATE INDEX IF NOT EXISTS ix_media_parent_visible ON media(parent_rating_key, is_in_allowed_categories)"),
        ("ix_media_grandparent", "CREATE INDEX IF NOT EXISTS ix_media_grandparent ON media(grandparent_rating_key)"),
    ]

    for name, stmt in index_statements:
        async with engine.begin() as conn:
            try:
                await conn.execute(text(stmt))
                logger.info("Migration 015: %s index created", name)
            except Exception as e:
                logger.warning("Migration 015: %s may already exist: %s", name, e)

    # uix_media_pagination is UNIQUE: on an upgraded DB that never enforced
    # it, pre-existing duplicate (server_id, library_section_id, filter,
    # sort_order, page_offset) rows would make CREATE UNIQUE INDEX fail.
    # Isolated in its own transaction/try so that can't take down the
    # (non-unique) indexes created above.
    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uix_media_pagination "
                "ON media(server_id, library_section_id, filter, sort_order, page_offset)"
            ))
            logger.info("Migration 015: uix_media_pagination index created")
        except Exception as e:
            logger.warning(
                "Migration 015: uix_media_pagination not created (likely duplicate "
                "pagination rows on an upgraded DB, needs manual dedup): %s", e
            )


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
