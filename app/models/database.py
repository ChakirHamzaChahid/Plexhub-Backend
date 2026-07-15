from sqlalchemy import (
    Column, Text, Integer, BigInteger, Boolean, Float, Index, String,
    CheckConstraint,
)
from sqlalchemy.orm import DeclarativeBase

from app.utils.crypto_fields import EncryptedString


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
    file_size = Column(BigInteger)  # file size in bytes, populated by health-check HEAD Content-Length (nullable — unknown until validated)
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

    # NFO-imported metadata (tinyMediaManager) — see services/nfo_import_service.
    # Filled from movie.nfo / tvshow.nfo; never sourced from the Xtream sync.
    original_title = Column(Text)            # <originaltitle> / <english_title>
    tagline = Column(Text)                   # <tagline>
    premiered = Column(Text)                 # ISO date "YYYY-MM-DD" (<premiered>/<aired>)
    status = Column(Text)                    # show status: "Continuing" / "Ended"
    studio = Column(Text)                    # comma-separated <studio>
    country = Column(Text)                   # comma-separated <country>
    tvdb_id = Column(Text)                   # <uniqueid type="tvdb">
    wikidata_id = Column(Text)               # <uniqueid type="wikidata">
    imdb_rating = Column(Float)              # <ratings><rating name="imdb"><value>
    imdb_votes = Column(Integer)             # <ratings><rating name="imdb"><votes>
    tmdb_rating = Column(Float)              # <ratings><rating name="themoviedb"><value>
    tmdb_votes = Column(Integer)             # <ratings><rating name="themoviedb"><votes>
    cast_json = Column(Text)                 # JSON [{name, role, thumb, profile, tvdbid}]

    # Backend-specific
    stream_error_count = Column(Integer, nullable=False, default=0)
    last_stream_check = Column(BigInteger)
    is_broken = Column(Boolean, nullable=False, default=False)
    tmdb_match_confidence = Column(Float)
    content_hash = Column(Text)  # MD5 of sync-provided fields, skip UPDATE if unchanged
    dto_hash = Column(Text)  # MD5 of Xtream DTO basic fields, for incremental sync
    is_in_allowed_categories = Column(Boolean, nullable=False, default=True)  # Category filtering
    is_adult = Column(Boolean, nullable=False, default=False)  # Adult/X-rated (from adult Xtream category)

    __table_args__ = (
        Index("uix_media_pagination", "server_id", "library_section_id", "filter", "sort_order", "page_offset", unique=True),
        Index("ix_media_guid", "guid"),
        Index("ix_media_type_added", "type", "added_at"),
        Index("ix_media_imdb", "imdb_id"),
        Index("ix_media_tmdb", "tmdb_id"),
        Index("ix_media_tvdb", "tvdb_id"),
        Index("ix_media_server_lib", "server_id", "library_section_id"),
        Index("ix_media_unification", "unification_id"),
        Index("ix_media_type_rating", "type", "display_rating"),
        Index("ix_media_parent", "parent_rating_key"),
        Index("ix_media_title_sort", "title_sortable"),
        Index("ix_media_broken", "is_broken"),
        Index("ix_media_updated", "updated_at"),
        Index("ix_media_category_visible", "is_in_allowed_categories"),
        Index("ix_media_adult", "is_adult"),
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
    password = Column(EncryptedString(), nullable=False)  # encrypted at rest, CR-S03
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


class ApiKey(Base):
    """Backend API key issued to a user/device.

    The plaintext token (format ``phk_<random>``) is shown ONCE at creation and
    never stored — only its SHA-256 hex digest lives in ``key_hash`` (unique,
    indexed for O(1) lookup on each request). ``key_prefix`` keeps the first few
    plaintext chars for display so a key is recognisable in the admin list.

    A key is valid when ``revoked_at IS NULL`` and (``expires_at IS NULL`` or
    ``expires_at`` is in the future). Revoking = setting ``revoked_at`` — takes
    effect immediately since verification hits this table per request.

    The legacy shared secret ``settings.AI_API_KEY`` stays a permanent master
    key handled separately in ``app.api.deps`` and is NOT stored here.
    """

    __tablename__ = "api_keys"

    id = Column(Text, primary_key=True)          # short uuid4 hex
    key_hash = Column(Text, nullable=False, unique=True)  # sha256 hex of the token
    key_prefix = Column(Text, nullable=False)    # e.g. "phk_a1b2c3" for display
    label = Column(Text, nullable=False)         # assignee (user / device name)
    created_at = Column(BigInteger, nullable=False)  # epoch ms
    expires_at = Column(BigInteger)              # epoch ms, NULL = never expires
    revoked_at = Column(BigInteger)              # epoch ms, NULL = active
    last_used_at = Column(BigInteger)            # epoch ms, NULL = never used
    last_used_ip = Column(Text)                  # last client IP seen

    __table_args__ = (
        Index("ix_api_keys_key_hash", "key_hash"),
    )


class MediaGroup(Base):
    """CR-P01: precomputed unified-group snapshot for the browse endpoints.

    One row per converged group — the output of the SAME whole-catalog
    aggregation (`aggregate_movies`/`_converge`) the live `/movies|shows/unified`
    path uses, materialized at pipeline time by `services.unified_group_service`.
    The UNFILTERED unified list then pages over THIS table with a DB LIMIT
    instead of loading + aggregating the entire category-allowed catalog on
    every request (CR-P01). Filtered/searched queries stay on the live path
    (filtering rows changes group membership + best-row selection, so a
    pre-aggregated snapshot can't reproduce them) — see
    `media_service.get_unified_list`.

    Deliberately minimal: only the grouping identity + recency sort key live
    here. The per-page card + `versions[]` are rebuilt byte-identically by
    re-aggregating the page's member rows (`MediaGroupMember`), so this table
    never mirrors ~60 `Media` columns and cannot drift from them.
    """

    __tablename__ = "media_group"

    media_type = Column(Text, primary_key=True)          # 'movie' | 'show'
    group_key = Column(Text, primary_key=True)           # converged aggregate key
    sort_added_at = Column(BigInteger, nullable=False, default=0)  # best.added_at
    version_count = Column(Integer, nullable=False, default=0)
    built_at = Column(BigInteger, nullable=False, default=0)       # epoch ms of the build

    __table_args__ = (
        Index("ix_media_group_type_sort", "media_type", "sort_added_at"),
    )


class MediaGroupMember(Base):
    """CR-P01: membership of a `MediaGroup` — one pointer per distinct
    (server_id, rating_key) in `media`, used to re-hydrate + re-aggregate a
    page's groups into byte-identical cards / `versions[]`.

    Only (server_id, rating_key) is stored (not `media`'s full 4-column PK):
    the read path's `(server_id, rating_key) IN (...)` join re-loads every
    filter/sort_order variant of an item and re-aggregates them, so the
    builder stores just one pointer per item (see unified_group_service).

    No secondary index: the page lookup ``WHERE media_type=? AND group_key IN
    (...)`` is already served by the composite-PK's implicit index (leading
    columns media_type, group_key), so an extra index would only add write cost
    to the full per-build rewrite.
    """

    __tablename__ = "media_group_member"

    media_type = Column(Text, primary_key=True)
    group_key = Column(Text, primary_key=True)
    server_id = Column(Text, primary_key=True)
    rating_key = Column(Text, primary_key=True)


class DownloadBatch(Base):
    """PH-DL-01: groups the N jobs of a "download whole series" selection
    (docs/20-impl-media-download.md §3.1).

    A single-movie download does NOT get a batch row (figée decision, §3.1):
    the movie's `DownloadJob.batch_id` stays NULL and it is displayed as a
    standalone job. A series download creates exactly one `DownloadBatch`
    (`scope='series_all'`) plus one `DownloadJob` per episode, all pointing back
    here via `batch_id` (soft pointer, no hard FK — see `DownloadJob`).
    """

    __tablename__ = "download_batch"

    id = Column(Text, primary_key=True)             # uuid4 hex
    media_type = Column(Text, nullable=False)        # 'movie' | 'show' (selection type)
    unification_id = Column(Text)                    # back-nav to the unified title
    title = Column(Text, nullable=False)              # cleaned display title
    server_id = Column(Text, nullable=False)          # chosen source account
    rating_key = Column(Text, nullable=False)          # vod_* (movie) / series rk (show)
    scope = Column(Text, nullable=False)               # 'movie' | 'series_all'
    total_jobs = Column(Integer, nullable=False, default=0)  # number of jobs created
    created_at = Column(BigInteger, nullable=False)    # epoch ms


class DownloadJob(Base):
    """PH-DL-01: one physical media download — a movie or a single episode
    (docs/20-impl-media-download.md §3.2).

    State machine: queued -> running -> completed|failed|canceled (queued/running
    can also go straight to canceled). All transitions/progress writes go through
    `run_with_retry` at the service/worker layer (CR-C04, house-law piège 8) —
    not enforced by the schema itself.

    `dest_path` is ALWAYS relative to `settings.DOWNLOAD_DIR` (never an absolute
    path, never client-supplied) — confinement is proven at write time via
    `download_service.resolve_confined` (F-007). `batch_id` is a soft pointer
    (NULL for a standalone movie job) — deliberately not a hard FK: a
    `completed` job must remain re-enqueue-able later without a batch existing.
    No secret/credential column: the upstream Xtream URL (contains user/password)
    is re-derived at worker time from the stored account and never persisted here
    (spec §0.7 / house-law piège "secrets jamais loggés").
    """

    __tablename__ = "download_job"

    id = Column(Text, primary_key=True)               # uuid4 hex
    batch_id = Column(Text)                             # -> DownloadBatch.id, soft pointer, NULL for a movie
    server_id = Column(Text, nullable=False)            # source account ("xtream_<id>")
    rating_key = Column(Text, nullable=False)           # vod_{id}.{ext} (movie) / ep_{id}.{ext} (episode)
    media_type = Column(Text, nullable=False)           # 'movie' | 'episode' (job granularity = one file)
    unification_id = Column(Text)                        # for grouping/display only
    title = Column(Text, nullable=False)                 # cleaned display title
    season = Column(Integer)                              # episode only
    episode = Column(Integer)                             # episode only
    dest_path = Column(Text, nullable=False)             # RELATIVE to DOWNLOAD_DIR, never absolute
    state = Column(Text, nullable=False, default="queued")  # queued|running|completed|failed|canceled
    bytes_total = Column(BigInteger)                      # NULL if no upstream Content-Length
    bytes_done = Column(BigInteger, nullable=False, default=0)  # persisted progress (bytes written)
    error = Column(Text)                                   # bounded message, NEVER the URL (see _safe_error)
    attempts = Column(Integer, nullable=False, default=0)  # transient auto-retries consumed
    created_at = Column(BigInteger, nullable=False)         # epoch ms
    updated_at = Column(BigInteger, nullable=False)         # epoch ms, bumped on every transition/progress write
    started_at = Column(BigInteger)                          # epoch ms, first queued->running transition
    finished_at = Column(BigInteger)                          # epoch ms, completed/failed/canceled

    __table_args__ = (
        # Drain (`WHERE state='queued'`) + boot reap (`WHERE state='running'`).
        Index("ix_download_job_state", "state"),
        # Batch detail view (list a series' jobs).
        Index("ix_download_job_batch", "batch_id"),
        # Queue ordering (ORDER BY created_at) for the drain loop + admin list.
        Index("ix_download_job_created", "created_at"),
        # Idempotent-enqueue dedup: find a non-terminal job for (server_id, rating_key).
        # Deliberately NOT unique — a `completed` job must be re-enqueue-able later.
        Index("ix_download_job_item", "server_id", "rating_key"),
    )


class PlexServer(Base):
    """PH-PLEX-01: a Plex Media Server reachable via the account's plex.tv
    token (owned or shared), discovered against plex.tv/api/resources
    (docs/10-prd-media-download.md — feature "Télécharger Plex").

    `access_token` is a PER-SERVER secret (plex.tv issues a distinct token
    per resource) and is Fernet-encrypted at rest exactly like
    `XtreamAccount.password` (CR-S03, `EncryptedString`) — it must NEVER be
    exposed in an API response, the admin HTML, or a log line (house-law
    piège "secrets jamais loggés"). `base_uri` is the winning connection
    picked by the reachability probe and intentionally does NOT carry the
    token (Plex accepts it as a header/query param separately) so it is safe
    to display/log for diagnostics. This table is fully isolated from the
    `media` catalog — Plex items live in `PlexMediaItem` below and never
    enter `/api/media`, the unified/`.strm` generation, or Android-facing
    responses (that stays Xtream-only, spec §1).
    """

    __tablename__ = "plex_server"

    client_identifier = Column(Text, primary_key=True)   # plex.tv machine id, stable across renames
    name = Column(Text, nullable=False)                    # friendly server name (plex.tv "name")
    owner_title = Column(Text)                              # sourceTitle (sharer's name); NULL if owned by this account
    owned = Column(Boolean, nullable=False, default=False)  # True if this account owns the server
    access_token = Column(EncryptedString())                # per-server secret, encrypted at rest, NEVER exposed
    base_uri = Column(Text)                                  # winning probed connection URI, WITHOUT token
    is_reachable = Column(Boolean, nullable=False, default=False)  # last probe result
    last_synced_at = Column(BigInteger)                      # epoch ms, last successful catalogue sync
    last_sync_error = Column(Text)                            # bounded message, NEVER a token/URL
    created_at = Column(BigInteger, nullable=False)            # epoch ms
    updated_at = Column(BigInteger, nullable=False)            # epoch ms


class PlexMediaItem(Base):
    """PH-PLEX-01: lightweight mirror of `Media` for items pulled from a Plex
    shared server's catalogue (docs/10-prd-media-download.md).

    Deliberately a SEPARATE table from `media` — this catalogue is a download
    source only (never surfaced through the existing Xtream-facing
    `/api/media` endpoints or the .strm/NFO generator), so it carries its own
    schema rather than overloading `Media`'s composite key/columns. Primary
    key `(server_id, rating_key)` mirrors `Media`'s convention but is scoped
    per-server (a `rating_key` is only unique within one Plex server).

    Populated by a mark-and-sweep sync per server: every item touched in a
    run gets `synced_at` bumped to that run's timestamp; rows left behind
    (stale `synced_at`) are swept afterwards — same idempotent-upsert spirit
    as the Xtream `sync_worker`, but simpler (no differential page-offset
    eviction, CR-F02 does not apply here).
    """

    __tablename__ = "plex_media_item"

    server_id = Column(Text, primary_key=True)     # "plex_<client_identifier>"
    rating_key = Column(Text, primary_key=True)     # Plex ratingKey, scoped to this server

    type = Column(Text, nullable=False)              # 'movie' | 'show' | 'episode'
    title = Column(Text, nullable=False)
    year = Column(Integer)

    parent_rating_key = Column(Text)                  # episode -> season ratingKey (same server)
    grandparent_rating_key = Column(Text)              # episode -> show ratingKey (same server)
    parent_index = Column(Integer)                      # season number
    index = Column("index", Integer)                     # episode number; "index" is a SQL reserved word,
                                                           # quoted via the explicit column name below

    imdb_id = Column(Text)
    tmdb_id = Column(Text)
    tvdb_id = Column(Text)
    unification_id = Column(Text)                          # Android rule; populated for movies/shows post-sync, NULL on episodes

    thumb_url = Column(Text)                                 # PMS-relative path (e.g. /library/metadata/123/thumb), no token

    added_at = Column(BigInteger)                             # epoch ms (Plex addedAt)

    # Best Media[] element retained for this item (Plex may list several).
    height = Column(Integer)
    width = Column(Integer)
    video_codec = Column(Text)
    audio_codec = Column(Text)
    container = Column(Text)
    bitrate = Column(Integer)

    part_key = Column(Text)                                    # /library/parts/... — NOT a secret, no token embedded
    part_size = Column(BigInteger)                               # bytes
    duration_ms = Column(BigInteger)

    synced_at = Column(BigInteger, nullable=False)                # epoch ms, bumped every sync run (mark-and-sweep)

    __table_args__ = (
        # Resolve a show/movie's unified group for the download-source picker.
        Index("ix_plex_item_type_unif", "type", "unification_id"),
        # List a show's episodes (same server) for "download whole series".
        Index("ix_plex_item_show", "server_id", "grandparent_rating_key"),
        # Recency-ordered browse per type.
        Index("ix_plex_item_type_added", "type", "added_at"),
        # Title search/sort per type.
        Index("ix_plex_item_type_title", "type", "title"),
    )


class PlexSyncStatus(Base):
    """PH-PLEX-01: singleton row (id=1) tracking the Plex catalogue sync
    state, mirroring the master-only worker convention used by the download
    feature (house-law piège 7/17).

    Claimed by a conditional `UPDATE ... WHERE id=1 AND state='idle'` at the
    service layer (idle -> running) so two workers racing to start a sync
    can't both win; reaped back to idle at master boot if a previous process
    died mid-run (state left stuck on 'running').
    """

    __tablename__ = "plex_sync_status"

    id = Column(Integer, primary_key=True)          # always 1 (singleton)
    state = Column(Text, nullable=False, default="idle")  # 'idle' | 'running'
    started_at = Column(BigInteger)                          # epoch ms
    finished_at = Column(BigInteger)                          # epoch ms
    error = Column(Text)                                       # bounded message, never a token/URL

    # Enforce the singleton on a fresh DB too (create_all), converging with
    # migration 019's `CHECK (id = 1)` — otherwise the constraint only existed
    # on upgraded DBs (same fresh-vs-migration divergence class as CR-C05).
    __table_args__ = (CheckConstraint("id = 1", name="ck_plex_sync_status_singleton"),)
