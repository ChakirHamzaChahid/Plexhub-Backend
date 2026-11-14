"""
Category management API endpoints.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.schemas import (
    CategoryListResponse,
    CategoryRefreshResponse,
    CategoryUpdateRequest,
    CategoryResponse,
)
from app.services.category_service import (
    get_categories,
    bulk_update_categories,
    fetch_categories_from_provider,
    upsert_categories_batch,
    AccountNotFoundError,
)
from app.utils.db_retry import session_factory_for, write_with_retry

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


@router.post(
    "/{account_id}/categories/refresh",
    response_model=CategoryRefreshResponse,
    response_model_by_alias=True,
)
async def refresh_categories(
    account_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Force refresh categories from Xtream provider.

    Fetches current categories from Xtream and updates the database.
    Preserves existing is_allowed settings.

    Returns:
        CategoryRefreshResponse (camelCase on the wire: vodCount/seriesCount)
    """
    try:
        vod_categories, series_categories, new_allowed = await fetch_categories_from_provider(
            db, account_id
        )

        async def _work(session: AsyncSession) -> tuple[int, int]:
            counts = await upsert_categories_batch(
                session, account_id, vod_categories, series_categories, new_allowed
            )
            await session.commit()
            return counts

        vod_count, series_count = await write_with_retry(
            _work,
            session_factory=session_factory_for(db),
            op="categories.refresh",
        )

        total_count = vod_count + series_count
        logger.info(f"Refreshed {total_count} categories for account {account_id}")

        return CategoryRefreshResponse(
            message="Categories refreshed successfully",
            vod_count=vod_count,
            series_count=series_count,
            total=total_count,
        )
    except AccountNotFoundError:
        raise HTTPException(status_code=404, detail="Account not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to refresh categories for account {account_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
