from sqlalchemy import (
    Column, Text, Integer, BigInteger, Boolean, Float, Index, String,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Media(Base):
    __tablename__ = "media"

    # Composite Primary Key (matches Android)
    rating_key = Column(Text, primary_key=True)
    server_id = Column(Text, primary_key=True)
    filter = Column(Text, primary_key=True, default="all")
    sort_order = Column(Text, primary_key=True, default="default")

    # Core metadata
    library_section_id = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    title_sortable = Column(Text, nullable=False, default="")
    page_offset = Column(Integer, nullable=False, default=0)
    type = Column(Text, nullable=False)  # 'movie', 'show', 'episode'
    thumb_url = Column(Text)
    art_url = Column(Text)
    year = Column(Integer)
    duration = Column(Integer)  # milliseconds
    summary = Column(Text)
    genres = Column(Text)  # comma-separated
    content_rating = Column(Text)

    # Playback state
    view_offset = Column(Integer, nullable=False, default=0)
    view_count = Column(Integer, nullable=False, default=0)
    last_viewed_at = Column(BigInteger, nullable=False, default=0)

    # Hierarchy (Series -> Season -> Episode)
    parent_title = Column(Text)
    parent_rating_key = Column(Text)
    parent_index = Column(Integer)  # season number
    grandparent_title = Column(Text)
    grandparent_rating_key = Column(Text)
    index = Column("index", Integer)  # episode number
    parent_thumb = Column(Text)
    grandparent_thumb = Column(Text)

    # Media parts (always "[]" for Xtream)
    media_parts = Column(Text, nullable=False, default="[]")

    # External IDs
    guid = Column(Text)
    imdb_id = Column(Text)
    tmdb_id = Column(Text)
    rating = Column(Float)
    audience_rating = Column(Float)

    # Unification / Aggregation
    unification_id = Column(Text, nullable=False, default="")
    history_group_key = Column(Text, nullable=False, default="")
    server_ids = Column(Text)  # comma-separated for aggregation
    rating_keys = Column(Text)  # comma-separated for aggregation

    # Timestamps
    added_at = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(BigInteger, nullable=False, default=0)

    # Display optimization
    display_rating = Column(Float, nullable=False, default=0.0)
    scraped_rating = Column(Float)
    resolved_thumb_url = Column(Text)
    resolved_art_url = Column(Text)
    resolved_base_url = Column(Text)
    alternative_thumb_urls = Column(Text)  # pipe-separated
    cast = Column(Text)  # comma-separated actor names from TMDB

    # Backend-specific
    stream_error_count = Column(Integer, nullable=False, default=0)
    last_stream_check = Column(BigInteger)
    is_broken = Column(Boolean, nullable=False, default=False)
    tmdb_match_confidence = Column(Float)
    content_hash = Column(Text)  # MD5 of sync-provided fields, skip UPDATE if unchanged
    dto_hash = Column(Text)  # MD5 of Xtream DTO basic fields, for incremental sync
    is_in_allowed_categories = Column(Boolean, nullable=False, default=True)  # Category filtering

    __table_args__ = (
        Index("uix_media_pagination", "server_id", "library_section_id", "filter", "sort_order", "page_offset", unique=True),
        Index("ix_media_guid", "guid"),
        Index("ix_media_type_added", "type", "added_at"),
        Index("ix_media_imdb", "imdb_id"),
        Index("ix_media_tmdb", "tmdb_id"),
        Index("ix_media_server_lib", "server_id", "library_section_id"),
        Index("ix_media_unification", "unification_id"),
        Index("ix_media_type_rating", "type", "display_rating"),
        Index("ix_media_parent", "parent_rating_key"),
        Index("ix_media_title_sort", "title_sortable"),
        Index("ix_media_broken", "is_broken"),
        Index("ix_media_updated", "updated_at"),
        Index("ix_media_category_visible", "is_in_allowed_categories"),
        # Compound indexes for common query patterns
        Index("ix_media_server_type", "server_id", "type"),
        Index("ix_media_server_visible", "server_id", "is_in_allowed_categories"),
        Index("ix_media_parent_visible", "parent_rating_key", "is_in_allowed_categories"),
        Index("ix_media_grandparent", "grandparent_rating_key"),
    )


class XtreamAccount(Base):
    __tablename__ = "xtream_accounts"

    id = Column(Text, primary_key=True)  # MD5(baseUrl+username)[:8]
    label = Column(Text, nullable=False)
    base_url = Column(Text, nullable=False)
    port = Column(Integer, nullable=False, default=80)
    username = Column(Text, nullable=False)
    password = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="Unknown")
    expiration_date = Column(BigInteger)
    max_connections = Column(Integer, nullable=False, default=1)
    allowed_formats = Column(Text, nullable=False, default="")  # "ts,mp4,m3u8"
    server_url = Column(Text)
    https_port = Column(Integer)
    last_synced_at = Column(BigInteger, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(BigInteger, nullable=False, default=0)
    category_filter_mode = Column(Text, nullable=False, default="all")  # "all", "whitelist", "blacklist"


class XtreamCategory(Base):
    __tablename__ = "xtream_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Text, nullable=False)
    category_id = Column(Text, nullable=False)
    category_type = Column(Text, nullable=False)  # "vod" or "series"
    category_name = Column(Text, nullable=False)
    is_allowed = Column(Boolean, nullable=False, default=True)
    last_fetched_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_xtream_categories_account", "account_id"),
        Index("ix_xtream_categories_type", "category_type"),
        Index("uix_xtream_categories_item", "account_id", "category_id", "category_type", unique=True),
    )


