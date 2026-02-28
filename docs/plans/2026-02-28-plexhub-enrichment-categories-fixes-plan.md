# PlexHub Enrichment & Category Filtering Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix enrichment logic, add category filtering per account, and fix series-episode relationship queries.

**Architecture:** Extend database with xtream_categories table, add category management API endpoints, modify sync/enrichment workers to filter by category and enrich when TMDB OR IMDB is missing.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), aiosqlite, Pydantic v2

---

## Task 1: Database Migrations — Add New Tables and Columns

**Files:**
- Create: `app/db/migrations.py`
- Modify: `app/models/database.py`

**Step 1: Add XtreamCategory ORM model to database.py**

Add after the `XtreamAccount` class:

```python
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
        Index("idx_categories_account", "account_id", "is_allowed"),
        Index("idx_categories_type", "category_type", "is_allowed"),
        Index("uix_categories_unique", "account_id", "category_id", "category_type", unique=True),
    )
```

**Step 2: Add new columns to existing models**

In the `XtreamAccount` class, add after `created_at`:

```python
    category_filter_mode = Column(Text, nullable=False, default="all")
```

In the `Media` class, add after `tmdb_match_confidence`:

```python
    is_in_allowed_categories = Column(Boolean, nullable=False, default=True)
```

Add index after existing `__table_args__`:

```python
        Index("idx_media_category_visibility", "is_in_allowed_categories", "type", "added_at"),
```

In the `EnrichmentQueue` class, add after `created_at`:

```python
    existing_tmdb_id = Column(Text)
    existing_imdb_id = Column(Text)
```

**Step 3: Create migration script**

Create `app/db/migrations.py`:

```python
"""
Database migrations for category filtering and enrichment improvements.
Run this once to update existing database schema.
"""
import asyncio
import logging
from sqlalchemy import text
from app.db.database import engine
from app.models.database import Base

logger = logging.getLogger("plexhub.migrations")


async def run_migrations():
    """Apply schema migrations to existing database."""

    async with engine.begin() as conn:
        logger.info("Starting database migrations...")

        # Check if xtream_categories table exists
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='xtream_categories'"
        ))
        table_exists = result.fetchone() is not None

        if not table_exists:
            logger.info("Creating xtream_categories table...")
            await conn.run_sync(Base.metadata.create_all, tables=[Base.metadata.tables['xtream_categories']])

        # Add columns to existing tables
        try:
            await conn.execute(text(
                "ALTER TABLE xtream_accounts ADD COLUMN category_filter_mode TEXT NOT NULL DEFAULT 'all'"
            ))
            logger.info("Added category_filter_mode to xtream_accounts")
        except Exception as e:
            logger.info(f"category_filter_mode already exists: {e}")

        try:
            await conn.execute(text(
                "ALTER TABLE media ADD COLUMN is_in_allowed_categories BOOLEAN NOT NULL DEFAULT TRUE"
            ))
            logger.info("Added is_in_allowed_categories to media")
        except Exception as e:
            logger.info(f"is_in_allowed_categories already exists: {e}")

        try:
            await conn.execute(text(
                "ALTER TABLE enrichment_queue ADD COLUMN existing_tmdb_id TEXT"
            ))
            logger.info("Added existing_tmdb_id to enrichment_queue")
        except Exception as e:
            logger.info(f"existing_tmdb_id already exists: {e}")

        try:
            await conn.execute(text(
                "ALTER TABLE enrichment_queue ADD COLUMN existing_imdb_id TEXT"
            ))
            logger.info("Added existing_imdb_id to enrichment_queue")
        except Exception as e:
            logger.info(f"existing_imdb_id already exists: {e}")

        # Create indexes
        try:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_media_category_visibility ON media(is_in_allowed_categories, type, added_at)"
            ))
            logger.info("Created idx_media_category_visibility index")
        except Exception as e:
            logger.info(f"Index creation skipped: {e}")

        logger.info("Database migrations completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migrations())
```

**Step 4: Run migrations**

Run: `python -m app.db.migrations`
Expected: "Database migrations completed successfully"

**Step 5: Commit**

```bash
git add app/models/database.py app/db/migrations.py
git commit -m "feat: add database schema for category filtering

- Add XtreamCategory model
- Add category_filter_mode to xtream_accounts
- Add is_in_allowed_categories to media
- Add existing_tmdb_id and existing_imdb_id to enrichment_queue
- Create migration script"
```

---

## Task 2: Category Schemas — Pydantic Models for API

**Files:**
- Modify: `app/models/schemas.py`

**Step 1: Add category schemas**

Add at the end of `app/models/schemas.py`:

```python
# --- Category Schemas ---

class CategoryResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    category_id: str
    category_name: str
    category_type: str
    is_allowed: bool
    last_fetched_at: int


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    category_id: str
    category_type: str
    is_allowed: bool


class CategoriesConfigRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    filter_mode: str  # "all", "whitelist", "blacklist"
    categories: list[CategoryUpdate]


class CategoriesResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    vod: list[CategoryResponse]
    series: list[CategoryResponse]
    filter_mode: str
```

**Step 2: Update MediaListResponse to include filter info**

No changes needed for now - keep it simple.

**Step 3: Commit**

```bash
git add app/models/schemas.py
git commit -m "feat: add Pydantic schemas for category management

- CategoryResponse for individual categories
- CategoriesConfigRequest for updating config
- CategoriesResponse for GET categories endpoint"
```

---

