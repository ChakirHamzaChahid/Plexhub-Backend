"""
Database migrations for PlexHub Backend.
"""
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.utils.crypto_fields import get_xtream_fernet, looks_encrypted

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

    # 008 runs on its own dedicated connection/transaction (not the shared
    # `engine` helper used by every other migration here) because it needs
    # sqlite-vec loaded on that specific connection to create the vec0
    # virtual table — see _migration_008_ai_embeddings' docstring (CR-C10).
    async with engine.begin() as conn:
        await _migration_008_ai_embeddings(conn)

    await _migration_009_create_tv_auth_sessions(engine)
    await _migration_010_scrape_cache(engine)
    await _migration_011_create_subtitle_cache(engine)
    await _migration_012_create_media_blurb(engine)
    await _migration_013_add_media_is_adult(engine)
    await _migration_014_add_nfo_metadata(engine)
    await _migration_015_add_missing_media_indexes(engine)
    await _migration_016_encrypt_xtream_passwords(engine)
    await _migration_017_create_media_group_snapshot(engine)
    await _migration_018_create_download_tables(engine)
    await _migration_019_create_plex_tables(engine)
    await _migration_020_add_media_file_size(engine)
    await _migration_021_add_plex_media_item_genres(engine)
    await _migration_022_create_omdb_scrape_cache(engine)

    logger.info("All migrations completed successfully")


