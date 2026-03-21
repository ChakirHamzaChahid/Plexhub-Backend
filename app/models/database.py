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

    __table_args__ = (
        Index("ix_enrichment_status", "status"),
        Index("uix_enrichment_item", "rating_key", "server_id", unique=True),
    )