## Task 3: Category Service — Business Logic

**Files:**
- Create: `app/services/category_service.py`

**Step 1: Create category service**

Create `app/services/category_service.py`:

```python
import logging
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import XtreamCategory, XtreamAccount

logger = logging.getLogger("plexhub.category_service")


def now_ms() -> int:
    return int(time.time() * 1000)


class CategoryService:
    """Service for managing Xtream categories."""

    async def get_categories(
        self,
        db: AsyncSession,
        account_id: str,
        category_type: Optional[str] = None,
    ) -> list[XtreamCategory]:
        """Get all categories for an account, optionally filtered by type."""

        query = select(XtreamCategory).where(
            XtreamCategory.account_id == account_id
        )

        if category_type:
            query = query.where(XtreamCategory.category_type == category_type)

        query = query.order_by(XtreamCategory.category_name)

        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_filter_mode(
        self,
        db: AsyncSession,
        account_id: str,
    ) -> str:
        """Get the category filter mode for an account."""

        result = await db.execute(
            select(XtreamAccount.category_filter_mode).where(
                XtreamAccount.id == account_id
            )
        )
        return result.scalar() or "all"

    async def upsert_category(
        self,
        db: AsyncSession,
        account_id: str,
        category_id: str,
        category_type: str,
        category_name: str,
        is_allowed: Optional[bool] = None,
    ):
        """Insert or update a category, preserving is_allowed if not specified."""

        values = {
            "account_id": account_id,
            "category_id": category_id,
            "category_type": category_type,
            "category_name": category_name,
            "last_fetched_at": now_ms(),
        }

        if is_allowed is not None:
            values["is_allowed"] = is_allowed

        stmt = sqlite_upsert(XtreamCategory).values(**values)

        # On conflict, update only name and timestamp (preserve is_allowed)
        update_dict = {
            "category_name": category_name,
            "last_fetched_at": now_ms(),
        }
        if is_allowed is not None:
            update_dict["is_allowed"] = is_allowed

        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "category_id", "category_type"],
            set_=update_dict,
        )

        await db.execute(stmt)

    async def update_filter_mode(
        self,
        db: AsyncSession,
        account_id: str,
        mode: str,
    ):
        """Update the category filter mode for an account."""

        from sqlalchemy import update

        await db.execute(
            update(XtreamAccount)
            .where(XtreamAccount.id == account_id)
            .values(category_filter_mode=mode)
        )

    async def update_category_allowed(
        self,
        db: AsyncSession,
        account_id: str,
        category_id: str,
        category_type: str,
        is_allowed: bool,
    ):
        """Update the is_allowed status for a specific category."""

        from sqlalchemy import update

        await db.execute(
            update(XtreamCategory)
            .where(
                XtreamCategory.account_id == account_id,
                XtreamCategory.category_id == category_id,
                XtreamCategory.category_type == category_type,
            )
            .values(is_allowed=is_allowed)
        )


# Singleton
category_service = CategoryService()
```

**Step 2: Commit**

```bash
git add app/services/category_service.py
git commit -m "feat: add category service for database operations

- Get categories with optional type filter
- Upsert categories preserving is_allowed
- Update filter mode and category permissions"
```

---

## Task 4: Category API Endpoints — GET Categories

**Files:**
- Create: `app/api/categories.py`
- Modify: `app/main.py`

**Step 1: Create categories API**

Create `app/api/categories.py`:

```python
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.database import XtreamAccount
from app.models.schemas import CategoriesResponse, CategoryResponse
from app.services.category_service import category_service
from app.services.xtream_service import xtream_service
from sqlalchemy import select

logger = logging.getLogger("plexhub.api.categories")
router = APIRouter(prefix="/accounts/{account_id}/categories", tags=["categories"])


async def _fetch_and_cache_categories(db: AsyncSession, account):
    """Fetch categories from Xtream and cache them in DB."""

    try:
        # Fetch VOD categories
        vod_cats = await xtream_service.get_vod_categories(account)
        for cat in vod_cats:
            await category_service.upsert_category(
                db,
                account_id=account.id,
                category_id=str(cat.get("category_id", "")),
                category_type="vod",
                category_name=cat.get("category_name", "Unknown"),
            )
        logger.info(f"Cached {len(vod_cats)} VOD categories for account {account.id}")
    except Exception as e:
        logger.error(f"Failed to fetch VOD categories: {e}")

    try:
        # Fetch series categories
        series_cats = await xtream_service.get_series_categories(account)
        for cat in series_cats:
            await category_service.upsert_category(
                db,
                account_id=account.id,
                category_id=str(cat.get("category_id", "")),
                category_type="series",
                category_name=cat.get("category_name", "Unknown"),
            )
        logger.info(f"Cached {len(series_cats)} series categories for account {account.id}")
    except Exception as e:
        logger.error(f"Failed to fetch series categories: {e}")


@router.get("", response_model=CategoriesResponse)
async def get_categories(
    account_id: str,
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all categories for an account.

    If refresh=true, fetches fresh data from Xtream.
    Otherwise returns cached data.
    """

    # Load account
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    # Check if we need to refresh
    if refresh:
        await _fetch_and_cache_categories(db, account)
        await db.commit()
    else:
        # Check if cache is empty
        existing = await category_service.get_categories(db, account_id)
        if not existing:
            await _fetch_and_cache_categories(db, account)
            await db.commit()

    # Load categories from DB
    vod_cats = await category_service.get_categories(db, account_id, "vod")
    series_cats = await category_service.get_categories(db, account_id, "series")
    filter_mode = await category_service.get_filter_mode(db, account_id)

    return CategoriesResponse(
        vod=[CategoryResponse.model_validate(c) for c in vod_cats],
        series=[CategoryResponse.model_validate(c) for c in series_cats],
        filter_mode=filter_mode,
    )
```

