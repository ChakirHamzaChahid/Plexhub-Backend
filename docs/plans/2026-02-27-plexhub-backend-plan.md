# PlexHub Backend Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a FastAPI backend that syncs Xtream IPTV catalogs, enriches them with TMDB metadata, and serves them to PlexHub TV Android for aggregation with Plex content via unificationId.

**Architecture:** Single-container FastAPI app with SQLite WAL, three background workers (sync, enrichment, health check), and a REST API serving camelCase JSON. No auth (LAN-only). APScheduler for periodic tasks.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async + aiosqlite), httpx, Pydantic v2, APScheduler, rapidfuzz, Docker.

---

## Task 1: Project Scaffold — Config, Dependencies, Docker

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `app/__init__.py`
- Create: `app/config.py`

**Step 1: Create `requirements.txt`**

```
fastapi>=0.115
uvicorn[standard]>=0.27
sqlalchemy[asyncio]>=2.0
aiosqlite>=0.20
httpx>=0.27
pydantic>=2.6
pydantic-settings>=2.1
apscheduler>=3.10
rapidfuzz>=3.6
python-dotenv
```

**Step 2: Create `.env.example`**

```
TMDB_API_KEY=your_tmdb_api_key_here
SYNC_INTERVAL_HOURS=6
ENRICHMENT_DAILY_LIMIT=50000
HEALTH_CHECK_BATCH_SIZE=1000
```

**Step 3: Create `Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN mkdir -p /app/data /app/logs
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Step 4: Create `docker-compose.yml`**

```yaml
services:
  backend:
    build: .
    container_name: plexhub-backend
    ports:
      - "8000:8000"
    environment:
      - TMDB_API_KEY=${TMDB_API_KEY}
      - SYNC_INTERVAL_HOURS=6
      - ENRICHMENT_DAILY_LIMIT=50000
      - HEALTH_CHECK_BATCH_SIZE=1000
      - TZ=Europe/Paris
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '1.0'
```

**Step 5: Create `app/__init__.py`**

Empty file.

**Step 6: Create `app/config.py`**

```python
import os
import logging
from pathlib import Path

logger = logging.getLogger("plexhub.config")


class Settings:
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "/app/data"))
    LOG_DIR: Path = Path(os.getenv("LOG_DIR", "/app/logs"))
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
```

**Step 7: Verify scaffold**

Run: `pip install -r requirements.txt`
Expected: All packages install successfully.

**Step 8: Commit**

```bash
git add requirements.txt .env.example Dockerfile docker-compose.yml app/__init__.py app/config.py
git commit -m "feat: project scaffold with config, Docker, dependencies"
```

---

## Task 2: Database Layer — Engine, Session Factory, ORM Models

**Files:**
- Create: `app/db/__init__.py`
- Create: `app/db/database.py`
- Create: `app/models/__init__.py`
- Create: `app/models/database.py`

**Step 1: Create `app/db/__init__.py`**

Empty file.

**Step 2: Create `app/db/database.py`**

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings

DATABASE_URL = f"sqlite+aiosqlite:///{settings.DB_PATH}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    """FastAPI dependency for DB sessions."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    """Create all tables and enable WAL mode."""
    from app.models.database import Base

    async with engine.begin() as conn:
        await conn.execute(
            __import__("sqlalchemy").text("PRAGMA journal_mode=WAL")
        )
        await conn.run_sync(Base.metadata.create_all)
```

**Step 3: Create `app/models/__init__.py`**

Empty file.

**Step 4: Create `app/models/database.py`**

This is the core ORM model mirroring Android's MediaEntity schema 33.

```python
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

    # Backend-specific
    stream_error_count = Column(Integer, nullable=False, default=0)
    last_stream_check = Column(BigInteger)
    is_broken = Column(Boolean, nullable=False, default=False)
    tmdb_match_confidence = Column(Float)

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

    __table_args__ = (
        Index("ix_enrichment_status", "status"),
        Index("uix_enrichment_item", "rating_key", "server_id", unique=True),
    )
```

**Step 5: Verify DB creation**

Create a quick test script or verify via Python REPL:

```bash
python -c "
import asyncio
from app.db.database import init_db
asyncio.run(init_db())
print('DB initialized successfully')
"
```

Expected: `DB initialized successfully`, and `data/plexhub.db` file exists.

**Step 6: Commit**

```bash
git add app/db/ app/models/
git commit -m "feat: database layer with SQLAlchemy ORM models (media, accounts, enrichment_queue)"
```

---

## Task 3: Utility Functions — String Normalizer, Unification, Helpers

**Files:**
- Create: `app/utils/__init__.py`
- Create: `app/utils/string_normalizer.py`
- Create: `app/utils/unification.py`

**Step 1: Create `app/utils/__init__.py`**

Empty file.

**Step 2: Create `app/utils/string_normalizer.py`**

```python
import re
import unicodedata


def parse_title_and_year(raw: str) -> tuple[str, int | None]:
    """
    Parse IPTV title, stripping prefixes and extracting year.

    Input:  "|VM| Le Monde apres nous (2023)"
    Output: ("Le Monde apres nous", 2023)
    """
    # Strip IPTV prefixes: |XX|, |XX XX|, [XX], etc.
    title = re.sub(r"^\|[^|]+\|\s*", "", raw)
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)

    # Extract year from (YYYY) at end of title
    year_match = re.search(r"\((\d{4})\)\s*$", title)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        title = title[: year_match.start()].strip()

    return title.strip() or "Unknown", year


def normalize_for_sorting(title: str) -> str:
    """
    Match Android StringNormalizer.normalizeForSorting().
    Strips leading articles and removes diacritics.
    """
    # Remove leading articles
    lower = title.lower()
    for article in [
        "the ", "a ", "an ",
        "le ", "la ", "les ", "l'",
        "un ", "une ",
    ]:
        if lower.startswith(article):
            title = title[len(article):]
            break

    # Normalize unicode (remove accents)
    nfkd = unicodedata.normalize("NFKD", title)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def parse_rating(value) -> float | None:
    """Safely parse a rating value to float."""
    if value is None:
        return None
    try:
        val = float(value)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None
```

**Step 3: Create `app/utils/unification.py`**

```python
import re
from app.utils.string_normalizer import normalize_for_sorting


def calculate_unification_id(
    title: str,
    year: int | None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
) -> str:
    """
    Priority: imdb > tmdb > title_year.
    Must match Android MediaMapper logic exactly.
    """
    if imdb_id:
        return f"imdb://{imdb_id}"
    if tmdb_id:
        return f"tmdb://{tmdb_id}"
    # Fallback: normalized title + year
    if title == "Unknown":
        return ""
    normalized = normalize_for_sorting(title).lower()
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"[^a-z0-9_]", "", normalized)
    return f"title_{normalized}_{year}" if year else f"title_{normalized}"


def calculate_history_group_key(
    unification_id: str,
    rating_key: str,
    server_id: str,
) -> str:
    return unification_id if unification_id else f"{rating_key}{server_id}"


def calculate_display_rating(
    scraped_rating: float | None,
    audience_rating: float | None,
    rating: float | None,
) -> float:
    """COALESCE(scrapedRating, audienceRating, rating, 0.0) — matches Android."""
    return scraped_rating or audience_rating or rating or 0.0
```