async def _column_exists(conn, table: str, column: str) -> bool:
    """Return True if ``column`` is already present on ``table`` (SQLite).

    CR-C05: ``Base.metadata.create_all`` (db/database.py:92) creates every
    ORM-declared column on a FRESH database *before* ``run_migrations()``
    runs, so a plain ``ALTER TABLE ... ADD COLUMN`` for that same column
    always raised "duplicate column name" there — caught by the per-column
    try/except, but logged as a WARNING on every cold start, masking real
    migration failures. Probing via ``PRAGMA table_info`` first turns the
    fresh-DB case into a silent no-op while an upgraded DB (column genuinely
    missing) still gets the ADD COLUMN. The try/except around the ADD
    COLUMN itself is kept as a safety net for a race with another process
    (``init_db()`` runs in every worker, cf. CLAUDE.md piège 7).
    """
    rows = (await conn.execute(text(f'PRAGMA table_info("{table}")'))).fetchall()
    return any(row[1] == column for row in rows)


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
        if await _column_exists(conn, "xtream_accounts", "category_filter_mode"):
            logger.debug("Migration 002: category_filter_mode already present, skipping ADD COLUMN")
            return
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
        if await _column_exists(conn, "media", "is_in_allowed_categories"):
            logger.debug("Migration 003: is_in_allowed_categories already present, skipping ADD COLUMN")
        else:
            try:
                await conn.execute(text("""
                    ALTER TABLE media
                    ADD COLUMN is_in_allowed_categories INTEGER NOT NULL DEFAULT 1
                """))
                logger.info("Migration 003: is_in_allowed_categories column added")
            except Exception as e:
                logger.warning(f"Migration 003: Column may already exist: {e}")

        # CREATE INDEX IF NOT EXISTS is already a silent no-op on a fresh DB
        # (create_all built it too, cf. Media.__table_args__) — safe to run
        # unconditionally regardless of which branch above was taken.
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_media_category_visible
            ON media(is_in_allowed_categories)
        """))


async def _migration_004_add_enrichment_existing_ids(engine: AsyncEngine) -> None:
    """Add existing_tmdb_id and existing_imdb_id to enrichment_queue table."""
    logger.info("Migration 004: Adding existing ID columns to enrichment_queue")

    async with engine.begin() as conn:
        if await _column_exists(conn, "enrichment_queue", "existing_tmdb_id"):
            logger.debug("Migration 004: existing_tmdb_id already present, skipping ADD COLUMN")
        else:
            try:
                await conn.execute(text("""
                    ALTER TABLE enrichment_queue
                    ADD COLUMN existing_tmdb_id TEXT
                """))
                logger.info("Migration 004: existing_tmdb_id column added")
            except Exception as e:
                logger.warning(f"Migration 004: existing_tmdb_id may already exist: {e}")

        if await _column_exists(conn, "enrichment_queue", "existing_imdb_id"):
            logger.debug("Migration 004: existing_imdb_id already present, skipping ADD COLUMN")
        else:
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
        if await _column_exists(conn, "media", "cast"):
            logger.debug("Migration 005: cast already present, skipping ADD COLUMN")
            return
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


async def _migration_008_ai_embeddings(conn) -> None:
    """Create ai_embeddings (sqlite-vec virtual table) and ai_tmdb_cache.

    CR-C10: defined here (between 007 and 009) to match its position in the
    numeric chain for readability. It still takes a raw ``conn`` (not an
    ``AsyncEngine``) and is still invoked from ``run_migrations()`` on its
    own dedicated connection/transaction (see the ``async with engine.begin()
    as conn: await _migration_008_ai_embeddings(conn)`` block just above
    ``_migration_009``'s call) — moving the *definition* doesn't touch the
    *execution* order, since Python resolves the name at call time and every
    function in this module is already defined before ``run_migrations()``
    ever runs. Kept on its own connection (rather than folded into the
    ``engine`` migrations list) because it depends on sqlite-vec being loaded
    on that connection (``register_sqlite_vec_listener``, db/database.py) and
    is reused as-is by isolated test fixtures that only need the vec0 tables.
    Idempotent: every DDL uses IF NOT EXISTS.
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
        if await _column_exists(conn, "enrichment_queue", "existing_summary"):
            logger.debug("Migration 010: existing_summary already present, skipping ADD COLUMN")
        else:
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
    Xtream category name/id. Idempotent: column existence is probed first
    (CR-C05) so a fresh DB (create_all already added it) is a silent no-op;
    try/except remains as a safety net for a race with another process.
    """
    logger.info("Migration 013: Adding is_adult to media")

    async with engine.begin() as conn:
        if await _column_exists(conn, "media", "is_adult"):
            logger.debug("Migration 013: is_adult already present, skipping ADD COLUMN")
        else:
            try:
                await conn.execute(text("""
                    ALTER TABLE media
                    ADD COLUMN is_adult INTEGER NOT NULL DEFAULT 0
                """))
                logger.info("Migration 013: is_adult column added")
            except Exception as e:
                logger.warning(f"Migration 013: Column may already exist: {e}")

        # CREATE INDEX IF NOT EXISTS is already a silent no-op on a fresh DB
        # (create_all built it too, cf. Media.__table_args__) — run it
        # unconditionally so the index always exists even if a previous
        # run of this migration only got as far as adding the column.
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_media_adult
            ON media(is_adult)
        """))