**Step 2: Register router in main.py**

In `app/main.py`, add import:

```python
from app.api import accounts, health, media, stream, sync, categories
```

Then add router after existing routers:

```python
app.include_router(categories.router, prefix="/api")
```

**Step 3: Test the endpoint**

Run server: `uvicorn app.main:app --reload`

Test: `curl http://localhost:8000/api/accounts/{account_id}/categories`

Expected: JSON with vod/series arrays (may be empty if no account exists)

**Step 4: Commit**

```bash
git add app/api/categories.py app/main.py
git commit -m "feat: add GET categories endpoint

- Fetch categories from Xtream API
- Cache in database with upsert
- Return vod and series categories separately"
```

---

## Task 5: Category API — PUT and POST Endpoints

**Files:**
- Modify: `app/api/categories.py`

**Step 1: Add PUT endpoint for updating category config**

Add to `app/api/categories.py`:

```python
from app.models.schemas import CategoriesConfigRequest


@router.put("")
async def update_categories_config(
    account_id: str,
    body: CategoriesConfigRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Update category filtering configuration for an account.

    Updates filter_mode and is_allowed for specified categories.
    Then updates media visibility in background.
    """

    # Verify account exists
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    # Update filter mode
    await category_service.update_filter_mode(db, account_id, body.filter_mode)

    # Update each category's is_allowed
    for cat in body.categories:
        await category_service.update_category_allowed(
            db,
            account_id,
            cat.category_id,
            cat.category_type,
            cat.is_allowed,
        )

    await db.commit()

    # Update media visibility in background
    from app.workers.sync_worker import update_media_category_visibility
    asyncio.create_task(update_media_category_visibility(account_id))

    return {"status": "updated", "message": "Category configuration updated successfully"}


@router.post("/refresh", status_code=202)
async def refresh_categories(
    account_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Force refresh categories from Xtream server."""

    # Load account
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    # Refresh in background
    async def _refresh():
        async with get_db() as db:
            await _fetch_and_cache_categories(db, account)
            await db.commit()

    asyncio.create_task(_refresh())

    return {"status": "refreshing", "message": "Category refresh initiated"}
```

**Step 2: Test PUT endpoint**

Test:
```bash
curl -X PUT http://localhost:8000/api/accounts/{account_id}/categories \
  -H "Content-Type: application/json" \
  -d '{
    "filterMode": "whitelist",
    "categories": [
      {"categoryId": "1", "categoryType": "vod", "isAllowed": true}
    ]
  }'
```

Expected: `{"status": "updated", ...}`

**Step 3: Commit**

```bash
git add app/api/categories.py
git commit -m "feat: add PUT and POST categories endpoints

- PUT updates filter mode and category permissions
- POST refresh forces category fetch from Xtream
- Background task for media visibility update"
```

---

## Task 6: Sync Worker — Category Filtering Functions

**Files:**
- Modify: `app/workers/sync_worker.py`

**Step 1: Add import for XtreamCategory**

At top of file, add to imports:

```python
from app.models.database import Media, XtreamAccount, EnrichmentQueue, XtreamCategory
```

**Step 2: Add _load_category_config function**

Add before `sync_account` function:

```python
async def _load_category_config(db, account_id: str) -> dict:
    """Load category filtering configuration from database."""

    # Get filter mode
    result = await db.execute(
        select(XtreamAccount.category_filter_mode).where(
            XtreamAccount.id == account_id
        )
    )
    mode = result.scalar() or "all"

    # Get allowed categories
    result = await db.execute(
        select(XtreamCategory).where(
            XtreamCategory.account_id == account_id,
            XtreamCategory.is_allowed == True,
        )
    )
    allowed_categories = result.scalars().all()

    # Get blocked categories
    result = await db.execute(
        select(XtreamCategory).where(
            XtreamCategory.account_id == account_id,
            XtreamCategory.is_allowed == False,
        )
    )
    blocked_categories = result.scalars().all()

    # Build sets by type
    vod_allowed = {c.category_id for c in allowed_categories if c.category_type == "vod"}
    vod_blocked = {c.category_id for c in blocked_categories if c.category_type == "vod"}
    series_allowed = {c.category_id for c in allowed_categories if c.category_type == "series"}
    series_blocked = {c.category_id for c in blocked_categories if c.category_type == "series"}

    return {
        "mode": mode,
        "vod_allowed": vod_allowed,
        "vod_blocked": vod_blocked,
        "series_allowed": series_allowed,
        "series_blocked": series_blocked,
    }


def _should_sync_category(category_id: str, category_type: str, config: dict) -> bool:
    """Determine if a category should be synchronized."""

    if config["mode"] == "all":
        return True
    elif config["mode"] == "whitelist":
        allowed_set = config[f"{category_type}_allowed"]
        return category_id in allowed_set
    elif config["mode"] == "blacklist":
        blocked_set = config[f"{category_type}_blocked"]
        return category_id not in blocked_set

    # Default: sync
    return True
```

**Step 3: Commit**