class LiveChannel(Base):
    __tablename__ = "live_channels"

    # Composite Primary Key
    stream_id = Column(Integer, primary_key=True)
    server_id = Column(Text, primary_key=True)  # "xtream_{account_id}"

    # Core metadata
    name = Column(Text, nullable=False)
    name_sortable = Column(Text, nullable=False, default="")
    stream_icon = Column(Text)  # channel logo URL
    epg_channel_id = Column(Text)  # EPG mapping ID
    category_id = Column(Text)  # Xtream category_id

    # Stream info
    container_extension = Column(Text, default="ts")  # ts, m3u8
    custom_sid = Column(Text)  # custom service ID

    # Catchup / TV Archive
    tv_archive = Column(Boolean, nullable=False, default=False)
    tv_archive_duration = Column(Integer, nullable=False, default=0)  # days

    # Content flags
    is_adult = Column(Boolean, nullable=False, default=False)

    # Status
    is_active = Column(Boolean, nullable=False, default=True)
    is_in_allowed_categories = Column(Boolean, nullable=False, default=True)

    # Sync tracking
    added_at = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(BigInteger, nullable=False, default=0)
    dto_hash = Column(Text)  # for incremental sync

    __table_args__ = (
        Index("ix_live_channels_server", "server_id"),
        Index("ix_live_channels_category", "category_id"),
        Index("ix_live_channels_epg", "epg_channel_id"),
        Index("ix_live_channels_name", "name_sortable"),
        Index("ix_live_channels_visible", "is_in_allowed_categories"),
        Index("ix_live_channels_server_visible", "server_id", "is_in_allowed_categories"),
        Index("ix_live_channels_server_category", "server_id", "category_id"),
    )


class EpgEntry(Base):
    __tablename__ = "epg_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(Text, nullable=False)
    epg_channel_id = Column(Text, nullable=False)  # maps to LiveChannel.epg_channel_id
    stream_id = Column(Integer)  # maps to LiveChannel.stream_id

    title = Column(Text, nullable=False)
    description = Column(Text)
    start_time = Column(BigInteger, nullable=False)  # epoch ms
    end_time = Column(BigInteger, nullable=False)  # epoch ms
    lang = Column(Text)

    # Sync tracking
    fetched_at = Column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        Index("ix_epg_channel", "epg_channel_id"),
        Index("ix_epg_server", "server_id"),
        Index("ix_epg_time", "start_time", "end_time"),
        Index("ix_epg_stream", "stream_id"),
        Index("ix_epg_server_channel", "server_id", "epg_channel_id"),
        Index("ix_epg_stream_time", "stream_id", "start_time", "end_time"),
        Index("uix_epg_dedup", "server_id", "stream_id", "start_time", unique=True),
    )


