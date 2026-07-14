import os
import logging
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


class Settings:
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
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

    @property
    def has_xtream_env(self) -> bool:
        return bool(self.XTREAM_BASE_URL and self.XTREAM_USERNAME and self.XTREAM_PASSWORD)

    def __init__(self):
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.DB_PATH = self.DATA_DIR / "plexhub.db"

        if self.TMDB_API_KEY:
            logger.info(f"TMDB API Key loaded: {self.TMDB_API_KEY[:4]}****")
        else:
            logger.warning("TMDB_API_KEY not set — enrichment will be disabled")

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


settings = Settings()