```bash
git add app/workers/sync_worker.py
git commit -m "feat: add category filtering logic to sync worker

- Load category config from database
- Check if category should be synced based on mode
- Support all/whitelist/blacklist modes"
```

---

## Task 7: Sync Worker — Apply Filtering in VOD Sync

**Files:**
- Modify: `app/workers/sync_worker.py`

**Step 1: Modify sync_account VOD section**

Find the VOD sync section (around line with `vod_streams = await xtream_service.get_vod_streams(account)`)

Replace the VOD mapping and upsert logic with:

```python
            # --- VOD Sync ---
            logger.info(f"Syncing VOD for account {account_id}")

            # Load category config
            category_config = await _load_category_config(db, account_id)

            try:
                vod_streams = await xtream_service.get_vod_streams(account)
            except Exception as e:
                logger.error(f"Failed to fetch VOD streams: {e}")
                vod_streams = []

            vod_rows = []
            for i, dto in enumerate(vod_streams):
                category_id = str(dto.get("category_id", ""))

                # SKIP if category not allowed
                if not _should_sync_category(category_id, "vod", category_config):
                    logger.debug(f"Skipping VOD {dto.get('name')} (category {category_id} not allowed)")
                    continue

                row = map_vod_to_media(dto, account_id, i)
                row["is_in_allowed_categories"] = True
                vod_rows.append(row)

            if vod_rows:
                await upsert_media_batch(db, vod_rows)
                vod_keys = {r["rating_key"] for r in vod_rows}
                await differential_cleanup(db, server_id, "all", vod_keys)
                await enqueue_for_enrichment(db, vod_rows)
                total_synced += len(vod_rows)
                logger.info(f"Synced {len(vod_rows)} VOD items")
```

**Step 2: Test sync with filtering**

Run: `python -m app.workers.sync_worker` (if you have a test harness)

Expected: VOD items skip categories not in whitelist/blacklist

**Step 3: Commit**

```bash
git add app/workers/sync_worker.py
git commit -m "feat: apply category filtering in VOD sync

- Load category config before sync
- Skip VOD items from disallowed categories
- Mark synced items as is_in_allowed_categories=True"
```

---

## Task 8: Sync Worker — Apply Filtering in Series Sync

**Files:**
- Modify: `app/workers/sync_worker.py`

**Step 1: Modify series sync section**

Find the series sync section and replace with:

```python
            # --- Series Sync ---
            logger.info(f"Syncing Series for account {account_id}")
            try:
                series_list = await xtream_service.get_series(account)
            except Exception as e:
                logger.error(f"Failed to fetch series: {e}")
                series_list = []

            series_rows = []
            for i, dto in enumerate(series_list):
                category_id = str(dto.get("category_id", ""))

                # SKIP if category not allowed
                if not _should_sync_category(category_id, "series", category_config):
                    logger.debug(f"Skipping series {dto.get('name')} (category {category_id} not allowed)")
                    continue

                row = map_series_to_media(dto, account_id, i)
                row["is_in_allowed_categories"] = True
                series_rows.append(row)

            if series_rows:
                await upsert_media_batch(db, series_rows)
                series_keys = {r["rating_key"] for r in series_rows}
                await differential_cleanup(db, server_id, "all", series_keys)
                await enqueue_for_enrichment(db, series_rows)
                total_synced += len(series_rows)
                logger.info(f"Synced {len(series_rows)} series items")
```

**Step 2: Commit**

```bash
git add app/workers/sync_worker.py
git commit -m "feat: apply category filtering in series sync

- Skip series from disallowed categories
- Mark synced series as in allowed categories"
```

---

## Task 9: Sync Worker — Media Visibility Update Function

**Files:**
- Modify: `app/workers/sync_worker.py`

**Step 1: Add update_media_category_visibility function**

Add at the end of the file, before `run_all_accounts`:

```python
async def update_media_category_visibility(account_id: str):
    """
    Update is_in_allowed_categories for all media of an account
    after category configuration changes.
    """

    logger.info(f"Updating media category visibility for account {account_id}")

    async with async_session_factory() as db:
        config = await _load_category_config(db, account_id)
        server_id = f"xtream_{account_id}"

        if config["mode"] == "all":
            # Mark all media as visible
            await db.execute(
                update(Media)
                .where(Media.server_id == server_id)
                .values(is_in_allowed_categories=True)
            )
            await db.commit()
            logger.info(f"Marked all media as visible (mode=all)")
            return

        # Mode whitelist
        if config["mode"] == "whitelist":
            # VOD - mark visible if in allowed set
            if config["vod_allowed"]:
                await db.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type == "movie",
                        Media.filter.in_(config["vod_allowed"])
                    )
                    .values(is_in_allowed_categories=True)
                )
                await db.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type == "movie",
                        Media.filter.notin_(config["vod_allowed"])
                    )
                    .values(is_in_allowed_categories=False)
                )

            # Series - mark visible if in allowed set
            if config["series_allowed"]:
                await db.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type.in_(["show", "episode"]),
                        Media.filter.in_(config["series_allowed"])
                    )
                    .values(is_in_allowed_categories=True)
                )
                await db.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type.in_(["show", "episode"]),
                        Media.filter.notin_(config["series_allowed"])
                    )
                    .values(is_in_allowed_categories=False)
                )

        # Mode blacklist
        elif config["mode"] == "blacklist":
            # VOD - mark hidden if in blocked set
            if config["vod_blocked"]:
                await db.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type == "movie",
                        Media.filter.in_(config["vod_blocked"])
                    )
                    .values(is_in_allowed_categories=False)
                )
                await db.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type == "movie",
                        Media.filter.notin_(config["vod_blocked"])
                    )
                    .values(is_in_allowed_categories=True)
                )

            # Series - mark hidden if in blocked set
            if config["series_blocked"]:
                await db.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type.in_(["show", "episode"]),
                        Media.filter.in_(config["series_blocked"])
                    )
                    .values(is_in_allowed_categories=False)
                )
                await db.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type.in_(["show", "episode"]),
                        Media.filter.notin_(config["series_blocked"])
                    )
                    .values(is_in_allowed_categories=True)
                )

        await db.commit()
        logger.info(f"Updated media category visibility for {server_id}")
```

