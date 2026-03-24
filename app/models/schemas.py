from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing import Optional


# --- Media Schemas ---

class MediaResponse(BaseModel):
    """Single media item in camelCase for Android consumption."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    rating_key: str
    server_id: str
    library_section_id: str
    title: str
    title_sortable: str = ""
    filter: str = "all"
    sort_order: str = "default"
    page_offset: int = 0
    type: str
    thumb_url: Optional[str] = None
    art_url: Optional[str] = None
    year: Optional[int] = None
    duration: Optional[int] = None
    summary: Optional[str] = None
    genres: Optional[str] = None
    content_rating: Optional[str] = None
    view_offset: int = 0
    view_count: int = 0
    last_viewed_at: int = 0

    # Hierarchy
    parent_title: Optional[str] = None
    parent_rating_key: Optional[str] = None
    parent_index: Optional[int] = None
    grandparent_title: Optional[str] = None
    grandparent_rating_key: Optional[str] = None
    index: Optional[int] = None
    parent_thumb: Optional[str] = None
    grandparent_thumb: Optional[str] = None

    # Media parts
    media_parts: str = "[]"

    # External IDs
    guid: Optional[str] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    rating: Optional[float] = None
    audience_rating: Optional[float] = None

    # Unification
    unification_id: str = ""
    history_group_key: str = ""
    server_ids: Optional[str] = None
    rating_keys: Optional[str] = None

    # Timestamps
    added_at: int = 0
    updated_at: int = 0

    # Display
    display_rating: float = 0.0
    scraped_rating: Optional[float] = None
    resolved_thumb_url: Optional[str] = None
    resolved_art_url: Optional[str] = None
    resolved_base_url: Optional[str] = None
    alternative_thumb_urls: Optional[str] = None

    # Backend-specific
    is_broken: bool = False
    tmdb_match_confidence: Optional[float] = None


class MediaListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[MediaResponse]
    total: int
    has_more: bool


# --- Stream Schemas ---

class StreamResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    url: str
    expires_at: Optional[int] = None


# --- Account Schemas ---

class AccountCreate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    label: str
    base_url: str
    port: int = 80
    username: str
    password: str


class AccountUpdate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    label: Optional[str] = None
    base_url: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None


class AccountResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    id: str
    label: str
    base_url: str
    port: int
    username: str
    status: str
    expiration_date: Optional[int] = None
    max_connections: int = 1
    allowed_formats: str = ""
    server_url: Optional[str] = None
    https_port: Optional[int] = None
    last_synced_at: int = 0
    is_active: bool = True
    created_at: int = 0


class AccountTestResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: str
    expiration_date: Optional[int] = None
    max_connections: Optional[int] = None
    allowed_formats: Optional[str] = None


# --- Sync Schemas ---

class SyncRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    account_id: str
    force: bool = False


class SyncStatusResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: str  # "pending", "processing", "completed", "failed"
    progress: Optional[dict] = None


# --- Category Schemas ---

class CategoryResponse(BaseModel):
    """Single category item in camelCase."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    category_id: str
    category_name: str
    category_type: str  # "vod" or "series"
    is_allowed: bool
    last_fetched_at: int


class CategoryUpdate(BaseModel):
    """Category update item for bulk operations."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    category_id: str
    category_type: str
    is_allowed: bool


class CategoryUpdateRequest(BaseModel):
    """Request body for updating category configuration."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    filter_mode: str  # "all", "whitelist", "blacklist"
    categories: list[dict]  # List of category dicts with categoryId, categoryType, isAllowed


class CategoryListResponse(BaseModel):
    """Response for listing categories."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[CategoryResponse]
    filter_mode: str


# --- Live Channel Schemas ---

class LiveChannelResponse(BaseModel):
    """Single live channel in camelCase for frontend consumption."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    stream_id: int
    server_id: str
    name: str
    name_sortable: str = ""
    stream_icon: Optional[str] = None
    epg_channel_id: Optional[str] = None
    category_id: Optional[str] = None
    container_extension: str = "ts"
    custom_sid: Optional[str] = None
    tv_archive: bool = False
    tv_archive_duration: int = 0
    is_adult: bool = False
    is_active: bool = True
    added_at: int = 0
    updated_at: int = 0


class LiveChannelListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[LiveChannelResponse]
    total: int
    has_more: bool


class EpgEntryResponse(BaseModel):
    """Single EPG entry in camelCase."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    id: int
    epg_channel_id: Optional[str] = None
    stream_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    start_time: int
    end_time: int
    lang: Optional[str] = None


class EpgListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[EpgEntryResponse]
    total: int


# --- Health Schemas ---

class HealthResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: str
    version: str
    accounts: int
    total_media: int
    enriched_media: int
    broken_streams: int
    last_sync_at: Optional[int] = None
