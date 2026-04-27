import asyncio
import logging

import httpx
from sqlalchemy import select, update, func, or_

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, XtreamAccount
from app.services.stream_service import build_stream_url
from app.utils.time import now_ms
from app.utils.db_retry import commit_with_retry

logger = logging.getLogger("plexhub.health_check")

# Module-level singleton — avoids creating a fresh AsyncClient (and its connection
# pool) on every run/run_pipeline_validation invocation.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(settings.STREAM_VALIDATION_TIMEOUT, connect=10.0),
                    limits=httpx.Limits(
                        max_connections=max(50, settings.STREAM_VALIDATION_CONCURRENCY * 2),
                        max_keepalive_connections=settings.STREAM_VALIDATION_CONCURRENCY,
                    ),
                )
    return _client


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None

# Known video container magic bytes
_MPEG_TS_SYNC = 0x47
_MP4_FTYP = b"ftyp"
_MKV_EBML = b"\x1a\x45\xdf\xa3"
_AVI_RIFF = b"RIFF"


def _looks_like_video(data: bytes) -> bool:
    """Quick heuristic: does the payload look like a video stream?"""
    if len(data) < 8:
        return False
    if data[0] == _MPEG_TS_SYNC:
        return True
    if data[4:8] == _MP4_FTYP:
        return True
    if data[:4] == _MKV_EBML:
        return True
    if data[:4] == _AVI_RIFF:
        return True
    # Large enough response from a 200/206 — assume valid even if format unknown
    if len(data) >= 1024:
        return True
    return False


def _content_type_is_video(content_type: str) -> bool:
    """Check if Content-Type header indicates a video/binary stream."""
    if not content_type:
        return False
    ct = content_type.lower().split(";")[0].strip()
    return ct in (
        "video/mp2t", "video/mp4", "video/x-matroska", "video/webm",
        "video/avi", "video/x-msvideo", "video/mpeg", "video/x-flv",
        "video/3gpp", "video/quicktime", "video/ogg",
        "application/octet-stream",
    )


def _content_type_is_error(content_type: str) -> bool:
    """Check if Content-Type indicates a non-video error response."""
    if not content_type:
        return False
    ct = content_type.lower().split(";")[0].strip()
    return ct in ("text/html", "application/json", "text/plain", "text/xml")


# Reasons that indicate a definitive failure — bypass STREAM_BROKEN_THRESHOLD
# and mark broken immediately. Transient errors (timeout, connect_error, 503)
# still require N consecutive failures before marking broken.
_DEFINITIVE_PREFIXES = (
    "head_404", "get_404",       # stream deleted on server
    "head_403", "get_403",       # access revoked
    "head_ct_error:", "get_ct_error:",  # error page (text/html, etc.)
    "get_empty",                 # 200 OK but empty body = dead stream
    "get_magic_fail:",           # bytes don't match any video format
)


def _is_definitive_failure(reason: str) -> bool:
    """Check if a failure reason is definitive (not transient)."""
    return any(reason.startswith(p) for p in _DEFINITIVE_PREFIXES)


# Diagnostic counters (reset each run)
_diag_reasons: dict[str, int] = {}


async def _check_one(client: httpx.AsyncClient, item, account, semaphore):
    """Check a single stream URL using HEAD first, then Range GET if needed.

    Returns (item, is_broken, reason).
    """
    url = build_stream_url(account, item.rating_key)
    if not url:
        return item, None, "no_url"

    async with semaphore:
        try:
            # Step 1: HEAD request — fast, checks if the resource exists
            head_resp = await client.head(url, follow_redirects=True)

            if head_resp.status_code >= 400:
                return item, True, f"head_{head_resp.status_code}"

            # If HEAD returns 200/206, check Content-Type
            # Don't trust Content-Type when Content-Length is 0 — Xtream
            # servers return 200 + application/octet-stream + empty body
            # for dead streams. Fall through to Range GET for verification.
            ct = head_resp.headers.get("content-type", "")
            content_length = head_resp.headers.get("content-length")
            if _content_type_is_video(ct) and content_length != "0":
                return item, False, "head_ct_video"

            if _content_type_is_error(ct):
                return item, True, f"head_ct_error:{ct.split(';')[0].strip()}"

            # Step 2: Content-Type ambiguous — do Range GET to inspect bytes
            async with client.stream(
                "GET",
                url,
                headers={"Range": "bytes=0-8191"},
                follow_redirects=True,
            ) as resp:
                if resp.status_code in (200, 206):
                    ct = resp.headers.get("content-type", "")
                    if _content_type_is_video(ct):
                        return item, False, "get_ct_video"
                    if _content_type_is_error(ct):
                        return item, True, f"get_ct_error:{ct.split(';')[0].strip()}"

                    # Read first chunk and check magic bytes
                    content = b""
                    async for chunk in resp.aiter_bytes(8192):
                        content = chunk
                        break
                    if len(content) > 0:
                        is_video = _looks_like_video(content)
                        if is_video:
                            return item, False, "get_magic_ok"
                        return item, True, f"get_magic_fail:{len(content)}b"
                    return item, True, "get_empty"
                elif resp.status_code == 416:
                    # Range not satisfiable but HEAD was OK — probably valid
                    return item, False, "get_416_head_ok"
                else:
                    return item, resp.status_code >= 400, f"get_{resp.status_code}"

        except httpx.TimeoutException:
            return item, True, "timeout"
        except httpx.ConnectError:
            return item, True, "connect_error"
        except Exception as e:
            logger.warning(f"Health check error for {item.rating_key}: {e}")
            return item, True, f"exception:{type(e).__name__}"