**Step 2: Commit**

```bash
git add app/workers/sync_worker.py
git commit -m "feat: add media visibility update function

- Update is_in_allowed_categories based on config
- Support all/whitelist/blacklist modes
- Batch update for VOD and series"
```

---

## Task 10: Media Service — Add Category Visibility Filtering

**Files:**
- Modify: `app/services/media_service.py`

**Step 1: Add include_filtered parameter**

Modify `get_media_list` signature:

```python
    async def get_media_list(
        self,
        db: AsyncSession,
        media_type: str,
        limit: int = 500,
        offset: int = 0,
        sort: str = "added_desc",
        server_id: Optional[str] = None,
        parent_rating_key: Optional[str] = None,
        series_rating_key: Optional[str] = None,  # NEW
        include_filtered: bool = False,  # NEW
    ) -> tuple[list[Media], int]:
```

**Step 2: Add visibility filtering**

After `query = select(Media).where(Media.type == media_type)`, add:

```python
        # Filter by category visibility
        if not include_filtered:
            query = query.where(Media.is_in_allowed_categories == True)
```

**Step 3: Commit**

```bash
git add app/services/media_service.py
git commit -m "feat: add category visibility filtering to media service

- Add include_filtered parameter
- Filter by is_in_allowed_categories by default"
```

---

## Task 11: Media Service — Fix Series-Episode Relationship

**Files:**
- Modify: `app/services/media_service.py`

**Step 1: Add series_rating_key support**

In `get_media_list`, after the server_id filter, add:

```python
        # Support series OR season filtering for episodes
        if series_rating_key:
            # Explicit series filter (grandparent)
            query = query.where(Media.grandparent_rating_key == series_rating_key)
        elif parent_rating_key:
            # Auto-detect: if starts with "series_", filter by grandparent
            if parent_rating_key.startswith("series_"):
                query = query.where(Media.grandparent_rating_key == parent_rating_key)
            else:
                # It's a season, filter by parent
                query = query.where(Media.parent_rating_key == parent_rating_key)
```

**Step 2: Test with series_6336**

Test: `curl http://localhost:8000/api/media/episodes?parent_rating_key=series_6336`

Expected: Episodes returned (if they exist in DB)

**Step 3: Commit**

```bash
git add app/services/media_service.py
git commit -m "feat: fix series-episode relationship query

- Add series_rating_key parameter
- Auto-detect series_ prefix in parent_rating_key
- Filter by grandparent_rating_key for series"
```

---

## Task 12: Media API — Update Endpoints with New Parameters

**Files:**
- Modify: `app/api/media.py`

**Step 1: Add include_filtered to movies endpoint**

```python
@router.get("/movies", response_model=MediaListResponse)
async def list_movies(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("added_desc"),
    server_id: Optional[str] = Query(None),
    include_filtered: bool = Query(False),  # NEW
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="movie", limit=limit, offset=offset,
        sort=sort, server_id=server_id, include_filtered=include_filtered,
    )
```

**Step 2: Add include_filtered to shows endpoint**

```python
@router.get("/shows", response_model=MediaListResponse)
async def list_shows(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort: str = Query("added_desc"),
    server_id: Optional[str] = Query(None),
    include_filtered: bool = Query(False),  # NEW
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="show", limit=limit, offset=offset,
        sort=sort, server_id=server_id, include_filtered=include_filtered,
    )
```

**Step 3: Add series_rating_key to episodes endpoint**

```python
@router.get("/episodes", response_model=MediaListResponse)
async def list_episodes(
    parent_rating_key: Optional[str] = Query(None),
    series_rating_key: Optional[str] = Query(None),  # NEW
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    server_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    items, total = await media_service.get_media_list(
        db, media_type="episode", limit=limit, offset=offset,
        server_id=server_id,
        parent_rating_key=parent_rating_key,
        series_rating_key=series_rating_key,
    )
```

**Step 4: Commit**

```bash
git add app/api/media.py
git commit -m "feat: add new query parameters to media endpoints

- Add include_filtered to movies and shows
- Add series_rating_key to episodes
- Pass through to media service"
```

---

## Task 13: Enrichment Worker — Fix Enqueue Logic

**Files:**
- Modify: `app/workers/sync_worker.py`

**Step 1: Update enqueue_for_enrichment function**

Find the `enqueue_for_enrichment` function and replace with:

