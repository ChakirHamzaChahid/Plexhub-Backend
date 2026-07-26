"""
Category management service for Xtream account filtering.
"""
import logging
import time
from typing import List, Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.config import settings
from app.models.database import XtreamCategory, XtreamAccount, Media, LiveChannel
from app.services.xtream_service import xtream_service
from app.utils.db_retry import commit_with_retry, write_with_retry
from app.utils.server_id import build_server_id

logger = logging.getLogger(__name__)


class AccountNotFoundError(Exception):
    """Raised when account_id doesn't resolve to an existing XtreamAccount."""


def _is_adult_category_name(name: Optional[str]) -> bool:
    """True when a category name matches any configured adult keyword.

    Case-insensitive substring match against settings.ADULT_CATEGORY_KEYWORDS
    (e.g. "VOD - ADULT +18" matches "adult" and "+18").
    """
    if not name:
        return False
    lowered = name.lower()
    return any(kw in lowered for kw in settings.ADULT_CATEGORY_KEYWORDS)


async def get_categories(
    db: AsyncSession,
    account_id: str,
) -> tuple[List[XtreamCategory], str]:
    """
    Get all categories for an account.

    Args:
        db: Database session
        account_id: Xtream account ID

    Returns:
        Tuple of (categories list, filter_mode)
    """
    # Get filter mode from account
    account_stmt = select(XtreamAccount.category_filter_mode).where(
        XtreamAccount.id == account_id
    )
    result = await db.execute(account_stmt)
    filter_mode = result.scalar_one_or_none() or "all"

    # Get categories
    stmt = select(XtreamCategory).where(
        XtreamCategory.account_id == account_id
    ).order_by(XtreamCategory.category_name)

    result = await db.execute(stmt)
    categories = result.scalars().all()

    return list(categories), filter_mode