async def _migration_014_add_nfo_metadata(engine: AsyncEngine) -> None:
    """Add tinyMediaManager NFO metadata columns to the media table.

    Populated only by services/nfo_import_service (never by the Xtream sync):
    - identity / descriptive: original_title, tagline, premiered, status,
      studio, country
    - external IDs: tvdb_id, wikidata_id
    - per-source ratings + vote counts: imdb_rating/imdb_votes,
      tmdb_rating/tmdb_votes (feed the IMDb/TMDb badges)
    - structured cast: cast_json (the legacy `cast` CSV is kept untouched)

    Idempotent: each column is probed (PRAGMA table_info) before ADD COLUMN
    is attempted, so a column already present (fresh DB via create_all,
    CR-C05) is a silent no-op instead of a raise-and-warn; the try/except
    remains as a safety net for a race with another process's init_db().
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
            if await _column_exists(conn, "media", name):
                logger.debug("Migration 014: %s already present, skipping ADD COLUMN", name)
                continue
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


async def _migration_016_encrypt_xtream_passwords(engine: AsyncEngine) -> None:
    """One-time, idempotent encryption of pre-existing xtream_accounts.password
    rows (CR-S03 — Xtream provider passwords were stored in plaintext).

    ``XtreamAccount.password`` is now mapped through
    ``app.utils.crypto_fields.EncryptedString`` (models/database.py), which
    transparently encrypts on write / decrypts on read for every ORM/Core
    access going forward. This migration handles the DATA that already
    exists on disk: rows written before this fix landed (and any DB backup
    snapshot taken since) are still plaintext.

    Uses raw ``text()`` SQL deliberately (bypassing the ORM type decorator)
    so it can inspect the byte-for-byte stored value and decide per-row
    whether encryption is needed — the exact same key resolution
    (`get_xtream_fernet`) as the column type, so what this migration writes
    round-trips correctly through the ORM afterwards.

    Idempotent / re-runnable:
    - A value already recognized as a Fernet token (`looks_encrypted`) is
      skipped -> re-running on an already-encrypted database is a no-op.
    - NULL/empty passwords are skipped.
    - Fail-open, consistent with `EncryptedString`: if no key is configured
      (`get_xtream_fernet()` returns None), this migration logs a warning
      and leaves rows untouched rather than failing the whole migration
      chain — safe to re-run once an operator sets a key and restarts.
    """
    logger.info("Migration 016: Encrypting existing xtream_accounts.password rows")

    fernet = get_xtream_fernet()
    if fernet is None:
        logger.warning(
            "Migration 016: no XTREAM_ENCRYPTION_KEY/AI_API_KEY configured — "
            "leaving existing Xtream passwords in plaintext (CR-S03 residual). "
            "Set one of those env vars and restart to encrypt them (safe to re-run)."
        )
        return

    async with engine.begin() as conn:
        try:
            result = await conn.execute(text("SELECT id, password FROM xtream_accounts"))
            rows = result.fetchall()
        except Exception as e:
            logger.warning("Migration 016: could not read xtream_accounts: %s", e)
            return

        encrypted_count = 0
        for account_id, password in rows:
            if not password or looks_encrypted(password):
                continue
            token = fernet.encrypt(password.encode("utf-8")).decode("ascii")
            await conn.execute(
                text("UPDATE xtream_accounts SET password = :token WHERE id = :account_id"),
                {"token": token, "account_id": account_id},
            )
            encrypted_count += 1

        logger.info(
            "Migration 016: encrypted %d pre-existing plaintext password row(s) "
            "out of %d total (rest already encrypted, empty, or missing)",
            encrypted_count, len(rows),
        )


async def _migration_017_create_media_group_snapshot(engine: AsyncEngine) -> None:
    """Create the CR-P01 unified-group snapshot tables (media_group +
    media_group_member).

    These hold the precomputed output of the whole-catalog unified aggregation
    (services.unified_group_service), so the UNFILTERED /movies|shows/unified
    browse endpoints page over already-grouped rows with a DB LIMIT instead of
    loading + aggregating the entire catalog per request. Purely additive — no
    existing table/column/data is touched; the snapshot is (re)built by the
    pipeline, and the read path falls back to live aggregation whenever it is
    empty (fresh DB before the first build), so an empty/absent snapshot is
    always safe.

    Idempotent: CREATE TABLE/INDEX IF NOT EXISTS; a fresh DB already has these
    via Base.metadata.create_all (models/database.py) so this is a silent no-op
    there, and an upgraded DB gets them here.
    """
    logger.info("Migration 017: Creating media_group snapshot tables")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS media_group (
                    media_type    TEXT    NOT NULL,
                    group_key     TEXT    NOT NULL,
                    sort_added_at INTEGER NOT NULL DEFAULT 0,
                    version_count INTEGER NOT NULL DEFAULT 0,
                    built_at      INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (media_type, group_key)
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS media_group_member (
                    media_type TEXT NOT NULL,
                    group_key  TEXT NOT NULL,
                    server_id  TEXT NOT NULL,
                    rating_key TEXT NOT NULL,
                    PRIMARY KEY (media_type, group_key, server_id, rating_key)
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_media_group_type_sort "
                "ON media_group(media_type, sort_added_at)"
            ))
            # No secondary index on media_group_member: the composite-PK's
            # implicit index (leading media_type, group_key) already serves the
            # `WHERE media_type=? AND group_key IN (...)` page lookup.
            logger.info("Migration 017: media_group snapshot tables created")
        except Exception as e:
            logger.warning("Migration 017: tables may already exist: %s", e)


async def _migration_018_create_download_tables(engine: AsyncEngine) -> None:
    """Create the physical-media-download tables: download_batch + download_job
    (PH-DL-01, docs/20-impl-media-download.md §3).

    Two purely additive tables backing the new "Télécharger" feature (writes
    actual media bytes to DOWNLOAD_DIR, distinct from the existing PLEX_LIBRARY_DIR
    .strm catalog — nothing about /api/media, /api/plex or existing sync/enrichment
    flows is touched here). Nothing destructive: no ALTER on an existing table, no
    FK to enforce (batch_id is a soft pointer — a completed job is intentionally
    re-enqueue-able, and a movie has batch_id=NULL by design, spec §3.1).

    - `download_batch`: one row per "download the whole series" selection (a
      single movie download does NOT get a batch row — batch_id stays NULL on its
      job, spec §3.1's figée decision).
    - `download_job`: one row per downloaded FILE (one movie, or one episode of a
      series batch). Carries the state machine (queued/running/completed/failed/
      canceled), byte progress, and a server-relative `dest_path` — never an
      absolute path and never the upstream Xtream URL (which contains
      user/password and is re-derived at worker time, never persisted, spec §0.7).

    Idempotent: CREATE TABLE/INDEX IF NOT EXISTS throughout; a fresh DB already
    has both tables (+ every index below) via Base.metadata.create_all
    (models/database.py's DownloadBatch/DownloadJob), so this is a silent no-op
    there, and an upgraded DB gets them here — same convergence invariant as
    migration 017 (CR-C05/CR-P02).
    """
    logger.info("Migration 018: Creating download_batch/download_job tables")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS download_batch (
                    id              TEXT    PRIMARY KEY,
                    media_type      TEXT    NOT NULL,
                    unification_id  TEXT,
                    title           TEXT    NOT NULL,
                    server_id       TEXT    NOT NULL,
                    rating_key      TEXT    NOT NULL,
                    scope           TEXT    NOT NULL,
                    total_jobs      INTEGER NOT NULL DEFAULT 0,
                    created_at      BIGINT  NOT NULL
                )
            """))

            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS download_job (
                    id              TEXT    PRIMARY KEY,
                    batch_id        TEXT,
                    server_id       TEXT    NOT NULL,
                    rating_key      TEXT    NOT NULL,
                    media_type      TEXT    NOT NULL,
                    unification_id  TEXT,
                    title           TEXT    NOT NULL,
                    season          INTEGER,
                    episode         INTEGER,
                    dest_path       TEXT    NOT NULL,
                    state           TEXT    NOT NULL DEFAULT 'queued',
                    bytes_total     BIGINT,
                    bytes_done      BIGINT  NOT NULL DEFAULT 0,
                    error           TEXT,
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    created_at      BIGINT  NOT NULL,
                    updated_at      BIGINT  NOT NULL,
                    started_at      BIGINT,
                    finished_at     BIGINT
                )
            """))

            for idx_sql in [
                # Drain (`WHERE state='queued'`) + boot reap (`WHERE state='running'`).
                "CREATE INDEX IF NOT EXISTS ix_download_job_state ON download_job(state)",
                # Batch detail view (list a series' jobs).
                "CREATE INDEX IF NOT EXISTS ix_download_job_batch ON download_job(batch_id)",
                # Queue ordering (ORDER BY created_at) for the drain loop + admin list.
                "CREATE INDEX IF NOT EXISTS ix_download_job_created ON download_job(created_at)",
                # Idempotent-enqueue dedup: find a non-terminal job for (server_id, rating_key).
                "CREATE INDEX IF NOT EXISTS ix_download_job_item ON download_job(server_id, rating_key)",
            ]:
                await conn.execute(text(idx_sql))

            logger.info("Migration 018: download_batch/download_job tables created")
        except Exception as e:
            logger.warning("Migration 018: tables may already exist: %s", e)


async def _migration_019_create_plex_tables(engine: AsyncEngine) -> None:
    """Create the Plex shared-servers catalogue tables: plex_server,
    plex_media_item, plex_sync_status (feature "Télécharger Plex",
    docs/10-prd-media-download.md).

    Purely additive, fully isolated from the existing `media`/Xtream schema:
    no ALTER on an existing table, no FK to enforce. `plex_media_item` is a
    lightweight, server-scoped mirror of `Media` used only as a download
    source (never surfaced by `/api/media` or the .strm/NFO generator).
    `plex_server` stores one row per discovered Plex Media Server (owned or
    shared) with its per-server `access_token` Fernet-encrypted at rest
    (`EncryptedString`, same convention as `XtreamAccount.password`,
    CR-S03) — never plaintext, never logged. `plex_sync_status` is a
    singleton (id=1) claimed via conditional UPDATE by the sync service.

    Idempotent: CREATE TABLE/INDEX IF NOT EXISTS throughout; a fresh DB
    already has all three tables (+ every index below) via
    Base.metadata.create_all (models/database.py's PlexServer/
    PlexMediaItem/PlexSyncStatus), so this is a silent no-op there, and an
    upgraded DB gets them here — same convergence invariant as migrations
    017/018 (CR-C05/CR-P02).
    """
    logger.info("Migration 019: Creating plex_server/plex_media_item/plex_sync_status tables")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS plex_server (
                    client_identifier TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner_title TEXT,
                    owned INTEGER NOT NULL DEFAULT 0,
                    access_token TEXT,
                    base_uri TEXT,
                    is_reachable INTEGER NOT NULL DEFAULT 0,
                    last_synced_at BIGINT,
                    last_sync_error TEXT,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )
            """))

            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS plex_media_item (
                    server_id TEXT NOT NULL,
                    rating_key TEXT NOT NULL,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    parent_rating_key TEXT,
                    grandparent_rating_key TEXT,
                    parent_index INTEGER,
                    "index" INTEGER,
                    imdb_id TEXT,
                    tmdb_id TEXT,
                    tvdb_id TEXT,
                    unification_id TEXT,
                    thumb_url TEXT,
                    added_at BIGINT,
                    height INTEGER,
                    width INTEGER,
                    video_codec TEXT,
                    audio_codec TEXT,
                    container TEXT,
                    bitrate INTEGER,
                    part_key TEXT,
                    part_size BIGINT,
                    duration_ms BIGINT,
                    synced_at BIGINT NOT NULL,
                    PRIMARY KEY (server_id, rating_key)
                )
            """))

            for idx_sql in [
                # Resolve a show/movie's unified group for the download-source picker.
                "CREATE INDEX IF NOT EXISTS ix_plex_item_type_unif ON plex_media_item(type, unification_id)",
                # List a show's episodes (same server) for "download whole series".
                "CREATE INDEX IF NOT EXISTS ix_plex_item_show ON plex_media_item(server_id, grandparent_rating_key)",
                # Recency-ordered browse per type.
                "CREATE INDEX IF NOT EXISTS ix_plex_item_type_added ON plex_media_item(type, added_at)",
                # Title search/sort per type.
                "CREATE INDEX IF NOT EXISTS ix_plex_item_type_title ON plex_media_item(type, title)",
            ]:
                await conn.execute(text(idx_sql))

            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS plex_sync_status (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state TEXT NOT NULL DEFAULT 'idle',
                    started_at BIGINT,
                    finished_at BIGINT,
                    error TEXT
                )
            """))

            logger.info("Migration 019: plex_server/plex_media_item/plex_sync_status tables created")
        except Exception as e:
            logger.warning("Migration 019: tables may already exist: %s", e)