**Step 4: Commit**

```bash
git add app/utils/
git commit -m "feat: utility functions for title parsing, unification ID, and normalization"
```

---

## Task 4: Pydantic Schemas — Request/Response Models

**Files:**
- Create: `app/models/schemas.py`

**Step 1: Create `app/models/schemas.py`**

```python
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
```

**Step 2: Commit**

```bash
git add app/models/schemas.py
git commit -m "feat: Pydantic schemas with camelCase aliases for Android API compatibility"
```

---

## Task 5: Xtream Service — API Client

**Files:**
- Create: `app/services/__init__.py`
- Create: `app/services/xtream_service.py`

**Step 1: Create `app/services/__init__.py`**

Empty file.

**Step 2: Create `app/services/xtream_service.py`**

```python
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("plexhub.xtream")


class XtreamService:
    """Client for Xtream Codes player_api.php endpoints."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _build_base_url(self, base_url: str, port: int) -> str:
        url = base_url.rstrip("/")
        is_default = (
            (url.startswith("http://") and port == 80)
            or (url.startswith("https://") and port == 443)
        )
        return f"{url}/" if is_default else f"{url}:{port}/"

    def _api_url(self, base_url: str, port: int) -> str:
        return f"{self._build_base_url(base_url, port)}player_api.php"

    async def _get(
        self, base_url: str, port: int, username: str, password: str,
        action: str | None = None, **extra_params,
    ) -> dict[str, Any]:
        client = await self._get_client()
        params = {"username": username, "password": password}
        if action:
            params["action"] = action
        params.update(extra_params)

        url = self._api_url(base_url, port)
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def authenticate(self, account) -> dict[str, Any]:
        """Authenticate and get account info."""
        data = await self._get(
            account.base_url, account.port,
            account.username, account.password,
        )
        return data

    async def get_vod_categories(self, account) -> list[dict]:
        data = await self._get(
            account.base_url, account.port,
            account.username, account.password,
            action="get_vod_categories",
        )
        return data if isinstance(data, list) else []

    async def get_vod_streams(
        self, account, category_id: str | None = None,
    ) -> list[dict]:
        kwargs = {}
        if category_id:
            kwargs["category_id"] = category_id
        data = await self._get(
            account.base_url, account.port,
            account.username, account.password,
            action="get_vod_streams",
            **kwargs,
        )
        return data if isinstance(data, list) else []

    async def get_vod_info(self, account, vod_id: int) -> dict:
        data = await self._get(
            account.base_url, account.port,
            account.username, account.password,
            action="get_vod_info",
            vod_id=str(vod_id),
        )
        return data if isinstance(data, dict) else {}

    async def get_series_categories(self, account) -> list[dict]:
        data = await self._get(
            account.base_url, account.port,
            account.username, account.password,
            action="get_series_categories",
        )
        return data if isinstance(data, list) else []

    async def get_series(
        self, account, category_id: str | None = None,
    ) -> list[dict]:
        kwargs = {}
        if category_id:
            kwargs["category_id"] = category_id
        data = await self._get(
            account.base_url, account.port,
            account.username, account.password,
            action="get_series",
            **kwargs,
        )
        return data if isinstance(data, list) else []

    async def get_series_info(self, account, series_id: int) -> dict:
        data = await self._get(
            account.base_url, account.port,
            account.username, account.password,
            action="get_series_info",
            series_id=str(series_id),
        )
        return data if isinstance(data, dict) else {}

    def build_movie_url(
        self, base_url: str, port: int, username: str, password: str,
        stream_id: int, extension: str,
    ) -> str:
        base = self._build_base_url(base_url, port)
        return f"{base}movie/{username}/{password}/{stream_id}.{extension}"

    def build_episode_url(
        self, base_url: str, port: int, username: str, password: str,
        episode_id: str, extension: str,
    ) -> str:
        base = self._build_base_url(base_url, port)
        return f"{base}series/{username}/{password}/{episode_id}.{extension}"


# Singleton
xtream_service = XtreamService()
```

**Step 3: Commit**

```bash
git add app/services/
git commit -m "feat: Xtream API client service (auth, VOD, series, URL building)"
```

---

## Task 6: Stream Service — URL Resolution from ratingKey

**Files:**
- Create: `app/services/stream_service.py`

**Step 1: Create `app/services/stream_service.py`**

```python
import re
import logging
from typing import Optional

from app.services.xtream_service import xtream_service

logger = logging.getLogger("plexhub.stream")


def parse_rating_key(rating_key: str) -> dict:
    """
    Parse a rating_key into its components.

    vod_435071.mp4 -> {"type": "movie", "id": "435071", "ext": "mp4"}
    ep_7890.mkv    -> {"type": "episode", "id": "7890", "ext": "mkv"}
    vod_435071     -> {"type": "movie", "id": "435071", "ext": None}
    series_6581    -> {"type": "series", "id": "6581", "ext": None}
    """
    if rating_key.startswith("vod_"):
        remainder = rating_key[4:]
        parts = remainder.rsplit(".", 1)
        stream_id = parts[0]
        ext = parts[1] if len(parts) > 1 else None
        return {"type": "movie", "id": stream_id, "ext": ext}
    elif rating_key.startswith("ep_"):
        remainder = rating_key[3:]
        parts = remainder.rsplit(".", 1)
        ep_id = parts[0]
        ext = parts[1] if len(parts) > 1 else None
        return {"type": "episode", "id": ep_id, "ext": ext}
    elif rating_key.startswith("series_"):
        return {"type": "series", "id": rating_key[7:], "ext": None}
    elif rating_key.startswith("season_"):
        return {"type": "season", "id": rating_key[7:], "ext": None}
    else:
        return {"type": "unknown", "id": rating_key, "ext": None}


def build_stream_url(account, rating_key: str) -> Optional[str]:
    """Build the direct stream URL for a given media item."""
    parsed = parse_rating_key(rating_key)

    if parsed["type"] == "movie":
        ext = parsed["ext"] or "ts"
        return xtream_service.build_movie_url(
            account.base_url, account.port,
            account.username, account.password,
            int(parsed["id"]), ext,
        )
    elif parsed["type"] == "episode":
        ext = parsed["ext"] or "ts"
        return xtream_service.build_episode_url(
            account.base_url, account.port,
            account.username, account.password,
            parsed["id"], ext,
        )
    else:
        logger.warning(f"Cannot build stream URL for type: {parsed['type']}")
        return None
```

**Step 2: Commit**

```bash
git add app/services/stream_service.py
git commit -m "feat: stream service for resolving ratingKey to stream URL"
```

---

## Task 7: Media Service — Business Logic for Queries

**Files:**
- Create: `app/services/media_service.py`

**Step 1: Create `app/services/media_service.py`**