```python
async def enqueue_for_enrichment(db, rows: list[dict]):
    """
    Insert media into enrichment queue if AT LEAST ONE ID is missing.
    Track existing IDs to optimize API calls.
    """
    for row in rows:
        if row["type"] not in ("movie", "show"):
            continue

        has_tmdb = bool(row.get("tmdb_id"))
        has_imdb = bool(row.get("imdb_id"))

        # Skip if both IDs present
        if has_tmdb and has_imdb:
            continue

        # At least one ID missing - enqueue
        stmt = sqlite_upsert(EnrichmentQueue).values(
            rating_key=row["rating_key"],
            server_id=row["server_id"],
            media_type=row["type"],
            title=row["title"],
            year=row.get("year"),
            existing_tmdb_id=row.get("tmdb_id"),  # NEW
            existing_imdb_id=row.get("imdb_id"),  # NEW
            status="pending",
            attempts=0,
            created_at=now_ms(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["rating_key", "server_id"],
            set_={
                "status": "pending",
                "existing_tmdb_id": row.get("tmdb_id"),
                "existing_imdb_id": row.get("imdb_id"),
            },
        )
        await db.execute(stmt)
```

**Step 2: Commit**

```bash
git add app/workers/sync_worker.py
git commit -m "feat: enqueue for enrichment when TMDB OR IMDB missing

- Check both IDs, enqueue if either missing
- Track existing IDs in queue for optimization"
```

---

## Task 14: Enrichment Worker — Fix VOD Enrichment Logic

**Files:**
- Modify: `app/workers/enrichment_worker.py`

**Step 1: Replace _enrich_vod_item function**

Find the `_enrich_vod_item` function and replace entirely with:

```python
async def _enrich_vod_item(db, item, account):
    """
    Enrich VOD item, optimizing API calls based on existing IDs.

    Scenarios:
    1. Both IDs present: skip
    2. TMDB present, IMDB absent: get external_ids only
    3. IMDB present, TMDB absent: search + get TMDB
    4. Both absent: full flow
    """
    used = 0
    tmdb_id = item.existing_tmdb_id
    imdb_id = item.existing_imdb_id
    confidence = None

    # Scenario 1: Both IDs present
    if tmdb_id and imdb_id:
        item.status = "skipped"
        item.processed_at = now_ms()
        return 0

    # Scenario 2: TMDB present, IMDB absent
    if tmdb_id and not imdb_id:
        try:
            ext_ids = await tmdb_service.get_movie_external_ids(int(tmdb_id))
            imdb_id = ext_ids.get("imdb_id")
            used += 1
            confidence = 1.0
            logger.info(f"Enriched {item.rating_key}: got IMDB from existing TMDB")
        except Exception as e:
            logger.debug(f"Failed to get external_ids for tmdb_id {tmdb_id}: {e}")

    # Scenario 3 & 4: TMDB absent
    elif not tmdb_id:
        # Try get_vod_info first (free, may have tmdb_id)
        try:
            vod_id_str = item.rating_key.split("_")[1].split(".")[0]
            vod_id = int(vod_id_str)
            vod_info = await xtream_service.get_vod_info(account, vod_id)
            info = vod_info.get("info") or {}
            raw_tmdb = info.get("tmdb_id")

            if raw_tmdb and str(raw_tmdb).strip():
                tmdb_id = str(int(raw_tmdb))

                # Get IMDB if we don't have it
                if not imdb_id:
                    ext_ids = await tmdb_service.get_movie_external_ids(int(tmdb_id))
                    imdb_id = ext_ids.get("imdb_id")
                    used += 1

                confidence = 1.0
                logger.info(f"Enriched {item.rating_key}: got TMDB from Xtream")
        except Exception as e:
            logger.debug(f"Xtream vod_info failed for {item.rating_key}: {e}")

        # Fallback to TMDB search if still no tmdb_id
        if not tmdb_id and tmdb_service.is_configured:
            try:
                match = await tmdb_service.search_movie(item.title, item.year)
                if match and match.confidence >= 0.85:
                    tmdb_id = str(match.tmdb_id)

                    # Get IMDB if we don't have it
                    if not imdb_id:
                        ext_ids = await tmdb_service.get_movie_external_ids(int(tmdb_id))
                        imdb_id = ext_ids.get("imdb_id")
                        used += 2  # search + external_ids
                    else:
                        used += 1  # search only

                    confidence = match.confidence
                    logger.info(f"Enriched {item.rating_key}: got TMDB from search")
            except Exception as e:
                logger.debug(f"TMDB search failed for {item.title}: {e}")

    # Update media if we got at least one ID
    if tmdb_id or imdb_id:
        # Import here to avoid circular dependency
        from app.utils.unification import calculate_unification_id

        new_unification = calculate_unification_id(
            item.title, item.year, imdb_id, tmdb_id
        )

        await db.execute(
            update(Media)
            .where(
                Media.rating_key == item.rating_key,
                Media.server_id == item.server_id,
            )
            .values(
                tmdb_id=tmdb_id,
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
```

**Step 2: Commit**

```bash
git add app/workers/enrichment_worker.py
git commit -m "feat: optimize VOD enrichment for partial IDs

- Skip if both IDs present
- Get external_ids only if TMDB exists
- Full search flow if both missing
- Reduce API calls significantly"
```

---

## Task 15: Enrichment Worker — Fix Series Enrichment Logic

**Files:**
- Modify: `app/workers/enrichment_worker.py`

**Step 1: Replace _enrich_series_item function**

Find `_enrich_series_item` and replace with:

