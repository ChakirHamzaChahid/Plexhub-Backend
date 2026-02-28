"""
Category management service for Xtream account filtering.
"""
import logging
import time
from typing import List, Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import XtreamCategory, XtreamAccount

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

    Args:
        db: Database session
        account_id: Xtream account ID
        filter_mode: "all", "whitelist", or "blacklist"
        categories: List of dicts with categoryId, categoryType, isAllowed
    """
    # Update filter mode
    await update_filter_mode(db, account_id, filter_mode)

    # Update each category's is_allowed status
    for cat_dict in categories:
        category_id = cat_dict.get("categoryId")
        category_type = cat_dict.get("categoryType")
        is_allowed = cat_dict.get("isAllowed", True)

        if category_id and category_type:
            await update_category_allowed(
                db, account_id, category_id, category_type, is_allowed
            )

    logger.info(
        f"Bulk updated {len(categories)} categories for account {account_id}"
    )