```python
import logging
from typing import Optional

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Media

logger = logging.getLogger("plexhub.media")


class MediaService:

    async def get_media_list(
        self,
        db: AsyncSession,
        media_type: str,
        limit: int = 500,
        offset: int = 0,
        sort: str = "added_desc",
        server_id: Optional[str] = None,
        parent_rating_key: Optional[str] = None,
    ) -> tuple[list[Media], int]:
        """Get paginated media list with total count."""
        query = select(Media).where(Media.type == media_type)

        if server_id:
            query = query.where(Media.server_id == server_id)
        if parent_rating_key:
            query = query.where(Media.parent_rating_key == parent_rating_key)

        # Count total
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        # Apply sorting
        if sort == "added_desc":
            query = query.order_by(Media.added_at.desc())
        elif sort == "added_asc":
            query = query.order_by(Media.added_at.asc())
        elif sort == "title_asc":
            query = query.order_by(Media.title_sortable.asc())
        elif sort == "title_desc":
            query = query.order_by(Media.title_sortable.desc())
        elif sort == "rating_desc":
            query = query.order_by(Media.display_rating.desc())
        elif sort == "year_desc":
            query = query.order_by(Media.year.desc().nulls_last())
        else:
            query = query.order_by(Media.added_at.desc())

        # Apply pagination
        query = query.offset(offset).limit(limit)

        result = await db.execute(query)
        items = list(result.scalars().all())

        return items, total

    async def get_media_by_key(
        self,
        db: AsyncSession,
        rating_key: str,
        server_id: str,
    ) -> Optional[Media]:
        """Get a single media item by its composite key."""
        result = await db.execute(
            select(Media).where(
                Media.rating_key == rating_key,
                Media.server_id == server_id,
            ).limit(1)
        )
        return result.scalars().first()

    async def get_stats(self, db: AsyncSession) -> dict:
        """Get media statistics for health endpoint."""
        total_result = await db.execute(select(func.count()).select_from(Media))
        total = total_result.scalar() or 0

        enriched_result = await db.execute(
            select(func.count()).select_from(
                select(Media).where(Media.tmdb_id.isnot(None)).subquery()
            )
        )
        enriched = enriched_result.scalar() or 0

        broken_result = await db.execute(
            select(func.count()).select_from(
                select(Media).where(Media.is_broken == True).subquery()
            )
        )
        broken = broken_result.scalar() or 0

        return {
            "total_media": total,
            "enriched_media": enriched,
            "broken_streams": broken,
        }


media_service = MediaService()
```

**Step 2: Commit**

```bash
git add app/services/media_service.py
git commit -m "feat: media service with paginated queries and stats"
```

---

## Task 8: Sync Worker — Xtream Catalog to DB

**Files:**
- Create: `app/workers/__init__.py`
- Create: `app/workers/sync_worker.py`

**Step 1: Create `app/workers/__init__.py`**

Empty file.

**Step 2: Create `app/workers/sync_worker.py`**