async def upsert_category(
    db: AsyncSession,
    account_id: str,
    category_id: str,
    category_type: str,
    category_name: str,
    is_allowed: bool = True,
) -> XtreamCategory:
    """
    Insert or update a category.

    Args:
        db: Database session
        account_id: Xtream account ID
        category_id: Category ID from Xtream
        category_type: "vod" or "series"
        category_name: Human-readable category name
        is_allowed: Whether category is allowed (default True)

    Returns:
        Updated or created XtreamCategory
    """
    now = int(time.time() * 1000)

    # Check if category exists
    stmt = select(XtreamCategory).where(
        XtreamCategory.account_id == account_id,
        XtreamCategory.category_id == category_id,
        XtreamCategory.category_type == category_type,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        # Update existing (no commit — let caller manage transaction)
        existing.category_name = category_name
        existing.last_fetched_at = now
        return existing
    else:
        # Create new
        category = XtreamCategory(
            account_id=account_id,
            category_id=category_id,
            category_type=category_type,
            category_name=category_name,
            is_allowed=is_allowed,
            last_fetched_at=now,
        )
        db.add(category)
        return category


async def update_filter_mode(
    db: AsyncSession,
    account_id: str,
    filter_mode: str,
) -> None:
    """
    Update the category filter mode for an account.

    Args:
        db: Database session
        account_id: Xtream account ID
        filter_mode: "all", "whitelist", or "blacklist"
    """
    if filter_mode not in ("all", "whitelist", "blacklist"):
        raise ValueError(f"Invalid filter_mode: {filter_mode}")

    stmt = (
        update(XtreamAccount)
        .where(XtreamAccount.id == account_id)
        .values(category_filter_mode=filter_mode)
    )
    await db.execute(stmt)
    # No commit here — let caller manage transaction
    logger.info(f"Updated filter mode for account {account_id} to {filter_mode}")


async def update_category_allowed(
    db: AsyncSession,
    account_id: str,
    category_id: str,
    category_type: str,
    is_allowed: bool,
) -> None:
    """
    Update the is_allowed status for a specific category.

    Args:
        db: Database session
        account_id: Xtream account ID
        category_id: Category ID
        category_type: "vod" or "series"
        is_allowed: Whether category is allowed
    """
    stmt = (
        update(XtreamCategory)
        .where(
            XtreamCategory.account_id == account_id,
            XtreamCategory.category_id == category_id,
            XtreamCategory.category_type == category_type,
        )
        .values(is_allowed=is_allowed)
    )
    result = await db.execute(stmt)
    # No commit here — let caller manage transaction

    if result.rowcount == 0:
        logger.warning(
            f"No category found to update: account={account_id}, "
            f"category={category_id}, type={category_type}"
        )
    else:
        logger.info(
            f"Updated category {category_id} ({category_type}) "
            f"for account {account_id} to allowed={is_allowed}"
        )


async def bulk_update_categories(
    db: AsyncSession,
    account_id: str,
    filter_mode: str,
    categories: List[dict],
) -> None:
    """
    Bulk update category configuration.

    In whitelist mode: categories in the request get their isAllowed value,
    all OTHER categories are set to is_allowed=False.
    In blacklist mode: categories in the request get their isAllowed value,
    all OTHER categories are set to is_allowed=True.

    Args:
        db: Database session
        account_id: Xtream account ID
        filter_mode: "all", "whitelist", or "blacklist"
        categories: List of dicts with categoryId, categoryType, isAllowed
    """
    # Update filter mode
    await update_filter_mode(db, account_id, filter_mode)

    # Build set of explicitly listed category keys
    listed_keys = set()
    for cat_dict in categories:
        category_id = cat_dict.get("categoryId")
        category_type = cat_dict.get("categoryType")
        is_allowed = cat_dict.get("isAllowed", True)

        if category_id and category_type:
            listed_keys.add((str(category_id), category_type))
            await update_category_allowed(
                db, account_id, str(category_id), category_type, is_allowed
            )

    # Set default for unlisted categories based on filter mode (single bulk UPDATE)
    if filter_mode in ("whitelist", "blacklist"):
        default_allowed = filter_mode == "blacklist"  # whitelist: False, blacklist: True

        # Build exclusion filter: skip categories that were explicitly listed
        from sqlalchemy import tuple_
        base_stmt = (
            update(XtreamCategory)
            .where(XtreamCategory.account_id == account_id)
            .values(is_allowed=default_allowed)
        )
        if listed_keys:
            # Exclude explicitly listed categories from bulk default
            base_stmt = base_stmt.where(
                ~tuple_(XtreamCategory.category_id, XtreamCategory.category_type).in_(
                    list(listed_keys)
                )
            )
        result = await db.execute(base_stmt)
        # NOT converted to write_with_retry (AUDIT-P1-001, ADR 0004 Decision
        # 4 — "no mechanical conversion"): this is the only commit point for
        # update_filter_mode()'s and the per-category update_category_
        # allowed() statements executed above, all pending on this SAME `db`
        # session (no earlier commit in this function). A fresh session
        # would either orphan those uncommitted statements or race this
        # session's own held write transaction. commit_with_retry is honest
        # here; busy_timeout=60s is the real safety net for this call path.
        await commit_with_retry(db)
        logger.info(
            f"Set {result.rowcount} unlisted categories to is_allowed={default_allowed} "
            f"(filter_mode={filter_mode})"
        )

    # Recalculate media visibility based on new category config
    await update_media_category_visibility(db, account_id)

    logger.info(
        f"Bulk updated {len(categories)} categories for account {account_id}"
    )


async def update_media_category_visibility(
    db: AsyncSession,
    account_id: str,
) -> None:
    """
    Recalculate is_in_allowed_categories for ALL media of an account
    based on the current category configuration.

    The media.filter column stores the category_id from Xtream.
    - mode 'all': everything visible
    - mode 'whitelist': only categories with is_allowed=True are visible
    - mode 'blacklist': everything except categories with is_allowed=False

    Episodes inherit visibility from their parent series (grandparent_rating_key).
    """
    server_id = build_server_id(account_id)

    # Load current config
    result = await db.execute(
        select(XtreamAccount.category_filter_mode).where(
            XtreamAccount.id == account_id
        )
    )
    filter_mode = result.scalar_one_or_none() or "all"

    if filter_mode == "all":
        # Everything visible
        await db.execute(
            update(Media)
            .where(Media.server_id == server_id)
            .values(is_in_allowed_categories=True)
        )
        await db.execute(
            update(LiveChannel)
            .where(LiveChannel.server_id == server_id)
            .values(is_in_allowed_categories=True)
        )
        # NOT converted to write_with_retry (AUDIT-P1-001, ADR 0004 Decision
        # 4 — "no mechanical conversion"): this function's own
        # `category_filter_mode` read at the top of update_media_category_
        # visibility() depends on read-after-write consistency with this
        # SAME `db` session — notably bulk_update_categories()'s
        # update_filter_mode() call when filter_mode == "all" (this is its
        # only commit point). A fresh session would silently read the STALE
        # filter_mode instead. commit_with_retry is honest here;
        # busy_timeout=60s is the real safety net for this call path.
        await commit_with_retry(db)
        logger.info(f"Visibility update [{account_id}]: mode=all, all media + live channels set to visible")
        return

    # Load category config
    result = await db.execute(
        select(XtreamCategory).where(XtreamCategory.account_id == account_id)
    )
    categories = result.scalars().all()

    allowed_vod_ids = set()
    allowed_series_ids = set()
    allowed_live_ids = set()
    for cat in categories:
        if cat.category_type == "vod" and cat.is_allowed:
            allowed_vod_ids.add(cat.category_id)
        elif cat.category_type == "series" and cat.is_allowed:
            allowed_series_ids.add(cat.category_id)
        elif cat.category_type == "live" and cat.is_allowed:
            allowed_live_ids.add(cat.category_id)

    # --- Movies: set all to False, then True for allowed category IDs ---
    await db.execute(
        update(Media)
        .where(Media.server_id == server_id, Media.type == "movie")
        .values(is_in_allowed_categories=False)
    )
    if allowed_vod_ids:
        chunk_size = 500
        vod_list = list(allowed_vod_ids)
        for i in range(0, len(vod_list), chunk_size):
            chunk = vod_list[i : i + chunk_size]
            await db.execute(
                update(Media)
                .where(
                    Media.server_id == server_id,
                    Media.type == "movie",
                    Media.filter.in_(chunk),
                )
                .values(is_in_allowed_categories=True)
            )

    # --- Shows: set all to False, then True for allowed category IDs ---
    await db.execute(
        update(Media)
        .where(Media.server_id == server_id, Media.type == "show")
        .values(is_in_allowed_categories=False)
    )
    if allowed_series_ids:
        chunk_size = 500
        series_list = list(allowed_series_ids)
        for i in range(0, len(series_list), chunk_size):
            chunk = series_list[i : i + chunk_size]
            await db.execute(
                update(Media)
                .where(
                    Media.server_id == server_id,
                    Media.type == "show",
                    Media.filter.in_(chunk),
                )
                .values(is_in_allowed_categories=True)
            )

    # --- Episodes: inherit visibility from their parent series ---
    # First set all episodes to False
    await db.execute(
        update(Media)
        .where(Media.server_id == server_id, Media.type == "episode")
        .values(is_in_allowed_categories=False)
    )
    # Get visible series rating_keys
    visible_series_result = await db.execute(
        select(Media.rating_key).where(
            Media.server_id == server_id,
            Media.type == "show",
            Media.is_in_allowed_categories == True,
        )
    )
    visible_series_keys = [row[0] for row in visible_series_result]

    if visible_series_keys:
        chunk_size = 500
        for i in range(0, len(visible_series_keys), chunk_size):
            chunk = visible_series_keys[i : i + chunk_size]
            await db.execute(
                update(Media)
                .where(
                    Media.server_id == server_id,
                    Media.type == "episode",
                    Media.grandparent_rating_key.in_(chunk),
                )
                .values(is_in_allowed_categories=True)
            )

    # --- Live Channels: set all to False, then True for allowed category IDs ---
    await db.execute(
        update(LiveChannel)
        .where(LiveChannel.server_id == server_id)
        .values(is_in_allowed_categories=False)
    )
    if allowed_live_ids:
        chunk_size = 500
        live_list = list(allowed_live_ids)
        for i in range(0, len(live_list), chunk_size):
            chunk = live_list[i : i + chunk_size]
            await db.execute(
                update(LiveChannel)
                .where(
                    LiveChannel.server_id == server_id,
                    LiveChannel.category_id.in_(chunk),
                )
                .values(is_in_allowed_categories=True)
            )

    # NOT converted to write_with_retry (AUDIT-P1-001, ADR 0004 Decision 4 —
    # "no mechanical conversion"): same read-after-uncommitted-write
    # dependency as the mode=="all" branch above (this function's own
    # `category_filter_mode` read, plus whatever bulk_update_categories()
    # left pending on this SAME `db` session before calling us) — a fresh
    # session risks a stale read or a lock race against `db`'s own held
    # write transaction. commit_with_retry is honest here; busy_timeout=60s
    # is the real safety net for this call path.
    await commit_with_retry(db)

    logger.info(
        f"Visibility update [{account_id}]: mode={filter_mode}, "
        f"VOD categories={len(allowed_vod_ids)}, "
        f"Series categories={len(allowed_series_ids)}, "
        f"Live categories={len(allowed_live_ids)}, "
        f"visible series={len(visible_series_keys)}"
    )


async def update_media_adult_flags(
    db: AsyncSession,
    account_id: str,
) -> None:
    """Recalculate is_adult for ALL movies of an account from its categories.

    A VOD category is "adult" when its name matches an adult keyword
    (settings.ADULT_CATEGORY_KEYWORDS) OR its category_id is explicitly listed
    in settings.ADULT_CATEGORY_IDS. Movies whose media.filter (Xtream
    category_id) belongs to such a category are flagged is_adult and have their
    content_rating forced to settings.ADULT_CONTENT_RATING (read by the NFO
    <mpaa> tag and the API). Non-adult movies keep their original content_rating.

    Movies only (matches the adult-category scope). The title "[XXX] " prefix is
    applied at the output boundaries — API serialization AND Plex/Jellyfin library
    generation (folder/file names + movie.nfo <title>) — via apply_adult_prefix,
    but never stored on media.title. Idempotent and retroactive: runs every sync
    after update_media_category_visibility.

    AUDIT-P1-001 / ADR 0004 Decision 4 (S3.2): converted to write_with_retry.
    This function is fully self-contained — everything it reads and writes
    is re-derived from `account_id` alone, nothing depends on prior
    uncommitted state of the caller's `db` session (unlike
    update_media_category_visibility, which is NOT converted — see its
    docstring/comments) — so it is safe to run each retry attempt on a
    FRESH session bound to the same engine as the caller's `db` (`db.bind`,
    not `db.get_bind()` — the latter returns the underlying sync `Engine`,
    which `async_sessionmaker` rejects; `.bind` is the actual `AsyncEngine`
    the session was built from). This also keeps tests against an isolated
    `db_engine` fixture writing to that same test database rather than the
    production pool. `db` itself is only used to resolve the target engine;
    the real read+write happens on the fresh session inside `_work`.
    """
    server_id = build_server_id(account_id)

    async def _work(session: AsyncSession) -> int:
        result = await session.execute(
            select(XtreamCategory).where(XtreamCategory.account_id == account_id)
        )
        categories = result.scalars().all()

        explicit_ids = set(settings.ADULT_CATEGORY_IDS)
        adult_vod_ids = {
            cat.category_id
            for cat in categories
            if cat.category_type == "vod"
            and (_is_adult_category_name(cat.category_name) or cat.category_id in explicit_ids)
        }

        # Reset every movie to non-adult, then flag the adult categories.
        # Idempotent by construction: a replayed attempt (retry) fully
        # recomputes from scratch every time, never doubling an effect.
        await session.execute(
            update(Media)
            .where(Media.server_id == server_id, Media.type == "movie")
            .values(is_adult=False)
        )

        if adult_vod_ids:
            chunk_size = 500
            vod_list = list(adult_vod_ids)
            for i in range(0, len(vod_list), chunk_size):
                chunk = vod_list[i : i + chunk_size]
                await session.execute(
                    update(Media)
                    .where(
                        Media.server_id == server_id,
                        Media.type == "movie",
                        Media.filter.in_(chunk),
                    )
                    .values(is_adult=True)
                )

            # Force the +18 certification on flagged movies (NFO <mpaa> / API).
            await session.execute(
                update(Media)
                .where(
                    Media.server_id == server_id,
                    Media.type == "movie",
                    Media.is_adult == True,
                )
                .values(content_rating=settings.ADULT_CONTENT_RATING)
            )

        await session.commit()
        return len(adult_vod_ids)

    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    adult_category_count = await write_with_retry(
        _work,
        session_factory=session_factory,
        op="category_service.update_media_adult_flags",
    )

    logger.info(
        f"Adult flags update [{account_id}]: "
        f"adult VOD categories={adult_category_count}"
    )


async def refresh_categories_from_provider(
    db: AsyncSession,
    account_id: str,
) -> tuple[int, int]:
    """Force-refresh VOD + series categories from the Xtream provider.

    Fetches current categories from Xtream and upserts them (preserves
    existing is_allowed settings — upsert_category only sets is_allowed=True
    on first insert; already-known categories keep their prior flag).

    Caller commits (CR-C04 lock-retry stays with the caller, unchanged).

    Args:
        db: Database session
        account_id: Xtream account ID

    Returns:
        (vod_count, series_count)

    Raises:
        AccountNotFoundError: no account with this id.
    """
    stmt = select(XtreamAccount).where(XtreamAccount.id == account_id)
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()

    if not account:
        raise AccountNotFoundError(account_id)

    # Fetch VOD categories
    vod_categories = await xtream_service.get_vod_categories(
        account.base_url,
        account.port,
        account.username,
        account.password,
    )

    # Fetch Series categories
    series_categories = await xtream_service.get_series_categories(
        account.base_url,
        account.port,
        account.username,
        account.password,
    )

    # Upsert VOD categories
    for cat in vod_categories:
        await upsert_category(
            db,
            account_id,
            cat.get("category_id", ""),
            "vod",
            cat.get("category_name", "Unknown"),
            is_allowed=True,  # Default to allowed, preserves existing if already exists
        )

    # Upsert Series categories
    for cat in series_categories:
        await upsert_category(
            db,
            account_id,
            cat.get("category_id", ""),
            "series",
            cat.get("category_name", "Unknown"),
            is_allowed=True,
        )

    return len(vod_categories), len(series_categories)