class TvAuthSession(Base):
    """Device-flow TV pairing session (Mission 18).

    Lifecycle: pending -> approved -> completed (or -> expired).
    - device_code: long opaque secret known only by the TV (poll credential).
    - user_code: short human code displayed on the TV, validated on mobile/web.
    - payload_encrypted: Fernet token (config/token Plex) — encrypted at rest,
      delivered to the TV exactly once, scrubbed at completion.
    """

    __tablename__ = "tv_auth_sessions"

    id = Column(Text, primary_key=True)  # uuid4 hex
    device_code = Column(Text, nullable=False)  # secrets.token_urlsafe(32)
    user_code = Column(Text, nullable=False)  # 8 chars, unambiguous alphabet
    status = Column(Text, nullable=False, default="pending")  # pending/approved/completed/expired
    payload_encrypted = Column(Text)  # Fernet token, null until approved
    payload_delivered = Column(Boolean, nullable=False, default=False)  # one-shot delivery
    device_name = Column(Text)  # optional, e.g. "Mi Box S (salon)"
    created_at = Column(BigInteger, nullable=False)  # epoch ms
    expires_at = Column(BigInteger, nullable=False)  # epoch ms (created_at + TTL 15 min)
    approved_at = Column(BigInteger)
    completed_at = Column(BigInteger)

    __table_args__ = (
        Index("uix_tv_auth_device_code", "device_code", unique=True),
        Index("uix_tv_auth_user_code", "user_code", unique=True),
        Index("ix_tv_auth_expires", "expires_at"),
        Index("ix_tv_auth_status", "status"),
    )


class AiSubtitleCache(Base):
    """Cache for AI-translated subtitle content (WP3 ai-subtitle-translation).

    cache_key is a deterministic hash of (source_content, target_lang, model,
    source_format) so the same source always resolves to the same cache entry.
    created_at stores epoch milliseconds (utils/time.now_ms).
    """

    __tablename__ = "ai_subtitle_cache"

    cache_key = Column(Text, primary_key=True)
    target_lang = Column(Text, nullable=False)
    model = Column(Text, nullable=False)
    source_format = Column(Text, nullable=False)
    cue_count = Column(Integer, nullable=False)
    translated_content = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)  # epoch ms


class AiMediaBlurb(Base):
    """AI-generated French synopsis + mood/genre tags for a title (F3).

    Composite PK on (tmdb_id, media_type, lang) so the same TMDB entry can
    hold blurbs for multiple languages and both movie/tv variants.
    tags is stored as a JSON array string (e.g. '["drame", "émouvant"]').
    created_at stores epoch milliseconds (utils/time.now_ms).
    """

    __tablename__ = "ai_media_blurb"

    tmdb_id = Column(Integer, primary_key=True)
    media_type = Column(Text, primary_key=True)   # "movie" or "tv"
    lang = Column(Text, primary_key=True)          # e.g. "fr", "en"
    summary = Column(Text, nullable=False)
    tags = Column(Text, nullable=False)            # JSON array string
    model = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)  # epoch ms


class EnrichmentQueue(Base):
    __tablename__ = "enrichment_queue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rating_key = Column(Text, nullable=False)
    server_id = Column(Text, nullable=False)
    media_type = Column(Text, nullable=False)  # 'movie' or 'show'
    title = Column(Text, nullable=False)
    year = Column(Integer)
    status = Column(Text, nullable=False, default="pending")  # pending/processing/done/failed/skipped
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text)
    created_at = Column(BigInteger, nullable=False)
    processed_at = Column(BigInteger)
    existing_tmdb_id = Column(Text)  # TMDB ID present before enrichment (if any)
    existing_imdb_id = Column(Text)  # IMDB ID present before enrichment (if any)
    existing_summary = Column(Text)  # Xtream plot — feeds the summary tie-break

    __table_args__ = (
        Index("ix_enrichment_status", "status"),
        Index("uix_enrichment_item", "rating_key", "server_id", unique=True),
        Index("ix_enrichment_status_type", "status", "media_type"),
    )


class TmdbScrapeCache(Base):
    """Persistent cache of a TMDB resolution for a normalized title signature.

    Keyed by `cache_key = f"{media_type}|{normalized_title}|{year or ''}"` so the
    same film/series across accounts (and across restarts) reuses one resolution
    instead of re-querying TMDB. `payload` holds the enrichment JSON on a match.
    """

    __tablename__ = "tmdb_scrape_cache"

    cache_key = Column(Text, primary_key=True)
    media_type = Column(Text, nullable=False)  # 'movie' | 'show'
    result = Column(Text, nullable=False)       # 'matched' | 'ambiguous' | 'nomatch'
    tmdb_id = Column(Text)
    imdb_id = Column(Text)
    confidence = Column(Float)
    payload = Column(Text)                      # JSON of TMDBEnrichmentData when matched
    fetched_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_scrape_cache_fetched_at", "fetched_at"),
    )