```python
import asyncio
import logging
import time

from sqlalchemy import select, delete, update
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from app.db.database import async_session_factory
from app.models.database import Media, XtreamAccount, EnrichmentQueue
from app.services.xtream_service import xtream_service
from app.utils.string_normalizer import (
    parse_title_and_year,
    normalize_for_sorting,
    parse_rating,
)
from app.utils.unification import (
    calculate_unification_id,
    calculate_history_group_key,
    calculate_display_rating,
)

logger = logging.getLogger("plexhub.sync")

# In-memory sync job tracking
_sync_jobs: dict[str, dict] = {}


def now_ms() -> int:
    return int(time.time() * 1000)


def map_vod_to_media(dto: dict, account_id: str, index: int) -> dict:
    """Map Xtream VOD stream DTO to media row dict."""
    title, year = parse_title_and_year(dto.get("name") or "Unknown")
    ext = (dto.get("container_extension") or "").strip() or None
    stream_id = dto["stream_id"]
    rating_key = f"vod_{stream_id}.{ext}" if ext else f"vod_{stream_id}"
    server_id = f"xtream_{account_id}"
    rating_val = parse_rating(dto.get("rating"))

    unification_id = calculate_unification_id(title, year)
    return {
        "rating_key": rating_key,
        "server_id": server_id,
        "library_section_id": "xtream_vod",
        "title": title,
        "title_sortable": normalize_for_sorting(title).lower(),
        "filter": str(dto.get("category_id", "all")),
        "sort_order": "default",
        "page_offset": index,
        "type": "movie",
        "thumb_url": dto.get("stream_icon"),
        "resolved_thumb_url": dto.get("stream_icon"),
        "year": year,
        "rating": rating_val,
        "display_rating": rating_val or 0.0,
        "added_at": int(dto.get("added") or 0) * 1000,  # seconds -> ms
        "updated_at": now_ms(),
        "unification_id": unification_id,
        "history_group_key": calculate_history_group_key(
            unification_id, rating_key, server_id
        ),
        "media_parts": "[]",
    }


def map_series_to_media(dto: dict, account_id: str, index: int) -> dict:
    """Map Xtream series DTO to media row dict."""
    title, year = parse_title_and_year(dto.get("name") or "Unknown")
    series_id = dto["series_id"]
    rating_key = f"series_{series_id}"
    server_id = f"xtream_{account_id}"
    rating_val = parse_rating(dto.get("rating"))
    backdrop = dto.get("backdrop_path")

    unification_id = calculate_unification_id(title, year)
    return {
        "rating_key": rating_key,
        "server_id": server_id,
        "library_section_id": "xtream_series",
        "title": title,
        "title_sortable": normalize_for_sorting(title).lower(),
        "filter": str(dto.get("category_id", "all")),
        "sort_order": "default",
        "page_offset": index,
        "type": "show",
        "thumb_url": dto.get("cover"),
        "art_url": backdrop[0] if isinstance(backdrop, list) and backdrop else None,
        "resolved_thumb_url": dto.get("cover"),
        "resolved_art_url": backdrop[0] if isinstance(backdrop, list) and backdrop else None,
        "year": year,
        "summary": dto.get("plot"),
        "genres": dto.get("genre"),
        "duration": (int(dto["episode_run_time"]) * 60_000)
        if dto.get("episode_run_time")
        else None,
        "rating": rating_val,
        "display_rating": rating_val or 0.0,
        "added_at": now_ms(),
        "updated_at": now_ms(),
        "unification_id": unification_id,
        "history_group_key": calculate_history_group_key(
            unification_id, rating_key, server_id
        ),
        "media_parts": "[]",
    }


def map_episode_to_media(
    episode: dict, series_dto: dict, account_id: str, season_num: int,
) -> dict:
    """Map Xtream episode DTO to media row dict."""
    series_id = series_dto["series_id"]
    ep_id = str(episode.get("id", ""))
    ext = (episode.get("container_extension") or "").strip() or None
    rating_key = f"ep_{ep_id}.{ext}" if ext else f"ep_{ep_id}"
    server_id = f"xtream_{account_id}"
    ep_num = episode.get("episode_num")
    info = episode.get("info") or {}

    rating_val = parse_rating(info.get("rating"))

    return {
        "rating_key": rating_key,
        "server_id": server_id,
        "library_section_id": "xtream_series",
        "title": episode.get("title") or f"Episode {ep_num}",
        "title_sortable": (episode.get("title") or f"Episode {ep_num}").lower(),
        "filter": "all",
        "sort_order": "default",
        "page_offset": int(ep_id) if ep_id.isdigit() else (ep_num or 0),
        "type": "episode",
        "thumb_url": info.get("movie_image"),
        "resolved_thumb_url": info.get("movie_image"),
        "year": None,
        "summary": info.get("plot"),
        "duration": (int(info["duration_secs"]) * 1000)
        if info.get("duration_secs")
        else None,
        "parent_rating_key": f"season_{series_id}_{season_num}",
        "parent_title": f"Season {season_num}",
        "parent_index": season_num,
        "grandparent_rating_key": f"series_{series_id}",
        "grandparent_title": series_dto.get("name"),
        "index": ep_num,
        "rating": rating_val,
        "display_rating": rating_val or 0.0,
        "added_at": now_ms(),
        "updated_at": now_ms(),
        "unification_id": "",
        "history_group_key": f"{rating_key}{server_id}",
        "media_parts": "[]",
    }


async def upsert_media_batch(db, rows: list[dict]):
    """Bulk upsert media rows using SQLite INSERT OR REPLACE."""
    if not rows:
        return
    for row in rows:
        stmt = sqlite_upsert(Media).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["rating_key", "server_id", "filter", "sort_order"],
            set_={
                k: v for k, v in row.items()
                if k not in ("rating_key", "server_id", "filter", "sort_order",
                             "view_offset", "view_count", "last_viewed_at")
            },
        )
        await db.execute(stmt)


async def enqueue_for_enrichment(db, rows: list[dict]):
    """Insert items into enrichment_queue if not already present."""
    for row in rows:
        if row["type"] not in ("movie", "show"):
            continue
        stmt = sqlite_upsert(EnrichmentQueue).values(
            rating_key=row["rating_key"],
            server_id=row["server_id"],
            media_type=row["type"],
            title=row["title"],
            year=row.get("year"),
            status="pending",
            attempts=0,
            created_at=now_ms(),
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["rating_key", "server_id"]
        )
        await db.execute(stmt)


async def differential_cleanup(
    db, server_id: str, filter_val: str, api_rating_keys: set[str],
):
    """Remove DB items not present in the API response (delisted content)."""
    result = await db.execute(
        select(Media.rating_key).where(
            Media.server_id == server_id,
            Media.filter == filter_val,
        )
    )
    existing_keys = {row[0] for row in result}
    stale_keys = existing_keys - api_rating_keys

    if stale_keys:
        await db.execute(
            delete(Media).where(
                Media.rating_key.in_(stale_keys),
                Media.server_id == server_id,
            )
        )
        logger.info(
            f"Removed {len(stale_keys)} stale items from {server_id}/{filter_val}"
        )


async def sync_account(account_id: str):
    """Full sync for a single Xtream account."""
    job_id = f"sync_{account_id}_{now_ms()}"
    _sync_jobs[job_id] = {"status": "processing", "progress": {}}

    try:
        async with async_session_factory() as db:
            # Load account
            result = await db.execute(
                select(XtreamAccount).where(
                    XtreamAccount.id == account_id,
                    XtreamAccount.is_active == True,
                )
            )
            account = result.scalars().first()
            if not account:
                logger.warning(f"Account {account_id} not found or inactive")
                _sync_jobs[job_id]["status"] = "failed"
                return job_id

            server_id = f"xtream_{account_id}"
            total_synced = 0

            # --- VOD Sync ---
            logger.info(f"Syncing VOD for account {account_id}")
            try:
                vod_streams = await xtream_service.get_vod_streams(account)
            except Exception as e:
                logger.error(f"Failed to fetch VOD streams: {e}")
                vod_streams = []

            vod_rows = [
                map_vod_to_media(dto, account_id, i)
                for i, dto in enumerate(vod_streams)
            ]

            if vod_rows:
                await upsert_media_batch(db, vod_rows)
                vod_keys = {r["rating_key"] for r in vod_rows}
                await differential_cleanup(db, server_id, "all", vod_keys)
                await enqueue_for_enrichment(db, vod_rows)
                total_synced += len(vod_rows)
                logger.info(f"Synced {len(vod_rows)} VOD items")

            # --- Series Sync ---
            logger.info(f"Syncing Series for account {account_id}")
            try:
                series_list = await xtream_service.get_series(account)
            except Exception as e:
                logger.error(f"Failed to fetch series: {e}")
                series_list = []

            series_rows = [
                map_series_to_media(dto, account_id, i)
                for i, dto in enumerate(series_list)
            ]

            if series_rows:
                await upsert_media_batch(db, series_rows)
                series_keys = {r["rating_key"] for r in series_rows}
                await differential_cleanup(db, server_id, "all", series_keys)
                await enqueue_for_enrichment(db, series_rows)
                total_synced += len(series_rows)
                logger.info(f"Synced {len(series_rows)} series items")

            # --- Episodes Sync (for each series) ---
            episode_count = 0
            for series_dto in series_list:
                try:
                    series_info = await xtream_service.get_series_info(
                        account, series_dto["series_id"]
                    )
                    episodes_data = series_info.get("episodes") or {}

                    for season_str, episodes in episodes_data.items():
                        season_num = int(season_str)
                        for ep in episodes:
                            ep_row = map_episode_to_media(
                                ep, series_dto, account_id, season_num
                            )
                            await upsert_media_batch(db, [ep_row])
                            episode_count += 1

                    await asyncio.sleep(0.05)  # Rate limit
                except Exception as e:
                    logger.error(
                        f"Failed to sync series {series_dto.get('series_id')}: {e}"
                    )

            total_synced += episode_count
            logger.info(f"Synced {episode_count} episodes")

            # Update account last_synced_at
            await db.execute(
                update(XtreamAccount)
                .where(XtreamAccount.id == account_id)
                .values(last_synced_at=now_ms())
            )

            await db.commit()

            _sync_jobs[job_id] = {
                "status": "completed",
                "progress": {"total": total_synced, "synced": total_synced},
            }
            logger.info(
                f"Sync complete for account {account_id}: {total_synced} items"
            )

    except Exception as e:
        logger.error(f"Sync failed for account {account_id}: {e}")
        _sync_jobs[job_id] = {"status": "failed", "progress": {"error": str(e)}}

    return job_id


async def run_all_accounts():
    """Sync all active accounts."""
    logger.info("Starting catalog sync for all accounts")
    async with async_session_factory() as db:
        result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.is_active == True)
        )
        accounts = result.scalars().all()

    for account in accounts:
        await sync_account(account.id)

    logger.info("All accounts sync complete")


def get_sync_job(job_id: str) -> dict | None:
    return _sync_jobs.get(job_id)
```