async def _migration_020_add_media_file_size(engine: AsyncEngine) -> None:
    """Add the `file_size` column to the media table (extension "download
    Xtream granulaire + taille", Tâche X1a fondation).

    Purely additive and nullable: `file_size` (BIGINT, bytes) is NOT
    backfilled by this migration — it stays NULL for every existing row
    until the health-check worker (Tâche X1b) captures the `Content-Length`
    header of the HEAD request it already issues during stream validation
    and writes it back. Consumed later by the download-selector UI to show
    file sizes before enqueueing a download job.

    Idempotent: the column is probed (PRAGMA table_info) before ADD COLUMN
    is attempted, so a column already present (fresh DB via create_all,
    CR-C05) is a silent no-op instead of a raise-and-warn; the try/except
    remains as a safety net for a race with another process's init_db().
    """
    logger.info("Migration 020: Adding file_size column to media")

    columns = [
        ("file_size", "BIGINT"),
    ]

    for name, sql_type in columns:
        async with engine.begin() as conn:
            if await _column_exists(conn, "media", name):
                logger.debug("Migration 020: %s already present, skipping ADD COLUMN", name)
                continue
            try:
                await conn.execute(text(
                    f"ALTER TABLE media ADD COLUMN {name} {sql_type}"
                ))
                logger.info("Migration 020: %s column added", name)
            except Exception as e:
                logger.warning("Migration 020: %s may already exist: %s", name, e)