async def run():
    """Check a batch of stream URLs for availability (cron job, random sample)."""
    batch_size = settings.HEALTH_CHECK_BATCH_SIZE
    timeout = float(settings.STREAM_VALIDATION_TIMEOUT)
    concurrency = settings.STREAM_VALIDATION_CONCURRENCY
    cutoff = now_ms() - 7 * 24 * 3600 * 1000  # 7 days ago

    logger.info(f"Starting health check (batch size: {batch_size}, concurrency: {concurrency}, timeout: {timeout}s)")

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

        # Pre-load all needed accounts in one query
        account_ids = {item.server_id.replace("xtream_", "") for item in items}
        acc_result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id.in_(list(account_ids)))
        )
        accounts = {acc.id: acc for acc in acc_result.scalars().all()}

        semaphore = asyncio.Semaphore(concurrency)
        checked = 0
        broken_count = 0

        client = await _get_client()

        # Build tasks for all items
        tasks = []
        for item in items:
            account_id = item.server_id.replace("xtream_", "")
            account = accounts.get(account_id)
            if not account:
                continue
            tasks.append(_check_one(client, item, account, semaphore))

        # Run all health checks concurrently
        results = await asyncio.gather(*tasks)

        # Apply results to DB
        reasons: dict[str, int] = {}
        for item, is_broken, reason in results:
            if is_broken is None:
                continue
            reasons[reason] = reasons.get(reason, 0) + 1

            new_error_count = (
                (item.stream_error_count or 0) + 1 if is_broken else 0
            )
            definitive = is_broken and _is_definitive_failure(reason)
            mark_broken = (
                is_broken
                and (definitive or new_error_count >= settings.STREAM_BROKEN_THRESHOLD)
            )

            await db.execute(
                update(Media)
                .where(
                    Media.rating_key == item.rating_key,
                    Media.server_id == item.server_id,
                )
                .values(
                    is_broken=mark_broken,
                    last_stream_check=now_ms(),
                    stream_error_count=new_error_count,
                )
            )
            checked += 1
            if mark_broken:
                broken_count += 1

        await commit_with_retry(db)

    logger.info(
        f"Health check complete: {checked} checked, {broken_count} broken | "
        f"reasons: {reasons}"
    )