**Step 3: Commit**

```bash
git add app/workers/
git commit -m "feat: sync worker with VOD/series/episode mapping and differential cleanup"
```

---

## Task 9: TMDB Service — Search and External IDs

**Files:**
- Create: `app/services/tmdb_service.py`

**Step 1: Create `app/services/tmdb_service.py`**

```python
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from rapidfuzz import fuzz

from app.config import settings
from app.utils.string_normalizer import normalize_for_sorting

logger = logging.getLogger("plexhub.tmdb")


@dataclass
class TMDBMatch:
    tmdb_id: int
    title: str
    year: int | None
    confidence: float


class TMDBService:
    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=10.0,
                headers={"Authorization": f"Bearer {settings.TMDB_API_KEY}"},
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @property
    def is_configured(self) -> bool:
        return bool(settings.TMDB_API_KEY)

    async def search_movie(
        self, title: str, year: int | None,
    ) -> TMDBMatch | None:
        if not self.is_configured:
            return None
        client = await self._get_client()
        params: dict = {"query": title, "language": "en-US"}
        if year:
            params["year"] = year
        resp = await client.get(f"{self.BASE_URL}/search/movie", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return self._best_match(results, title, year, title_key="title", date_key="release_date")

    async def search_tv(
        self, title: str, year: int | None,
    ) -> TMDBMatch | None:
        if not self.is_configured:
            return None
        client = await self._get_client()
        params: dict = {"query": title, "language": "en-US"}
        if year:
            params["first_air_date_year"] = year
        resp = await client.get(f"{self.BASE_URL}/search/tv", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return self._best_match(results, title, year, title_key="name", date_key="first_air_date")

    async def get_movie_external_ids(self, tmdb_id: int) -> dict:
        """Get IMDb ID from TMDB."""
        client = await self._get_client()
        resp = await client.get(f"{self.BASE_URL}/movie/{tmdb_id}/external_ids")
        resp.raise_for_status()
        return resp.json()

    async def get_tv_external_ids(self, tmdb_id: int) -> dict:
        client = await self._get_client()
        resp = await client.get(f"{self.BASE_URL}/tv/{tmdb_id}/external_ids")
        resp.raise_for_status()
        return resp.json()

    def _best_match(
        self,
        results: list[dict],
        title: str,
        year: int | None,
        title_key: str,
        date_key: str,
    ) -> TMDBMatch | None:
        """Fuzzy match by title + year, return best with confidence score."""
        if not results:
            return None

        normalized_query = normalize_for_sorting(title).lower()
        best: TMDBMatch | None = None
        best_confidence = 0.0

        for r in results[:10]:  # Check top 10 results
            r_title = r.get(title_key, "")
            r_normalized = normalize_for_sorting(r_title).lower()

            # Title similarity (0-100 from rapidfuzz, normalize to 0-1)
            title_sim = fuzz.ratio(normalized_query, r_normalized) / 100.0

            # Year factor
            r_date = r.get(date_key, "")
            r_year = int(r_date[:4]) if r_date and len(r_date) >= 4 else None
            year_factor = 1.0
            if year and r_year:
                if year == r_year:
                    year_factor = 1.0
                elif abs(year - r_year) <= 1:
                    year_factor = 0.95
                else:
                    year_factor = 0.85
            elif year and not r_year:
                year_factor = 0.9

            confidence = title_sim * year_factor

            if confidence > best_confidence:
                best_confidence = confidence
                best = TMDBMatch(
                    tmdb_id=r["id"],
                    title=r_title,
                    year=r_year,
                    confidence=confidence,
                )

        # Threshold: >= 0.85
        if best and best.confidence >= 0.85:
            return best
        return None


# Singleton
tmdb_service = TMDBService()
```

**Step 2: Commit**

```bash
git add app/services/tmdb_service.py
git commit -m "feat: TMDB service with fuzzy search matching and external IDs"
```

---

## Task 10: Enrichment Worker — Batch TMDB Enrichment

**Files:**
- Create: `app/workers/enrichment_worker.py`

**Step 1: Create `app/workers/enrichment_worker.py`**