async def _migration_021_add_plex_media_item_genres(engine: AsyncEngine) -> None:
    """Add the `genres` column to plex_media_item (feature "écran de
    téléchargement unifié Plex+Xtream", Vague W1 — genre filter parity with
    the Xtream `media.genres` column).

    Purely additive and nullable: `genres` (TEXT, comma-separated Plex
    `Genre[].tag`) is NOT backfilled — it stays NULL for every existing
    `plex_media_item` row until the next Plex catalogue sync
    (`plex_sync_service`) re-upserts it with the value captured by
    `plex_api_service.parse_genres`. Consumed by `plex_catalog_service`'s and
    the unified download screen's genre filter (`.ilike('%genre%')`).

    Idempotent: the column is probed (PRAGMA table_info) before ADD COLUMN, so
    a column already present (fresh DB via create_all, CR-C05) is a silent
    no-op; the try/except remains as a safety net for a race with another
    process's init_db().
    """
    logger.info("Migration 021: Adding genres column to plex_media_item")

    async with engine.begin() as conn:
        if await _column_exists(conn, "plex_media_item", "genres"):
            logger.debug("Migration 021: genres already present, skipping ADD COLUMN")
            return
        try:
            await conn.execute(text(
                "ALTER TABLE plex_media_item ADD COLUMN genres TEXT"
            ))
            logger.info("Migration 021: genres column added")
        except Exception as e:
            logger.warning("Migration 021: genres may already exist: %s", e)


