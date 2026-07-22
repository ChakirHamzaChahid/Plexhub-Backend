import os
import logging
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("plexhub.config")


def _safe_int(env_var: str, default: int) -> int:
    """Parse env var as int with safe fallback and clear error message."""
    raw = os.getenv(env_var, str(default))
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning(f"Invalid integer for {env_var}={raw!r}, using default {default}")
        return default


def _safe_float(env_var: str, default: float) -> float:
    """Parse env var as float with safe fallback and clear error message."""
    raw = os.getenv(env_var, str(default))
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning(f"Invalid float for {env_var}={raw!r}, using default {default}")
        return default


class Settings:
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
    # OMDb (imdb-id consistency validator) — separate provider/key from TMDB,
    # used to cross-check imdb_id resolutions. "" = validator disabled.
    OMDB_API_KEY: str = os.getenv("OMDB_API_KEY", "")
    # User's plan = 100k requests/day; default keeps margin, env-overridable.
    OMDB_DAILY_LIMIT: int = _safe_int("OMDB_DAILY_LIMIT", 95000)
    AI_API_KEY: str = os.getenv("AI_API_KEY", "")

    # Admin web UI (/admin) — HTTP Basic Auth, separate from the X-API-Key
    # backend secret. Browser-friendly (custom headers can't be sent on a
    # navigation). When ADMIN_PASSWORD is empty the UI is fail-closed (503).
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")

    # TV pairing (device-flow) — Mission 18
    # Optional explicit Fernet key (urlsafe base64, 32 bytes) for payload
    # encryption at rest. When empty, a key is derived from AI_API_KEY.
    # When neither is set, tv-auth endpoints return 503.
    TV_AUTH_ENCRYPTION_KEY: str = os.getenv("TV_AUTH_ENCRYPTION_KEY", "")
    TV_AUTH_TTL_SECONDS: int = _safe_int("TV_AUTH_TTL_SECONDS", 900)  # 15 min

    # Xtream credential encryption at rest (CR-S03) — see
    # app/utils/crypto_fields.py for full key-resolution semantics.
    # Optional explicit Fernet key (urlsafe base64, 32 bytes) used to encrypt
    # XtreamAccount.password at rest. When empty, a key is derived from
    # AI_API_KEY (domain-separated from the tv-auth derivation above). When
    # neither is set, passwords are stored in PLAINTEXT (fail-open — see
    # crypto_fields.py docstring) rather than breaking account creation/sync.
    XTREAM_ENCRYPTION_KEY: str = os.getenv("XTREAM_ENCRYPTION_KEY", "")
    AI_EMBED_CACHE_DIR: str = os.getenv("AI_EMBED_CACHE_DIR", "")
    AI_EMBED_MODEL: str = os.getenv("AI_EMBED_MODEL", "")
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "./data"))
    LOG_DIR: Path = Path(os.getenv("LOG_DIR", "./logs"))
    DB_PATH: Path
    SYNC_INTERVAL_HOURS: int = _safe_int("SYNC_INTERVAL_HOURS", 6)
    ENRICHMENT_DAILY_LIMIT: int = _safe_int("ENRICHMENT_DAILY_LIMIT", 50000)
    HEALTH_CHECK_BATCH_SIZE: int = _safe_int("HEALTH_CHECK_BATCH_SIZE", 1000)

    # Stream Validation
    STREAM_VALIDATION_ENABLED: bool = os.getenv("STREAM_VALIDATION_ENABLED", "true").lower() in ("true", "1", "yes")
    STREAM_VALIDATION_CONCURRENCY: int = _safe_int("STREAM_VALIDATION_CONCURRENCY", 20)
    STREAM_VALIDATION_TIMEOUT: int = _safe_int("STREAM_VALIDATION_TIMEOUT", 15)
    STREAM_BROKEN_THRESHOLD: int = _safe_int("STREAM_BROKEN_THRESHOLD", 3)
    STREAM_VALIDATION_RECHECK_HOURS: int = _safe_int("STREAM_VALIDATION_RECHECK_HOURS", 24)
    STREAM_FILTER_BROKEN: bool = os.getenv("STREAM_FILTER_BROKEN", "true").lower() in ("true", "1", "yes")

    PLEX_LIBRARY_DIR: str = os.getenv("PLEX_LIBRARY_DIR", "")

    # DB backups (online sqlite .backup snapshots)
    BACKUP_ENABLED: bool = os.getenv("BACKUP_ENABLED", "true").lower() in ("true", "1", "yes")
    BACKUP_DIR: Path = Path(os.getenv("BACKUP_DIR", "./data/backups"))
    BACKUP_RETENTION_DAYS: int = _safe_int("BACKUP_RETENTION_DAYS", 7)
    BACKUP_HOUR: int = _safe_int("BACKUP_HOUR", 4)  # daily cron hour
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
    ]
    TMDB_LANGUAGE: str = os.getenv("TMDB_LANGUAGE", "fr-FR")

    # Ollama LLM (gemma4 via khoj-ollama)
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://khoj-ollama:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma4:e4b")

    # AI subtitle translation
    SUBTITLE_CHUNK_CUES: int = _safe_int("SUBTITLE_CHUNK_CUES", 20)          # cues per LLM call
    SUBTITLE_CONCURRENCY: int = _safe_int("SUBTITLE_CONCURRENCY", 4)         # max concurrent LLM calls
    SUBTITLE_MAX_CUES: int = _safe_int("SUBTITLE_MAX_CUES", 3000)            # reject above (413); long film ≈ 2500 cues
    SUBTITLE_MAX_BYTES: int = _safe_int("SUBTITLE_MAX_BYTES", 2000000)       # ~2 MB, reject above (413)
    SUBTITLE_PER_CHUNK_TIMEOUT: int = _safe_int("SUBTITLE_PER_CHUNK_TIMEOUT", 120)  # seconds per chunk
    SUBTITLE_TOTAL_TIMEOUT: int = _safe_int("SUBTITLE_TOTAL_TIMEOUT", 600)   # whole-request deadline (seconds)
    SUBTITLE_CACHE_RETENTION_DAYS: int = _safe_int("SUBTITLE_CACHE_RETENTION_DAYS", 30)  # 0 = keep forever

    # Xtream account auto-provisioning from env
    XTREAM_BASE_URL: str = os.getenv("XTREAM_BASE_URL", "")
    XTREAM_PORT: int = _safe_int("XTREAM_PORT", 80)
    XTREAM_USERNAME: str = os.getenv("XTREAM_USERNAME", "")
    XTREAM_PASSWORD: str = os.getenv("XTREAM_PASSWORD", "")
    # Some providers sit behind Cloudflare, which 403-blocks the default
    # python-httpx User-Agent. Send a real media-player UA on every request
    # to the provider (player_api.php and stream validation alike).
    XTREAM_USER_AGENT: str = os.getenv("XTREAM_USER_AGENT", "VLC/3.0.20 LibVLC/3.0.20")

    # Adult / X-rated content tagging.
    # Movies whose Xtream category name matches one of ADULT_CATEGORY_KEYWORDS
    # (or whose category_id is in ADULT_CATEGORY_IDS) are flagged is_adult,
    # get content_rating forced to ADULT_CONTENT_RATING (NFO <mpaa>), and are
    # prefixed in the API title (see api/media.py / schemas.py).
    ADULT_CONTENT_RATING: str = os.getenv("ADULT_CONTENT_RATING", "XXX")
    ADULT_CATEGORY_KEYWORDS: list[str] = [
        kw.strip().lower()
        for kw in os.getenv(
            "ADULT_CATEGORY_KEYWORDS", "adult,xxx,+18,18+,porn,porno,x-rated"
        ).split(",")
        if kw.strip()
    ]
    ADULT_CATEGORY_IDS: list[str] = [
        cid.strip()
        for cid in os.getenv("ADULT_CATEGORY_IDS", "").split(",")
        if cid.strip()
    ]

    # Physical media download (feature "Télécharger") — separate from
    # PLEX_LIBRARY_DIR; the .strm catalogue is untouched by this feature.
    # docs/20-impl-media-download.md §2. "" = feature disabled (config guard
    # in download_service.enqueue_selection / download_worker.run_drain_loop).
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "")
    DOWNLOAD_CONCURRENCY: int = _safe_int("DOWNLOAD_CONCURRENCY", 1)
    DOWNLOAD_CHUNK_BYTES: int = _safe_int("DOWNLOAD_CHUNK_BYTES", 1_048_576)        # 1 MiB
    DOWNLOAD_MAX_RETRIES: int = _safe_int("DOWNLOAD_MAX_RETRIES", 3)               # transient auto-retries
    DOWNLOAD_MIN_FREE_DISK_MB: int = _safe_int("DOWNLOAD_MIN_FREE_DISK_MB", 2048)  # préflight, see download_service.check_free_disk_space (<=0 disables)
    DOWNLOAD_POLL_INTERVAL: int = _safe_int("DOWNLOAD_POLL_INTERVAL", 2)           # worker drain poll (s)
    DOWNLOAD_CONNECT_TIMEOUT: int = _safe_int("DOWNLOAD_CONNECT_TIMEOUT", 30)      # httpx connect (s)
    DOWNLOAD_READ_TIMEOUT: int = _safe_int("DOWNLOAD_READ_TIMEOUT", 120)           # httpx read/chunk (s)
    # Max HTTP redirects a download will follow. Xtream stream URLs legitimately
    # 302 to a CDN host, so downloads must follow — but each hop's target is
    # verified to resolve to a PUBLIC IP first (DL-01 SSRF guard). Set to 0 to
    # restore the old strict behaviour (any 3xx = permanent failure).
    DOWNLOAD_MAX_REDIRECTS: int = _safe_int("DOWNLOAD_MAX_REDIRECTS", 5)
    # Per-source ceiling for the unified "Téléchargements" screen's in-memory
    # cross-catalogue merge (Xtream + Plex). Beyond this a source's window is
    # truncated and the UI says so (never a silent cap). The admin screen is
    # search/genre-driven in practice, so the default is comfortable.
    UNIFIED_DOWNLOAD_MERGE_CAP: int = _safe_int("UNIFIED_DOWNLOAD_MERGE_CAP", 5000)

    # Plex shared-servers download source (feature "Télécharger Plex").
    # "" = feature disabled (no plex.tv discovery, no catalogue sync). The
    # per-server accessTokens fetched from plex.tv are stored encrypted
    # (PlexServer.access_token, EncryptedString). Isolated from the `media`
    # table — Plex items never enter the Android API / .strm generation.
    PLEX_ACCOUNT_TOKEN: str = os.getenv("PLEX_ACCOUNT_TOKEN", "")
    PLEX_PROBE_TIMEOUT: int = _safe_int("PLEX_PROBE_TIMEOUT", 5)          # per-connection probe (s)
    PLEX_SYNC_INTERVAL_HOURS: int = _safe_int("PLEX_SYNC_INTERVAL_HOURS", 0)  # 0 = no periodic cron (manual only)
    # Stable UUID sent as X-Plex-Client-Identifier to plex.tv. Resolved in
    # __init__: explicit env value wins; otherwise read/generate-and-persist
    # a uuid4 under DATA_DIR/plex_client_id so it survives restarts (plex.tv
    # ties resource visibility to this identifier). Class attribute below is
    # only for typing/defaults — the real value is always set in __init__.
    PLEX_CLIENT_IDENTIFIER: str = ""

    # WebDAV virtual filesystem for Plex (see docs/30-ops-plex-webdav.md).
    # Plex ignores .strm files at scan time, so a read-only WebDAV tree is
    # exposed at /dav (rclone-mounted on the same host) presenting Xtream
    # content as regular video files; bytes are relayed on GET with Range
    # pass-through. Additive/feature-gated: DAV_ENABLED=false by default, and
    # even when true /dav fail-closes with 503 until DAV_PASSWORD is set
    # (same fail-closed convention as ADMIN_PASSWORD above).
    DAV_ENABLED: bool = os.getenv("DAV_ENABLED", "false").lower() in ("true", "1", "yes")
    DAV_USERNAME: str = os.getenv("DAV_USERNAME", "plexdav")
    DAV_PASSWORD: str = os.getenv("DAV_PASSWORD", "")
    DAV_MOVIE_LIMIT: int = _safe_int("DAV_MOVIE_LIMIT", 25)    # 0 = unlimited
    DAV_SERIES_LIMIT: int = _safe_int("DAV_SERIES_LIMIT", 5)   # 0 = unlimited
    # csv of account ids; empty = every active account (mirrors DatabaseSource
    # default in app/plex_generator/source.py).
    DAV_ACCOUNT_IDS: list[str] = [
        aid.strip() for aid in os.getenv("DAV_ACCOUNT_IDS", "").split(",") if aid.strip()
    ]
    DAV_INCLUDE_ADULT: bool = os.getenv("DAV_INCLUDE_ADULT", "false").lower() in ("true", "1", "yes")
    DAV_SINGLE_VERSION: bool = os.getenv("DAV_SINGLE_VERSION", "true").lower() in ("true", "1", "yes")
    # A version without a known file_size is excluded — an unknown/wrong size
    # breaks rclone's VFS layer (Content-Length drives its read-ahead/cache).
    DAV_REQUIRE_KNOWN_SIZE: bool = os.getenv("DAV_REQUIRE_KNOWN_SIZE", "true").lower() in ("true", "1", "yes")
    DAV_TREE_TTL_MINUTES: int = _safe_int("DAV_TREE_TTL_MINUTES", 60)
    DAV_UPSTREAM_PER_ACCOUNT: int = _safe_int("DAV_UPSTREAM_PER_ACCOUNT", 1)
    DAV_QUEUE_TIMEOUT_SECONDS: int = _safe_int("DAV_QUEUE_TIMEOUT_SECONDS", 30)
    DAV_RANGE_SHIM: bool = os.getenv("DAV_RANGE_SHIM", "true").lower() in ("true", "1", "yes")
    # Reuse the physical-download HTTP timeouts by default (same upstream
    # class of Xtream provider connections) — independently env-overridable.
    DAV_CONNECT_TIMEOUT: float = _safe_float("DAV_CONNECT_TIMEOUT", float(DOWNLOAD_CONNECT_TIMEOUT))
    DAV_READ_TIMEOUT: float = _safe_float("DAV_READ_TIMEOUT", float(DOWNLOAD_READ_TIMEOUT))

    @property
    def has_xtream_env(self) -> bool:
        return bool(self.XTREAM_BASE_URL and self.XTREAM_USERNAME and self.XTREAM_PASSWORD)

    def __init__(self):
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.DB_PATH = self.DATA_DIR / "plexhub.db"

        env_client_id = os.getenv("PLEX_CLIENT_IDENTIFIER", "")
        if env_client_id:
            self.PLEX_CLIENT_IDENTIFIER = env_client_id
        else:
            client_id_path = self.DATA_DIR / "plex_client_id"
            try:
                if client_id_path.exists():
                    self.PLEX_CLIENT_IDENTIFIER = client_id_path.read_text(encoding="utf-8").strip()
                if not self.PLEX_CLIENT_IDENTIFIER:
                    self.PLEX_CLIENT_IDENTIFIER = uuid.uuid4().hex
                    client_id_path.write_text(self.PLEX_CLIENT_IDENTIFIER, encoding="utf-8")
            except OSError as exc:
                # DATA_DIR unwritable (read-only mount, permissions, ...):
                # fall back to a per-process id rather than crashing boot —
                # the Plex download source stays functional, just without a
                # stable identifier across restarts.
                logger.warning("Could not read/persist plex_client_id (%s) — using a transient id", exc)
                self.PLEX_CLIENT_IDENTIFIER = uuid.uuid4().hex

        if self.TMDB_API_KEY:
            logger.info(f"TMDB API Key loaded: {self.TMDB_API_KEY[:4]}****")
        else:
            logger.warning("TMDB_API_KEY not set — enrichment will be disabled")

        if self.OMDB_API_KEY:
            logger.info(f"OMDb API Key loaded: {self.OMDB_API_KEY[:4]}****")
        else:
            logger.warning("OMDB_API_KEY not set — imdb-id consistency validator will be disabled")

        logger.info(f"Ollama LLM: {self.OLLAMA_URL} / model={self.OLLAMA_MODEL}")
        logger.info(
            f"Adult tagging: rating={self.ADULT_CONTENT_RATING!r}, "
            f"keywords={self.ADULT_CATEGORY_KEYWORDS}, "
            f"explicit_ids={self.ADULT_CATEGORY_IDS}"
        )

        if self.DOWNLOAD_DIR:
            logger.info(
                f"Physical download: dir={self.DOWNLOAD_DIR!r}, "
                f"concurrency={self.DOWNLOAD_CONCURRENCY}"
            )
        else:
            logger.info("Physical download: DOWNLOAD_DIR not set — feature disabled")

        if self.PLEX_ACCOUNT_TOKEN:
            logger.info("Plex download source: enabled (client_id=%s…)", self.PLEX_CLIENT_IDENTIFIER[:8])
        else:
            logger.info("Plex download source: PLEX_ACCOUNT_TOKEN not set — feature disabled")

        if self.DAV_ENABLED:
            logger.info(
                "DAV WebDAV: enabled=%s movies=%s series=%s accounts=%s single_version=%s",
                self.DAV_ENABLED,
                self.DAV_MOVIE_LIMIT or "unlimited",
                self.DAV_SERIES_LIMIT or "unlimited",
                self.DAV_ACCOUNT_IDS or "all-active",
                self.DAV_SINGLE_VERSION,
            )
        else:
            logger.info("DAV WebDAV: disabled (DAV_ENABLED=false)")


settings = Settings()
