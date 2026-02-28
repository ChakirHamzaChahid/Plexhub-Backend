"""
Category management API endpoints.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.schemas import CategoryListResponse, CategoryUpdateRequest, CategoryResponse
from app.services.category_service import (
    get_categories,
    bulk_update_categories,
)
from app.services.xtream_service import xtream_service

logger = logging.getLogger("plexhub.api.categories")
router = APIRouter(prefix="/accounts", tags=["categories"])


@router.get("/{account_id}/categories", response_model=CategoryListResponse)
async def list_categories(
    account_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Get all categories for an account with current filter mode.
    
    Returns:
        CategoryListResponse with categories list and filter mode
    """
    try:
        categories, filter_mode = await get_categories(db, account_id)
        
        return CategoryListResponse(
            items=[CategoryResponse.model_validate(cat) for cat in categories],
            filter_mode=filter_mode,
        )
    except Exception as e:
        logger.error(f"Failed to list categories for account {account_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{account_id}/categories")
async def update_categories(
    account_id: str,
    request: CategoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Update category filter configuration for an account.
    
    Body should contain:
    - filterMode: "all", "whitelist", or "blacklist"
    - categories: list of category objects with categoryId, categoryType, isAllowed
    
    Returns:
        Success message
    """
    try:
        await bulk_update_categories(
            db,
            account_id,
            request.filter_mode,
            request.categories,
        )
        
        return {"message": "Category configuration updated successfully"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update categories for account {account_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{account_id}/categories/refresh")
async def refresh_categories(
    account_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Force refresh categories from Xtream provider.
    
    Fetches current categories from Xtream and updates the database.
    Preserves existing is_allowed settings.
    
    Returns:
        Count of categories fetched
    """
    from app.models.database import XtreamAccount
    from sqlalchemy import select
    from app.services.category_service import upsert_category
    
    try:
        # Get account details
        stmt = select(XtreamAccount).where(XtreamAccount.id == account_id)
        result = await db.execute(stmt)
        account = result.scalar_one_or_none()
        
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
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
        
        total_count = len(vod_categories) + len(series_categories)
        logger.info(f"Refreshed {total_count} categories for account {account_id}")
        
        return {
            "message": "Categories refreshed successfully",
            "vod_count": len(vod_categories),
            "series_count": len(series_categories),
            "total": total_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to refresh categories for account {account_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
