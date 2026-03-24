import asyncio
import logging

logger = logging.getLogger("plexhub.tasks")

# Strong reference set — prevents GC from silently cancelling fire-and-forget tasks
_background_tasks: set[asyncio.Task] = set()


def create_background_task(coro, *, name: str | None = None) -> asyncio.Task:
    """Create an asyncio task with a strong reference so it won't be GC'd.

    Usage:
        create_background_task(sync_account(account_id), name="sync_abc123")
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_task_done)
    return task


def _task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        logger.debug(f"Background task cancelled: {task.get_name()}")
    elif exc := task.exception():
        logger.error(
            f"Background task failed: {task.get_name()}: {exc}",
            exc_info=exc,
        )


async def cancel_all_background_tasks(timeout: float = 10.0) -> None:
    """Cancel all background tasks and wait for them to finish (graceful shutdown)."""
    if not _background_tasks:
        return
    logger.info(f"Cancelling {len(_background_tasks)} background tasks...")
    for task in list(_background_tasks):
        task.cancel()
    results = await asyncio.gather(*list(_background_tasks), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
            logger.warning(f"Background task raised during shutdown: {r}")
    _background_tasks.clear()
    logger.info("All background tasks cancelled")