async def _migration_022_create_omdb_scrape_cache(engine: AsyncEngine) -> None:
    """Create the omdb_scrape_cache table (imdb-id consistency validator).

    Persistent cache of an OMDb lookup, keyed directly on `imdb_id` — the
    validator always has an imdb_id in hand (it's cross-checking an existing
    resolution), so this is a direct point cache, unlike `tmdb_scrape_cache`
    (title-signature keyed, because TMDB resolution starts from a title).
    Purely additive: no existing table/column/data is touched.

    Idempotent: CREATE TABLE/INDEX IF NOT EXISTS; a fresh DB already has this
    table (+ its index) via Base.metadata.create_all (models/database.py's
    OmdbScrapeCache), so this is a silent no-op there, and an upgraded DB
    gets it here — same convergence invariant as migrations 017/018/019
    (CR-C05/CR-P02): the DDL below byte-matches the ORM model's columns/types
    and index name.
    """
    logger.info("Migration 022: Creating omdb_scrape_cache table")

    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS omdb_scrape_cache (
                    imdb_id    TEXT    PRIMARY KEY,
                    result     TEXT    NOT NULL,
                    payload    TEXT,
                    fetched_at INTEGER NOT NULL
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_omdb_scrape_cache_fetched_at "
                "ON omdb_scrape_cache(fetched_at)"
            ))
            logger.info("Migration 022: omdb_scrape_cache table created")
        except Exception as e:
            logger.warning("Migration 022: table may already exist: %s", e)
