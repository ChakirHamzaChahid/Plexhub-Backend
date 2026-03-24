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
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "./data"))
    LOG_DIR: Path = Path(os.getenv("LOG_DIR", "./logs"))
    DB_PATH: Path
    SYNC_INTERVAL_HOURS: int = _safe_int("SYNC_INTERVAL_HOURS", 6)
    ENRICHMENT_DAILY_LIMIT: int = _safe_int("ENRICHMENT_DAILY_LIMIT", 50000)
    HEALTH_CHECK_BATCH_SIZE: int = _safe_int("HEALTH_CHECK_BATCH_SIZE", 1000)
    PLEX_LIBRARY_DIR: str = os.getenv("PLEX_LIBRARY_DIR", "")
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
    ]
    TMDB_LANGUAGE: str = os.getenv("TMDB_LANGUAGE", "fr-FR")

    # Xtream account auto-provisioning from env
    XTREAM_BASE_URL: str = os.getenv("XTREAM_BASE_URL", "")
    XTREAM_PORT: int = _safe_int("XTREAM_PORT", 80)
    XTREAM_USERNAME: str = os.getenv("XTREAM_USERNAME", "")
    XTREAM_PASSWORD: str = os.getenv("XTREAM_PASSWORD", "")

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


settings = Settings()