```python
import asyncio
import logging
import time

from sqlalchemy import select, update

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, EnrichmentQueue, XtreamAccount
from app.services.tmdb_service import tmdb_service
from app.services.xtream_service import xtream_service

logger = logging.getLogger("plexhub.enrichment")


def now_ms() -> int:
    return int(time.time() * 1000)


async def _get_account_for_server(db, server_id: str) -> object | None:
    """Extract account_id from server_id and load account."""
    if not server_id.startswith("xtream_"):
        return None
    account_id = server_id[7:]
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    return result.scalars().first()


async def _enrich_vod_item(db, item, account):
    """Enrich a single VOD movie item. Returns TMDB API calls used."""
    used = 0
    tmdb_id = None
    imdb_id = None
    confidence = None

    # Step 1: Try Xtream get_vod_info (free, may have tmdb_id)
    try:
        vod_id_str = item.rating_key.split("_")[1].split(".")[0]
        vod_id = int(vod_id_str)
        vod_info = await xtream_service.get_vod_info(account, vod_id)
        info = vod_info.get("info") or {}
        raw_tmdb = info.get("tmdb_id")

        if raw_tmdb and str(raw_tmdb).strip():
            tmdb_id = int(raw_tmdb)
            # Get imdb_id from TMDB
            ext_ids = await tmdb_service.get_movie_external_ids(tmdb_id)
            imdb_id = ext_ids.get("imdb_id")
            used += 1
            confidence = 1.0

            # Also update metadata from Xtream response
            updates = {}
            if info.get("plot"):
                updates["summary"] = info["plot"]
            if info.get("genre"):
                updates["genres"] = info["genre"]
            if info.get("duration") and not item.rating_key:
                pass  # duration already set
            if updates:
                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                    )
                    .values(**updates)
                )
    except Exception as e:
        logger.debug(f"Xtream vod_info failed for {item.rating_key}: {e}")

    # Step 2: Fallback to TMDB search if no tmdb_id yet
    if not tmdb_id and tmdb_service.is_configured:
        try:
            match = await tmdb_service.search_movie(item.title, item.year)
            if match and match.confidence >= 0.85:
                tmdb_id = match.tmdb_id
                ext_ids = await tmdb_service.get_movie_external_ids(tmdb_id)
                imdb_id = ext_ids.get("imdb_id")
                confidence = match.confidence
                used += 1
        except Exception as e:
            logger.debug(f"TMDB search failed for {item.title}: {e}")

    # Update media if we found IDs
    if tmdb_id:
        new_unification = f"imdb://{imdb_id}" if imdb_id else f"tmdb://{tmdb_id}"
        await db.execute(
            update(Media)
            .where(
                Media.rating_key == item.rating_key,
                Media.server_id == item.server_id,
            )
            .values(
                tmdb_id=str(tmdb_id),
                imdb_id=imdb_id,
                unification_id=new_unification,
                history_group_key=new_unification,
                tmdb_match_confidence=confidence,
            )
        )
        item.status = "done"
    else:
        item.status = "skipped"

    item.attempts += 1
    item.processed_at = now_ms()
    return used


async def _enrich_series_item(db, item):
    """Enrich a single series item via TMDB search. Returns API calls used."""
    used = 0

    if not tmdb_service.is_configured:
        item.status = "skipped"
        item.attempts += 1
        item.processed_at = now_ms()
        return 0

    try:
        match = await tmdb_service.search_tv(item.title, item.year)
        if match and match.confidence >= 0.85:
            tmdb_id = match.tmdb_id
            ext_ids = await tmdb_service.get_tv_external_ids(tmdb_id)
            imdb_id = ext_ids.get("imdb_id")
            used += 1

            new_unification = (
                f"imdb://{imdb_id}" if imdb_id else f"tmdb://{tmdb_id}"
            )
            await db.execute(
                update(Media)
                .where(
                    Media.rating_key == item.rating_key,
                    Media.server_id == item.server_id,
                )
                .values(
                    tmdb_id=str(tmdb_id),
                    imdb_id=imdb_id,
                    unification_id=new_unification,
                    history_group_key=new_unification,
                    tmdb_match_confidence=match.confidence,
                )
            )
            item.status = "done"
        else:
            item.status = "skipped"
    except Exception as e:
        item.status = "failed"
        item.last_error = str(e)
        logger.error(f"TMDB enrichment failed for series '{item.title}': {e}")

    item.attempts += 1
    item.processed_at = now_ms()
    return used


async def run():
    """Run enrichment batch for all pending items."""
    daily_limit = settings.ENRICHMENT_DAILY_LIMIT
    used = 0

    logger.info(f"Starting enrichment batch (daily limit: {daily_limit})")

    async with async_session_factory() as db:
        # Phase 1: VOD movies
        result = await db.execute(
            select(EnrichmentQueue)
            .where(
                EnrichmentQueue.status == "pending",
                EnrichmentQueue.media_type == "movie",
            )
            .order_by(EnrichmentQueue.created_at)
            .limit(daily_limit)
        )
        pending_vod = list(result.scalars().all())

        for item in pending_vod:
            if used >= daily_limit:
                break
            try:
                account = await _get_account_for_server(db, item.server_id)
                if not account:
                    item.status = "skipped"
                    item.attempts += 1
                    item.processed_at = now_ms()
                    continue
                calls = await _enrich_vod_item(db, item, account)
                used += calls
            except Exception as e:
                item.status = "failed"
                item.last_error = str(e)
                item.attempts += 1
                logger.error(f"Enrichment error for {item.rating_key}: {e}")

            await asyncio.sleep(0.03)  # ~33 req/s rate limit

        # Phase 2: Series
        remaining = daily_limit - used
        if remaining > 0:
            result = await db.execute(
                select(EnrichmentQueue)
                .where(
                    EnrichmentQueue.status == "pending",
                    EnrichmentQueue.media_type == "show",
                )
                .order_by(EnrichmentQueue.created_at)
                .limit(remaining)
            )
            pending_series = list(result.scalars().all())

            for item in pending_series:
                if used >= daily_limit:
                    break
                try:
                    calls = await _enrich_series_item(db, item)
                    used += calls
                except Exception as e:
                    item.status = "failed"
                    item.last_error = str(e)
                    item.attempts += 1
                    logger.error(f"Enrichment error for {item.rating_key}: {e}")

                await asyncio.sleep(0.03)

        await db.commit()

    logger.info(f"Enrichment batch complete: {used} TMDB API calls used")
```

**Step 2: Commit**

```bash
git add app/workers/enrichment_worker.py
git commit -m "feat: enrichment worker with Xtream+TMDB fallback and daily budget"
```

---

## Task 11: Health Check Worker — Stream Probing

**Files:**
- Create: `app/workers/health_check_worker.py`

**Step 1: Create `app/workers/health_check_worker.py`**

```python
import asyncio
import logging
import time

import httpx
from sqlalchemy import select, update, func, or_

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, XtreamAccount
from app.services.stream_service import build_stream_url

logger = logging.getLogger("plexhub.health_check")


def now_ms() -> int:
    return int(time.time() * 1000)


async def run():
    """Check a batch of stream URLs for availability."""
    batch_size = settings.HEALTH_CHECK_BATCH_SIZE
    cutoff = now_ms() - 7 * 24 * 3600 * 1000  # 7 days ago

    logger.info(f"Starting health check (batch size: {batch_size})")

    async with async_session_factory() as db:
        # Get random batch of streams not checked in 7 days
        result = await db.execute(
            select(Media)
            .where(
                Media.server_id.like("xtream_%"),
                Media.type.in_(["movie", "episode"]),
                or_(
                    Media.last_stream_check.is_(None),
                    Media.last_stream_check < cutoff,
                ),
            )
            .order_by(func.random())
            .limit(batch_size)
        )
        items = list(result.scalars().all())

        if not items:
            logger.info("No streams to check")
            return

        # Cache accounts
        accounts: dict[str, object] = {}
        checked = 0
        broken_count = 0

        async with httpx.AsyncClient(timeout=5.0) as client:
            for item in items:
                # Get account for this item
                account_id = item.server_id.replace("xtream_", "")
                if account_id not in accounts:
                    acc_result = await db.execute(
                        select(XtreamAccount).where(
                            XtreamAccount.id == account_id
                        )
                    )
                    accounts[account_id] = acc_result.scalars().first()

                account = accounts.get(account_id)
                if not account:
                    continue

                url = build_stream_url(account, item.rating_key)
                if not url:
                    continue

                is_broken = False
                try:
                    resp = await client.head(url, follow_redirects=True)
                    is_broken = resp.status_code >= 400
                except (httpx.TimeoutException, httpx.ConnectError):
                    is_broken = True
                except Exception:
                    is_broken = True

                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                    )
                    .values(
                        is_broken=is_broken,
                        last_stream_check=now_ms(),
                        stream_error_count=(
                            Media.stream_error_count + (1 if is_broken else 0)
                        ),
                    )
                )

                checked += 1
                if is_broken:
                    broken_count += 1

                await asyncio.sleep(0.05)  # 50ms between probes

        await db.commit()

    logger.info(
        f"Health check complete: {checked} checked, {broken_count} broken"
    )
```

**Step 2: Commit**

```bash
git add app/workers/health_check_worker.py
git commit -m "feat: health check worker with HTTP HEAD stream probing"
```

---

## Task 12: API Endpoints — Health

**Files:**
- Create: `app/api/__init__.py`
- Create: `app/api/health.py`

**Step 1: Create `app/api/__init__.py`**

Empty file.

**Step 2: Create `app/api/health.py`**

