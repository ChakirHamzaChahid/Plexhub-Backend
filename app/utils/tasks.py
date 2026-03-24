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


def cancel_task_by_name(name: str) -> bool:
    """Cancel a background task by its name. Returns True if found and cancelled."""
    for task in list(_background_tasks):
        if task.get_name() == name and not task.done():
            task.cancel()
            logger.info(f"Cancelled task: {name}")
            return True
    return False


async def cancel_all_background_tasks(timeout: float = 10.0) -> None:
    """Cancel all background tasks and wait up to timeout seconds for them to finish."""
    if not _background_tasks:
        return
    tasks = list(_background_tasks)
    logger.info(f"Cancelling {len(tasks)} background tasks (timeout={timeout}s)...")
    for task in tasks:
        task.cancel()
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.warning(f"Background task raised during shutdown: {r}")
    except asyncio.TimeoutError:
        logger.warning(f"Shutdown timeout ({timeout}s) — {len(_background_tasks)} tasks still running")
    _background_tasks.clear()
    logger.info("Background tasks cleanup done")
