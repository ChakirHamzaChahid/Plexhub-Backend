import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from typing import Optional


_IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
_TMDB_ID_RE = re.compile(r"^\d{1,9}$")

# Display-only prefix prepended to adult/X-rated titles at serialization.
# Never stored on Media.title (avoids double-prefixing on re-sync).
ADULT_TITLE_PREFIX = "[XXX] "


def apply_adult_prefix(title: str, is_adult: bool) -> str:
    """Prepend ADULT_TITLE_PREFIX to an adult title, idempotently."""
    if is_adult and title and not title.startswith(ADULT_TITLE_PREFIX):
        return f"{ADULT_TITLE_PREFIX}{title}"
    return title


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
    is_adult: bool = False
    tmdb_match_confidence: Optional[float] = None

    # NFO-imported metadata (tinyMediaManager — see nfo_import_service)
    cast: Optional[str] = None
    cast_json: Optional[str] = None
    original_title: Optional[str] = None
    tagline: Optional[str] = None
    premiered: Optional[str] = None
    status: Optional[str] = None
    studio: Optional[str] = None
    country: Optional[str] = None
    tvdb_id: Optional[str] = None
    wikidata_id: Optional[str] = None
    imdb_rating: Optional[float] = None
    imdb_votes: Optional[int] = None
    tmdb_rating: Optional[float] = None
    tmdb_votes: Optional[int] = None

    @model_validator(mode="after")
    def _prefix_adult_title(self) -> "MediaResponse":
        self.title = apply_adult_prefix(self.title, self.is_adult)
        return self


class MediaListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[MediaResponse]
    total: int
    has_more: bool
    # CR-P04: opaque keyset cursor for the NEXT page. Only populated on the raw
    # list endpoints (/movies, /shows, /episodes) when the caller passes a
    # `cursor` and sorts by added_desc/added_asc; null otherwise. Additive and
    # optional — existing clients that page with offset ignore it.
    next_cursor: Optional[str] = None


# --- Unified (deduped) media schemas — one entry per title, N versions ---

class MediaVersionResponse(BaseModel):
    """One playable source (account/quality/language) of a unified title."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    server_id: str
    rating_key: str
    title: str            # raw source title (carries the VF/HD/… qualifier)
    label: str            # human version label, e.g. "VF · Compte 1"
    is_broken: bool = False


class UnifiedMediaResponse(BaseModel):
    """A movie or show deduped across accounts into a single card + versions."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    unification_id: str
    type: str             # 'movie' or 'show'
    title: str
    year: Optional[int] = None
    summary: Optional[str] = None
    genres: Optional[str] = None
    content_rating: Optional[str] = None
    thumb_url: Optional[str] = None
    art_url: Optional[str] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    rating: Optional[float] = None
    cast: Optional[str] = None
    is_adult: bool = False
    # NFO-imported metadata (tinyMediaManager — see nfo_import_service)
    original_title: Optional[str] = None
    tagline: Optional[str] = None
    premiered: Optional[str] = None
    status: Optional[str] = None
    studio: Optional[str] = None
    country: Optional[str] = None
    tvdb_id: Optional[str] = None
    wikidata_id: Optional[str] = None
    imdb_rating: Optional[float] = None
    imdb_votes: Optional[int] = None
    tmdb_rating: Optional[float] = None
    tmdb_votes: Optional[int] = None
    cast_json: Optional[str] = None
    version_count: int = 0
    versions: list[MediaVersionResponse] = []


class UnifiedMediaListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[UnifiedMediaResponse]
    total: int
    has_more: bool