```python
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.database import XtreamAccount
from app.models.schemas import HealthResponse
from app.services.media_service import media_service

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db)):
    stats = await media_service.get_stats(db)

    # Count accounts
    acc_result = await db.execute(
        select(func.count()).select_from(XtreamAccount)
    )
    account_count = acc_result.scalar() or 0

    # Last sync time
    last_sync_result = await db.execute(
        select(func.max(XtreamAccount.last_synced_at))
    )
    last_sync = last_sync_result.scalar()

    return HealthResponse(
        status="ok",
        version="1.0.0",
        accounts=account_count,
        total_media=stats["total_media"],
        enriched_media=stats["enriched_media"],
        broken_streams=stats["broken_streams"],
        last_sync_at=last_sync,
    )
```

**Step 3: Commit**

```bash
git add app/api/
git commit -m "feat: health API endpoint"
```

---

## Task 13: API Endpoints — Accounts CRUD

**Files:**
- Create: `app/api/accounts.py`

**Step 1: Create `app/api/accounts.py`**

```python
import asyncio
import hashlib
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.database import XtreamAccount
from app.models.schemas import (
    AccountCreate,
    AccountUpdate,
    AccountResponse,
    AccountTestResponse,
)
from app.services.xtream_service import xtream_service

logger = logging.getLogger("plexhub.api.accounts")
router = APIRouter(prefix="/accounts", tags=["accounts"])


def _generate_account_id(base_url: str, username: str) -> str:
    raw = f"{base_url}{username}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def now_ms() -> int:
    return int(time.time() * 1000)


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(XtreamAccount))
    return result.scalars().all()


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    body: AccountCreate, db: AsyncSession = Depends(get_db),
):
    account_id = _generate_account_id(body.base_url, body.username)

    # Check if exists
    existing = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    if existing.scalars().first():
        raise HTTPException(409, "Account already exists")

    # Create account object for auth test
    class TempAccount:
        pass
    temp = TempAccount()
    temp.base_url = body.base_url
    temp.port = body.port
    temp.username = body.username
    temp.password = body.password

    # Authenticate with Xtream
    try:
        auth_data = await xtream_service.authenticate(temp)
        user_info = auth_data.get("user_info", {})
        server_info = auth_data.get("server_info", {})
    except Exception as e:
        raise HTTPException(400, f"Authentication failed: {e}")

    account = XtreamAccount(
        id=account_id,
        label=body.label,
        base_url=body.base_url,
        port=body.port,
        username=body.username,
        password=body.password,
        status=user_info.get("status", "Unknown"),
        expiration_date=int(user_info["exp_date"]) * 1000
        if user_info.get("exp_date")
        else None,
        max_connections=int(user_info.get("max_connections", 1)),
        allowed_formats=",".join(user_info.get("allowed_output_formats", [])),
        server_url=server_info.get("url"),
        https_port=int(server_info["https_port"])
        if server_info.get("https_port")
        else None,
        is_active=True,
        created_at=now_ms(),
    )

    db.add(account)
    await db.flush()

    # Trigger initial sync in background
    from app.workers.sync_worker import sync_account
    asyncio.create_task(sync_account(account_id))

    return account


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str, body: AccountUpdate, db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    update_data = body.model_dump(exclude_unset=True)
    if update_data:
        await db.execute(
            update(XtreamAccount)
            .where(XtreamAccount.id == account_id)
            .values(**update_data)
        )
        await db.flush()

    # Reload
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    return result.scalars().first()


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: str, db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    if not result.scalars().first():
        raise HTTPException(404, "Account not found")

    # Delete account and its media
    from app.models.database import Media, EnrichmentQueue

    server_id = f"xtream_{account_id}"
    await db.execute(delete(Media).where(Media.server_id == server_id))
    await db.execute(
        delete(EnrichmentQueue).where(EnrichmentQueue.server_id == server_id)
    )
    await db.execute(
        delete(XtreamAccount).where(XtreamAccount.id == account_id)
    )


@router.post("/{account_id}/test", response_model=AccountTestResponse)
async def test_account(
    account_id: str, db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    try:
        auth_data = await xtream_service.authenticate(account)
        user_info = auth_data.get("user_info", {})

        return AccountTestResponse(
            status=user_info.get("status", "Unknown"),
            expiration_date=int(user_info["exp_date"]) * 1000
            if user_info.get("exp_date")
            else None,
            max_connections=int(user_info.get("max_connections", 1)),
            allowed_formats=",".join(
                user_info.get("allowed_output_formats", [])
            ),
        )
    except Exception as e:
        raise HTTPException(400, f"Connection test failed: {e}")
```

**Step 2: Commit**

```bash
git add app/api/accounts.py
git commit -m "feat: accounts CRUD API with Xtream auth validation"
```

---

## Task 14: API Endpoints — Media

**Files:**
- Create: `app/api/media.py`

**Step 1: Create `app/api/media.py`**

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.database import get_db
from app.models.schemas import MediaResponse, MediaListResponse
from app.services.media_service import media_service

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/movies", response_model=MediaListResponse)
async def list_movies(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("added_desc"),
    server_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="movie", limit=limit, offset=offset,
        sort=sort, server_id=server_id,
    )
    return MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/shows", response_model=MediaListResponse)
async def list_shows(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("added_desc"),
    server_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="show", limit=limit, offset=offset,
        sort=sort, server_id=server_id,
    )
    return MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/episodes", response_model=MediaListResponse)
