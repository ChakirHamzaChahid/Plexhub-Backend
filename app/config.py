import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("plexhub.config")


class Settings:
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "./data"))
    LOG_DIR: Path = Path(os.getenv("LOG_DIR", "./logs"))
    DB_PATH: Path
    SYNC_INTERVAL_HOURS: int = int(os.getenv("SYNC_INTERVAL_HOURS", "6"))
    ENRICHMENT_DAILY_LIMIT: int = int(os.getenv("ENRICHMENT_DAILY_LIMIT", "50000"))
    HEALTH_CHECK_BATCH_SIZE: int = int(os.getenv("HEALTH_CHECK_BATCH_SIZE", "1000"))

    def __init__(self):
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.DB_PATH = self.DATA_DIR / "plexhub.db"

        if self.TMDB_API_KEY:
            logger.info(f"TMDB API Key loaded: {self.TMDB_API_KEY[:4]}****")
        else:
            logger.warning("TMDB_API_KEY not set — enrichment will be disabled")


settings = Settings()