```python
async def _enrich_series_item(db, item):
    """Enrich series item, optimizing based on existing IDs."""
    used = 0
    tmdb_id = item.existing_tmdb_id
    imdb_id = item.existing_imdb_id

    # Skip if both IDs present
    if tmdb_id and imdb_id:
        item.status = "skipped"
        item.processed_at = now_ms()
        return 0

    # TMDB present, IMDB absent
    if tmdb_id and not imdb_id:
        if not tmdb_service.is_configured:
            item.status = "skipped"
            item.processed_at = now_ms()
            return 0

        try:
            ext_ids = await tmdb_service.get_tv_external_ids(int(tmdb_id))
            imdb_id = ext_ids.get("imdb_id")
            used += 1

            from app.utils.unification import calculate_unification_id
            new_unification = calculate_unification_id(
                item.title, item.year, imdb_id, tmdb_id
            )

            await db.execute(
                update(Media)
                .where(
                    Media.rating_key == item.rating_key,
                    Media.server_id == item.server_id,
                )
                .values(
                    imdb_id=imdb_id,
                    unification_id=new_unification,
                    history_group_key=new_unification,
                    tmdb_match_confidence=1.0,
                )
            )
            item.status = "done"
        except Exception as e:
            item.status = "failed"
            item.last_error = str(e)
            logger.error(f"Failed to get TV external_ids for {tmdb_id}: {e}")

    # TMDB absent - search
    elif not tmdb_id:
        if not tmdb_service.is_configured:
            item.status = "skipped"
            item.processed_at = now_ms()
            return 0

        try:
            match = await tmdb_service.search_tv(item.title, item.year)
            if match and match.confidence >= 0.85:
                tmdb_id = str(match.tmdb_id)

                # Get IMDB if we don't have it
                if not imdb_id:
                    ext_ids = await tmdb_service.get_tv_external_ids(int(tmdb_id))
                    imdb_id = ext_ids.get("imdb_id")
                    used += 2  # search + external_ids
                else:
                    used += 1  # search only

                from app.utils.unification import calculate_unification_id
                new_unification = calculate_unification_id(
                    item.title, item.year, imdb_id, tmdb_id
                )

                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                    )
                    .values(
                        tmdb_id=tmdb_id,
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
            logger.error(f"TMDB search failed for series '{item.title}': {e}")

    item.attempts += 1
    item.processed_at = now_ms()
    return used
```

**Step 2: Commit**

```bash
git add app/workers/enrichment_worker.py
git commit -m "feat: optimize series enrichment for partial IDs

- Same logic as VOD enrichment
- Use existing IDs to minimize API calls"
```

---

## Task 16: TMDB Service — Guarantee 'tt' Prefix for IMDB IDs

**Files:**
- Modify: `app/services/tmdb_service.py`

**Step 1: Update get_movie_external_ids**

Find `get_movie_external_ids` and modify the return:

```python
    async def get_movie_external_ids(self, tmdb_id: int) -> dict:
        """Get external IDs for a movie."""
        client = await self._get_client()
        resp = await client.get(f"{self.BASE_URL}/movie/{tmdb_id}/external_ids")
        resp.raise_for_status()
        data = resp.json()

        # Guarantee 'tt' prefix for IMDB ID
        imdb_id = data.get("imdb_id")
        if imdb_id and not imdb_id.startswith("tt"):
            imdb_id = f"tt{imdb_id}"

        return {
            "imdb_id": imdb_id,
            "tvdb_id": data.get("tvdb_id"),
            "facebook_id": data.get("facebook_id"),
            "instagram_id": data.get("instagram_id"),
            "twitter_id": data.get("twitter_id"),
        }
```

**Step 2: Update get_tv_external_ids**

Find `get_tv_external_ids` and apply same fix:

```python
    async def get_tv_external_ids(self, tmdb_id: int) -> dict:
        """Get external IDs for a TV series."""
        client = await self._get_client()
        resp = await client.get(f"{self.BASE_URL}/tv/{tmdb_id}/external_ids")
        resp.raise_for_status()
        data = resp.json()

        # Guarantee 'tt' prefix for IMDB ID
        imdb_id = data.get("imdb_id")
        if imdb_id and not imdb_id.startswith("tt"):
            imdb_id = f"tt{imdb_id}"

        return {
            "imdb_id": imdb_id,
            "tvdb_id": data.get("tvdb_id"),
            "freebase_id": data.get("freebase_id"),
            "tvrage_id": data.get("tvrage_id"),
        }
```

**Step 3: Commit**

```bash
git add app/services/tmdb_service.py
git commit -m "feat: guarantee 'tt' prefix in IMDB IDs from TMDB

- Add prefix if missing in get_movie_external_ids
- Add prefix if missing in get_tv_external_ids"
```

---

## Task 17: Unification Utility — Guarantee 'tt' Prefix

**Files:**
- Modify: `app/utils/unification.py`

**Step 1: Update calculate_unification_id**

Find the function and update the IMDB handling:

```python
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
        # Guarantee 'tt' prefix
        if not imdb_id.startswith("tt"):
            imdb_id = f"tt{imdb_id}"
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
```

**Step 2: Commit**

```bash
git add app/utils/unification.py
git commit -m "feat: guarantee 'tt' prefix in unification_id

- Add prefix to IMDB ID if missing
- Ensure format is always imdb://ttXXXXXXX"
```

---

## Task 18: Run Migrations and Test Full Flow

**Files:**
- None (testing)

**Step 1: Run database migrations**

Run: `python -m app.db.migrations`
Expected: "Database migrations completed successfully"

**Step 2: Start the server**

Run: `uvicorn app.main:app --reload`
Expected: Server starts without errors

**Step 3: Test category endpoints**