async def list_episodes(
    parent_rating_key: str = Query(...),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    server_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="episode", limit=limit, offset=offset,
        server_id=server_id, parent_rating_key=parent_rating_key,
    )
    return MediaListResponse(
        items=[MediaResponse.model_validate(i) for i in items],
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/{rating_key}", response_model=MediaResponse)
async def get_media(
    rating_key: str,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    item = await media_service.get_media_by_key(db, rating_key, server_id)
    if not item:
        raise HTTPException(404, "Media not found")
    return MediaResponse.model_validate(item)
```

**Step 2: Commit**

```bash
git add app/api/media.py
git commit -m "feat: media API endpoints with pagination and filtering"
```

---

## Task 15: API Endpoints — Stream

**Files:**
- Create: `app/api/stream.py`

**Step 1: Create `app/api/stream.py`**

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.database import XtreamAccount
from app.models.schemas import StreamResponse
from app.services.stream_service import build_stream_url

router = APIRouter(tags=["stream"])


@router.get("/stream/{rating_key}", response_model=StreamResponse)
async def get_stream(
    rating_key: str,
    server_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    # Extract account_id from server_id
    if not server_id.startswith("xtream_"):
        raise HTTPException(400, "Invalid server_id format")

    account_id = server_id[7:]

    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    url = build_stream_url(account, rating_key)
    if not url:
        raise HTTPException(400, f"Cannot build stream URL for: {rating_key}")

    return StreamResponse(url=url)
```

**Step 2: Commit**

```bash
git add app/api/stream.py
git commit -m "feat: stream API endpoint for resolving stream URLs"
```

---

## Task 16: API Endpoints — Sync

**Files:**
- Create: `app/api/sync.py`

**Step 1: Create `app/api/sync.py`**

```python
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.schemas import SyncRequest, SyncStatusResponse
from app.workers.sync_worker import sync_account, run_all_accounts, get_sync_job

logger = logging.getLogger("plexhub.api.sync")
router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/xtream", status_code=202)
async def trigger_sync(body: SyncRequest):
    """Trigger sync for a specific account."""
    task = asyncio.create_task(sync_account(body.account_id))

    # Return a job ID (we'll use a simple approach)
    job_id = f"sync_{body.account_id}_{id(task)}"
    return {"jobId": job_id}


@router.post("/xtream/all", status_code=202)
async def trigger_sync_all():
    """Trigger sync for all active accounts."""
    task = asyncio.create_task(run_all_accounts())
    job_id = f"sync_all_{id(task)}"
    return {"jobId": job_id}


@router.get("/status/{job_id}", response_model=SyncStatusResponse)
async def get_sync_status(job_id: str):
    """Check sync job status."""
    job = get_sync_job(job_id)
    if not job:
        return SyncStatusResponse(status="unknown")
    return SyncStatusResponse(
        status=job["status"],
        progress=job.get("progress"),
    )
```

**Step 2: Commit**

```bash
git add app/api/sync.py
git commit -m "feat: sync API endpoints for triggering and monitoring sync jobs"
```

---

## Task 17: Main App — FastAPI Lifespan with Scheduler

**Files:**
- Create: `app/main.py`

**Step 1: Create `app/main.py`**

```python
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import settings
from app.db.database import init_db
from app.api import accounts, health, media, stream, sync

logger = logging.getLogger("plexhub")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Master Worker election via lock file."""
    lock_file = settings.DATA_DIR / "server_start.lock"
    is_master = False
    scheduler = None

    try:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Initialize database
        await init_db()
        logger.info("Database initialized")

        # Master election
        try:
            with open(lock_file, "x") as f:
                f.write(str(os.getpid()))
            is_master = True
        except FileExistsError:
            if time.time() - lock_file.stat().st_mtime > 1200:
                lock_file.unlink(missing_ok=True)
                with open(lock_file, "x") as f:
                    f.write(str(os.getpid()))
                is_master = True
            else:
                is_master = False

        if is_master:
            logger.info(f"[Worker {os.getpid()}] Master — Starting scheduler")

            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from app.workers import sync_worker, enrichment_worker, health_check_worker

            scheduler = AsyncIOScheduler()
            scheduler.add_job(
                sync_worker.run_all_accounts,
                "interval",
                hours=settings.SYNC_INTERVAL_HOURS,
                id="xtream_sync",
            )
            scheduler.add_job(
                enrichment_worker.run,
                "interval",
                hours=settings.SYNC_INTERVAL_HOURS,
                id="tmdb_enrichment",
            )
            scheduler.add_job(
                health_check_worker.run,
                "cron",
                hour=2,
                id="health_check",
            )
            scheduler.start()

            # Non-blocking initial sync
            asyncio.create_task(sync_worker.run_all_accounts())
        else:
            logger.info(f"[Worker {os.getpid()}] Slave — Passive mode")

        yield

    finally:
        if is_master:
            lock_file.unlink(missing_ok=True)
            if scheduler:
                scheduler.shutdown(wait=False)

        # Close service clients
        from app.services.xtream_service import xtream_service
        from app.services.tmdb_service import tmdb_service

        await xtream_service.close()
        await tmdb_service.close()


app = FastAPI(
    title="PlexHub Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Routes
app.include_router(health.router, prefix="/api")
app.include_router(accounts.router, prefix="/api")
app.include_router(media.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(sync.router, prefix="/api")
```

**Step 2: Verify the full app starts**

```bash
cd plexhub-backend
DATA_DIR=./data LOG_DIR=./logs uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Expected: Server starts, DB initialized, scheduler running.

**Step 3: Smoke test health endpoint**

```bash
curl http://localhost:8000/api/health
```

Expected: `{"status":"ok","version":"1.0.0","accounts":0,"totalMedia":0,"enrichedMedia":0,"brokenStreams":0,"lastSyncAt":null}`

**Step 4: Commit**

```bash
git add app/main.py
git commit -m "feat: FastAPI main app with lifespan, scheduler, and all routes"
```

---

## Task 18: Docker Verification — Build and Run

**Step 1: Build Docker image**

```bash
docker compose build
```

Expected: Image builds successfully.

**Step 2: Start container**

```bash
docker compose up -d
```

Expected: Container starts, logs show "Database initialized" and "Master — Starting scheduler".

**Step 3: Verify health endpoint**

```bash
curl http://localhost:8000/api/health
```

Expected: JSON response with status "ok".

**Step 4: Verify API docs**

Open `http://localhost:8000/docs` in browser.
Expected: Swagger UI with all endpoints listed.

**Step 5: Commit (if any adjustments needed)**

```bash
git add -A
git commit -m "chore: Docker build verification and adjustments"
```

---

## Task 19: Integration Test — Full Flow

**Step 1: Add a test account**

```bash
curl -X POST http://localhost:8000/api/accounts \
  -H "Content-Type: application/json" \
  -d '{"label":"Test IPTV","baseUrl":"http://your-xtream-host","port":80,"username":"user","password":"pass"}'
```

Expected: 201 response with account details.

**Step 2: Trigger sync**

```bash
curl -X POST http://localhost:8000/api/sync/xtream \
  -H "Content-Type: application/json" \
  -d '{"accountId":"<id-from-step-1>"}'
```

Expected: 202 with jobId.

**Step 3: Check media**

```bash
curl "http://localhost:8000/api/media/movies?limit=10"
```

Expected: JSON with items array containing synced VOD movies.

**Step 4: Test stream URL**

```bash
curl "http://localhost:8000/api/stream/<ratingKey>?server_id=xtream_<accountId>"
```

Expected: JSON with url field containing the direct stream URL.

**Step 5: Verify health stats**

```bash
curl http://localhost:8000/api/health
```

Expected: totalMedia > 0 after sync.

---

## Summary

| Task | Description | Dependencies |
|------|-------------|-------------|
| 1 | Project scaffold (config, Docker, deps) | — |
| 2 | Database layer (engine, ORM models) | 1 |
| 3 | Utility functions (normalizer, unification) | — |
| 4 | Pydantic schemas | — |
| 5 | Xtream service (API client) | 1 |
| 6 | Stream service (URL builder) | 5 |
| 7 | Media service (queries) | 2 |
| 8 | Sync worker (catalog→DB) | 2, 3, 5 |
| 9 | TMDB service (search) | 1 |
| 10 | Enrichment worker | 2, 5, 9 |
| 11 | Health check worker | 2, 6 |
| 12 | Health API endpoint | 7 |
| 13 | Accounts API endpoint | 2, 5 |
| 14 | Media API endpoint | 7 |
| 15 | Stream API endpoint | 6 |
| 16 | Sync API endpoint | 8 |
| 17 | Main app (lifespan + router) | 12-16 |
| 18 | Docker verification | 17 |
| 19 | Integration test | 18 |
