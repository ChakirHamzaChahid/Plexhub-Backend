"""
Category management service for Xtream account filtering.
"""
import logging
import time
from typing import List, Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import XtreamCategory, XtreamAccount, Media, LiveChannel

logger = logging.getLogger(__name__)


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
        # Update existing
        existing.category_name = category_name
        existing.last_fetched_at = now
        await db.commit()
        await db.refresh(existing)
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
        await db.commit()
        await db.refresh(category)
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
    await db.commit()
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
    await db.commit()

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

    # Set default for unlisted categories based on filter mode
    if filter_mode in ("whitelist", "blacklist"):
        default_allowed = filter_mode == "blacklist"  # whitelist: False, blacklist: True

        all_cats_result = await db.execute(
            select(XtreamCategory).where(
                XtreamCategory.account_id == account_id
            )
        )
        all_cats = all_cats_result.scalars().all()

        unlisted_count = 0
        for cat in all_cats:
            if (cat.category_id, cat.category_type) not in listed_keys:
                stmt = (
                    update(XtreamCategory)
                    .where(
                        XtreamCategory.account_id == account_id,
                        XtreamCategory.category_id == cat.category_id,
                        XtreamCategory.category_type == cat.category_type,
                    )
                    .values(is_allowed=default_allowed)
                )
                await db.execute(stmt)
                unlisted_count += 1

        await db.commit()
        logger.info(
            f"Set {unlisted_count} unlisted categories to is_allowed={default_allowed} "
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
    server_id = f"xtream_{account_id}"

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
        await db.commit()
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

    await db.commit()

    logger.info(
        f"Visibility update [{account_id}]: mode={filter_mode}, "
        f"VOD categories={len(allowed_vod_ids)}, "
        f"Series categories={len(allowed_series_ids)}, "
        f"Live categories={len(allowed_live_ids)}, "
        f"visible series={len(visible_series_keys)}"
    )