```bash
# Get categories (should auto-fetch if empty)
curl http://localhost:8000/api/accounts/{account_id}/categories

# Update config
curl -X PUT http://localhost:8000/api/accounts/{account_id}/categories \
  -H "Content-Type: application/json" \
  -d '{"filterMode": "whitelist", "categories": [{"categoryId": "1", "categoryType": "vod", "isAllowed": true}]}'
```

**Step 4: Test media endpoints with filtering**

```bash
# Movies (filtered by default)
curl "http://localhost:8000/api/media/movies?limit=10"

# Movies (include filtered)
curl "http://localhost:8000/api/media/movies?limit=10&include_filtered=true"

# Episodes by series
curl "http://localhost:8000/api/media/episodes?parent_rating_key=series_6336"
```

**Step 5: Manual verification checklist**

- [ ] XtreamCategory table exists in DB
- [ ] New columns added to existing tables
- [ ] GET categories returns data
- [ ] PUT categories updates config
- [ ] Media endpoints respect is_in_allowed_categories
- [ ] Episodes query works with series_ prefix
- [ ] Enrichment queue has existing_tmdb_id and existing_imdb_id

**Step 6: Commit if any fixes needed**

```bash
git add <any-fixes>
git commit -m "fix: address integration test issues"
```

---

## Task 19: Documentation Update

**Files:**
- Modify: `docs/PLEXHUB_BACKEND_INTEGRATION_GUIDE.md`

**Step 1: Add category endpoints to documentation**

Add section after "Sync Schemas":

```markdown
### Category Management

#### `GET /api/accounts/{account_id}/categories`

Récupère les catégories VOD et séries pour un compte.

**Paramètres query :**
- `refresh` (optionnel, bool) : Force le fetch depuis Xtream

**Réponse 200 :**
```json
{
  "vod": [
    {"categoryId": "1", "categoryName": "Action", "categoryType": "vod", "isAllowed": true, "lastFetchedAt": 1772240521981}
  ],
  "series": [
    {"categoryId": "2", "categoryName": "Drama", "categoryType": "series", "isAllowed": true, "lastFetchedAt": 1772240521981}
  ],
  "filterMode": "whitelist"
}
```

#### `PUT /api/accounts/{account_id}/categories`

Met à jour la configuration de filtrage des catégories.

**Request body :**
```json
{
  "filterMode": "whitelist",
  "categories": [
    {"categoryId": "1", "categoryType": "vod", "isAllowed": true},
    {"categoryId": "44", "categoryType": "vod", "isAllowed": false}
  ]
}
```
```

**Step 2: Document new query parameters**

Update media endpoints section to document `include_filtered` and `series_rating_key`.

**Step 3: Commit**

```bash
git add docs/PLEXHUB_BACKEND_INTEGRATION_GUIDE.md
git commit -m "docs: add category management endpoints

- Document GET/PUT categories
- Document new query parameters
- Update integration examples"
```

---

## Task 20: Final Testing and Validation

**Files:**
- None (testing)

**Step 1: Full sync test**

```bash
# Trigger a full sync
curl -X POST http://localhost:8000/api/sync/xtream/all

# Wait for completion, then check
curl http://localhost:8000/api/health
```

Expected:
- Media synced with category filtering applied
- Enrichment queue populated with items missing IDs

**Step 2: Enrichment test**

Manually trigger enrichment worker or wait for scheduled run.

Check database:
```sql
SELECT rating_key, tmdb_id, imdb_id, unification_id
FROM media
WHERE type='movie'
LIMIT 10;
```

Expected:
- IMDB IDs have 'tt' prefix
- unification_id format is `imdb://ttXXXXX` or `tmdb://XXXXX`

**Step 3: Category filtering test**

1. Set filter mode to whitelist with only category "1"
2. Trigger sync
3. Check that only category "1" media is synced
4. Change to blacklist mode excluding category "44"
5. Verify category "44" media marked as not in allowed categories

**Step 4: Series-episode test**

```bash
# Get series
curl "http://localhost:8000/api/media/shows?limit=1"

# Extract series ratingKey, then get episodes
curl "http://localhost:8000/api/media/episodes?parent_rating_key=series_XXXX"
```

Expected: Episodes returned for the series

**Step 5: Final commit**

```bash
git add -A
git commit -m "test: verify all features working

- Category filtering in sync
- Enrichment with partial IDs
- Series-episode queries
- IMDB ID prefix guarantee"
```

---

## Summary

This implementation plan covers:

1. ✅ Database migrations (new table + columns)
2. ✅ Category management API (GET/PUT/POST)
3. ✅ Category filtering in sync worker
4. ✅ Media visibility updates
5. ✅ Fixed enrichment logic (TMDB OR IMDB)
6. ✅ Fixed series-episode queries
7. ✅ IMDB ID 'tt' prefix guarantee
8. ✅ Documentation updates

**Total Estimated Time:** 6-8 hours (bite-sized tasks, frequent commits)

**Key Files Modified:**
- `app/models/database.py` - ORM models
- `app/models/schemas.py` - API schemas
- `app/api/categories.py` - NEW endpoints
- `app/services/category_service.py` - NEW service
- `app/services/media_service.py` - Query fixes
- `app/services/tmdb_service.py` - IMDB prefix
- `app/utils/unification.py` - IMDB prefix
- `app/workers/sync_worker.py` - Filtering logic
- `app/workers/enrichment_worker.py` - Partial ID optimization
- `app/main.py` - Router registration