class UnifiedEpisodeResponse(BaseModel):
    """A single (season, episode) slot deduped across accounts."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    season: int
    episode: int
    title: Optional[str] = None
    summary: Optional[str] = None
    thumb_url: Optional[str] = None
    duration: Optional[int] = None
    version_count: int = 0
    versions: list[MediaVersionResponse] = []


class UnifiedEpisodeListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    unification_id: str
    series_title: str
    items: list[UnifiedEpisodeResponse]
    total: int


class MediaUpdate(BaseModel):
    """Partial update for a media item. Editable fields: imdb_id, tmdb_id."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None

    @field_validator("imdb_id", mode="before")
    @classmethod
    def _validate_imdb_id(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("imdb_id must be a string")
        v = v.strip()
        if v == "":
            return None
        if not _IMDB_ID_RE.match(v):
            raise ValueError("imdb_id must match ^tt\\d{7,10}$ (e.g. tt0133093)")
        return v

    @field_validator("tmdb_id", mode="before")
    @classmethod
    def _validate_tmdb_id(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            v = str(v)
        v = v.strip()
        if v == "":
            return None
        if not _TMDB_ID_RE.match(v):
            raise ValueError("tmdb_id must be a positive integer (1-9 digits)")
        return v


class MediaStatsResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    total: int
    missing_imdb: int
    missing_tmdb: int


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


class JobIdResponse(BaseModel):
    """Returned by fire-and-forget sync/enrichment/validation/pipeline triggers.

    CR-C03: these endpoints used to return a raw ``{"jobId": ...}`` dict (no
    ``response_model`` — missing from the OpenAPI schema). Wire shape is
    unchanged (``jobId`` was already camelCase).
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    job_id: str


class MessageResponse(BaseModel):
    """Simple ``{"message": ...}`` acknowledgement (e.g. task cancellation).

    CR-C03: wire shape unchanged (single-word key — no camelCase transform).
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    message: str


class SyncJobResponse(BaseModel):
    """One tracked sync job entry from the in-memory job tracker
    (``sync_worker.get_all_sync_jobs``).

    ``progress`` intentionally stays an opaque dict: its shape genuinely
    varies by job state (``{}`` while processing, ``{"total":.., "synced":..}``
    on success, ``{"error": ...}`` on failure) — same pattern already used by
    ``SyncStatusResponse.progress`` above.
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    job_id: str
    status: str
    progress: Optional[dict] = None


class SyncJobListResponse(BaseModel):
    """CR-C03: GET /api/sync/jobs used to return a raw ``{"jobs": [...]}``
    dict, each entry a snake_case dict (``job_id``/``status``/``progress``).
    Now typed. Wire change: each job entry's ``job_id`` key becomes
    ``jobId`` (camelCase) — ``status``/``progress`` are unchanged (already
    single-word).
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    jobs: list[SyncJobResponse]


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


class CategoryRefreshResponse(BaseModel):
    """Response for POST /accounts/{account_id}/categories/refresh.

    CR-C02: was a raw dict with snake_case ``vod_count``/``series_count`` keys
    (bypassing the camelCase-on-the-wire convention). Now typed + aliased —
    wire fields are ``vodCount``/``seriesCount``.
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    message: str
    vod_count: int
    series_count: int
    total: int


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


# --- Download Schemas (physical media download — F-008,
#     docs/20-impl-media-download.md, PH-DL-01) ---

class DownloadJobResponse(BaseModel):
    """One ``download_job`` row in camelCase, for both the JSON admin router
    (``GET /api/admin/downloads``) and any programmatic consumer.

    Wire shape (spec §4, figée): ``{ jobId, batchId?, type, unificationId?,
    title, season?, episode?, serverId, ratingKey, state, bytesDownloaded,
    bytesTotal?, percent?, speedBps?, destPath, error?, retries, createdAt,
    updatedAt, startedAt?, finishedAt? }``.

    ``bytesDownloaded``/``retries`` read different ORM column names
    (``DownloadJob.bytes_done``/``.attempts``) and ``percent``/``speedBps`` are
    COMPUTED (not stored) — so, per the spec's figée decision, this schema is
    deliberately NOT populated via ``from_attributes=True``/``model_validate(job)``.
    It is built by a single explicit builder (``to_download_response(job)``,
    owned by ``services.download_service`` alongside its ``compute_percent``/
    ``compute_speed_bps`` helpers — PH-DL-03) that maps a ``DownloadJob`` row to
    this response. Kept out of this module deliberately: ``schemas.py`` has a
    single editor (this ticket, PH-DL-01) per ``docs/31-board.md``'s
    non-collision rule, so a builder that PH-DL-03/04/05 need to reuse/extend
    belongs in a file they own, not here.
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    job_id: str
    batch_id: Optional[str] = None
    type: str                                  # 'movie' | 'episode' (== DownloadJob.media_type)
    unification_id: Optional[str] = None
    title: str
    season: Optional[int] = None
    episode: Optional[int] = None
    server_id: str
    rating_key: str
    state: str                                  # queued|running|completed|failed|canceled
    bytes_downloaded: int = 0                   # == DownloadJob.bytes_done
    bytes_total: Optional[int] = None
    percent: Optional[float] = None             # computed: round(bytes_downloaded/bytes_total*100, 1)
    speed_bps: Optional[float] = None           # computed: average bytes/sec while state=='running'
    dest_path: str                              # relative to DOWNLOAD_DIR, never absolute/client-supplied
    error: Optional[str] = None                 # bounded message, never the upstream URL
    retries: int = 0                            # == DownloadJob.attempts
    created_at: int
    updated_at: int
    started_at: Optional[int] = None
    finished_at: Optional[int] = None


class DownloadJobListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[DownloadJobResponse]
    total: int


class DownloadEnqueueRequest(BaseModel):
    """JSON body for ``POST /api/admin/downloads`` (P2 — the primary HTMX admin
    route ``POST /admin/downloads`` takes a Form instead, per spec §7.1)."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str                                   # 'movie' | 'show'
    unification_id: str
    server_id: str
    rating_key: str
    scope: str                                  # 'movie' | 'series_all' | 'series_seasons'
    seasons: Optional[list[int]] = None         # required (non-empty) for series_seasons


# --- Plex catalogue Schemas (feature "Télécharger Plex", Tâche C4 —
#     read-only shapes for the JSON mirror, Tâche C7; the reads themselves
#     live in services.plex_catalog_service, which returns dataclasses —
#     these Pydantic models are the wire serialization the router builds
#     from those dataclasses.) ---

class PlexServerResponse(BaseModel):
    """One ``plex_server`` row, camelCase. NEVER ``accessToken``/``baseUri``
    (per-server secret + its connection URI — see ``PlexServer`` docstring,
    house-law piège on secrets)."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    server_id: str                              # "plex_<clientIdentifier>"
    client_identifier: str
    name: str
    owner_title: Optional[str] = None
    owned: bool
    is_reachable: bool
    last_synced_at: Optional[int] = None
    last_sync_error: Optional[str] = None


class PlexSourceResponse(BaseModel):
    """One playable version of a unified Plex item (one row of
    ``plex_media_item``)."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    server_id: str
    rating_key: str
    server_name: str
    resolution: str                             # "1080p" or "" if unknown
    size_bytes: Optional[int] = None
    video_codec: Optional[str] = None
    container: Optional[str] = None


class PlexUnifiedItemResponse(BaseModel):
    """One deduplicated Plex movie/show group (``GROUP BY unification_id``
    over ``plex_media_item``)."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    unification_id: str
    type: str                                   # 'movie' | 'show'
    title: str
    year: Optional[int] = None
    source_count: int
    sources: list[PlexSourceResponse] = []


class PlexServerListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[PlexServerResponse]
    total: int


class PlexCatalogResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[PlexUnifiedItemResponse]
    total: int