async def run_pipeline_validation():
    """Validate unchecked/stale streams as part of the sync pipeline.

    Unlike the cron-based run() which samples randomly, this targets
    streams that have never been checked or whose check is stale.
    Ensures new content is validated before Plex file generation.
    """
    if not settings.STREAM_VALIDATION_ENABLED:
        logger.info("Stream validation disabled (STREAM_VALIDATION_ENABLED=false)")
        return

    concurrency = settings.STREAM_VALIDATION_CONCURRENCY
    timeout = float(settings.STREAM_VALIDATION_TIMEOUT)
    recheck_cutoff = now_ms() - settings.STREAM_VALIDATION_RECHECK_HOURS * 3600 * 1000

    logger.info(
        f"Starting pipeline stream validation "
        f"(concurrency: {concurrency}, timeout: {timeout}s, "
        f"recheck window: {settings.STREAM_VALIDATION_RECHECK_HOURS}h)"
    )

    total_checked = 0
    total_broken = 0
    total_recovered = 0
    diag_reasons: dict[str, int] = {}

    async with async_session_factory() as db:
        # Find all unchecked or stale streams (movies + episodes only)
        result = await db.execute(
            select(Media)
            .where(
                Media.server_id.like("xtream_%"),
                Media.type.in_(["movie", "episode"]),
                Media.is_in_allowed_categories == True,
                or_(
                    Media.last_stream_check.is_(None),
                    Media.last_stream_check < recheck_cutoff,
                ),
            )
            .order_by(Media.last_stream_check.asc().nullsfirst())
        )
        items = list(result.scalars().all())

        if not items:
            logger.info("No streams need validation")
            return

        logger.info(f"Found {len(items)} streams to validate")

        # Pre-load all needed accounts
        account_ids = {item.server_id.replace("xtream_", "") for item in items}
        acc_result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id.in_(list(account_ids)))
        )
        accounts = {acc.id: acc for acc in acc_result.scalars().all()}

        semaphore = asyncio.Semaphore(concurrency)
        commit_interval = 200  # Commit and log every N results
        log_interval = 50  # Log progress every N results
        circuit_breaker_sample = 50  # Check failure rate after N results
        circuit_breaker_threshold = 0.90  # Abort if > 90% broken
        circuit_tripped = False

        # Group items by account for per-account circuit breaking
        items_by_account: dict[str, list] = {}
        for item in items:
            account_id = item.server_id.replace("xtream_", "")
            items_by_account.setdefault(account_id, []).append(item)

        client = await _get_client()
        for account_id, account_items in items_by_account.items():
            account = accounts.get(account_id)
            if not account:
                continue

            account_checked = 0
            account_broken = 0
            account_tripped = False

            # Build tasks for this account
            tasks = []
            for item in account_items:
                tasks.append(asyncio.ensure_future(
                    _check_one(client, item, account, semaphore)
                ))

            pending_updates = 0
            for coro in asyncio.as_completed(tasks):
                item, is_broken, reason = await coro
                if is_broken is None:
                    continue

                diag_reasons[reason] = diag_reasons.get(reason, 0) + 1
                account_checked += 1

                # Circuit breaker: after N checks, if failure rate > threshold,
                # the server is likely down — abort this account
                if (
                    account_checked == circuit_breaker_sample
                    and account_checked > 0
                ):
                    failure_rate = account_broken / account_checked
                    if failure_rate >= circuit_breaker_threshold:
                        logger.warning(
                            f"CIRCUIT BREAKER: account {account_id} has "
                            f"{failure_rate:.0%} failure rate after "
                            f"{account_checked} checks — server likely down. "
                            f"Skipping remaining {len(account_items) - account_checked} "
                            f"streams. No streams will be marked as broken."
                        )
                        account_tripped = True
                        # Rollback uncommitted changes for this account
                        await db.rollback()
                        pending_updates = 0
                        # Cancel remaining tasks and await them to
                        # release httpx connections cleanly
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        break

                was_broken = item.is_broken
                new_error_count = (
                    (item.stream_error_count or 0) + 1 if is_broken else 0
                )
                definitive = is_broken and _is_definitive_failure(reason)
                mark_broken = (
                    is_broken
                    and (definitive or new_error_count >= settings.STREAM_BROKEN_THRESHOLD)
                )

                if was_broken and not is_broken:
                    total_recovered += 1

                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                    )
                    .values(
                        is_broken=mark_broken,
                        last_stream_check=now_ms(),
                        stream_error_count=new_error_count,
                    )
                )
                total_checked += 1
                if mark_broken:
                    total_broken += 1
                if is_broken:
                    account_broken += 1
                pending_updates += 1

                # Log progress frequently
                if total_checked % log_interval == 0:
                    logger.info(
                        f"Validation progress: {total_checked}/{len(items)} "
                        f"({total_broken} broken so far) | "
                        f"reasons: {diag_reasons}"
                    )

                # Commit periodically to avoid huge transactions
                if pending_updates >= commit_interval:
                    await commit_with_retry(db)
                    pending_updates = 0

            if account_tripped:
                circuit_tripped = True
                continue

            # Commit remaining for this account
            if pending_updates > 0:
                await commit_with_retry(db)
                pending_updates = 0

            logger.info(
                f"Account {account_id}: {account_checked} checked, "
                f"{account_broken} broken"
            )
            # Update alive ratio gauge for this account (after commit so DB is consistent)
            if not account_tripped and account_checked > 0:
                from app.utils.metrics import streams_alive_ratio
                streams_alive_ratio.labels(account_id=account_id).set(
                    1.0 - (account_broken / account_checked)
                )

    if circuit_tripped:
        logger.warning(
            "Pipeline validation finished with circuit breaker tripped on "
            "one or more accounts — those accounts were skipped entirely"
        )

    logger.info(
        f"Pipeline validation complete: {total_checked} checked, "
        f"{total_broken} broken, {total_recovered} recovered | "
        f"reasons: {diag_reasons}"
    )
