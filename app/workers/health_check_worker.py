import asyncio
import logging
import random

import httpx
from sqlalchemy import select, update, or_, text, func

from app.config import settings
from app.db.database import worker_session_factory
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
                    headers={"User-Agent": settings.XTREAM_USER_AGENT},
                    limits=httpx.Limits(
                        max_connections=max(50, settings.STREAM_VALIDATION_CONCURRENCY * 2),
                        # No keep-alive: use a fresh connection per probe, like a
                        # real player (VLC) does. Some Xtream providers — notably
                        # low `max_connections` ones — drop reused keep-alive
                        # connections mid-response, which httpx surfaces as
                        # `ReadError`. With keep-alive on, that made ~75-98% of a
                        # such-provider's streams appear broken and tripped the
                        # circuit breaker on every run (the account was never
                        # actually validated), while the very same stream URLs
                        # returned HTTP 206 video when fetched on a fresh
                        # connection. Reproduced deterministically: 12 sequential
                        # probes → keep-alive ON = 3/12 ok (9 ReadError), OFF =
                        # 12/12 ok. Providers that tolerate keep-alive are
                        # unaffected (fresh connections work everywhere).
                        max_keepalive_connections=0,
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


def _account_concurrency(account) -> int:
    """Validation concurrency clamped to the account's `max_connections`.

    Probing more streams in parallel than the provider allows does NOT validate
    faster — it just trips the provider's connection cap, which answers `503`
    (or drops the connection → timeout/ReadError). Those throttle responses then
    look like dead streams and, across `STREAM_BROKEN_THRESHOLD` runs, get a
    perfectly playable stream wrongly marked `is_broken`. So mirror the sync
    worker (PR #11): never open more concurrent checks than `max_connections`.

    `max_connections=0` means the provider sets no limit → fall back to the
    global `STREAM_VALIDATION_CONCURRENCY`, which also serves as the upper bound
    (the validator's own resource cap) when a provider advertises a huge limit.
    """
    mc = account.max_connections or 0
    if mc > 0:
        return max(1, min(mc, settings.STREAM_VALIDATION_CONCURRENCY))
    return settings.STREAM_VALIDATION_CONCURRENCY


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
        except httpx.InvalidURL:
            # Provider answered with a malformed redirect (bad Location header)
            # that httpx can't follow — the stream is effectively broken. Expected
            # provider-side garbage, so classify it without a noisy warning.
            return item, True, "bad_redirect"
        except Exception as e:
            # Many remote-IPTV failures (RemoteProtocolError, ReadError, SSL…)
            # stringify to an empty message — include the type so the log line
            # is actionable instead of "...: " with nothing after the colon.
            logger.warning(
                f"Health check error for {item.rating_key}: {type(e).__name__}: {e}"
            )
            return item, True, f"exception:{type(e).__name__}"


# Serializes the two stream-validation writers (the hour=2 cron run() and the
# pipeline run_pipeline_validation()). Both UPDATE `media` and, when they
# overlap, the second writer can starve past the 60s busy_timeout and raise
# "database is locked", crashing the cron job (dette CR-F24). Same process /
# event loop, so a module-level asyncio.Lock is enough.
_VALIDATION_LOCK = asyncio.Lock()


async def run():
    """Cron entrypoint for the random-sample health check.

    Skips this run entirely when a pipeline stream validation is already
    holding the validation lock — the cron is a best-effort re-check, so
    skipping one cycle is preferable to contending for the SQLite write lock.

    Gated by STREAM_VALIDATION_ENABLED like the pipeline validator, so the
    flag turns off *all* stream checking (cron + pipeline), not just the
    pipeline pass.
    """
    if not settings.STREAM_VALIDATION_ENABLED:
        logger.info("Health check (cron) disabled (STREAM_VALIDATION_ENABLED=false)")
        return
    if _VALIDATION_LOCK.locked():
        logger.info("Health check (cron) skipped — stream validation already in progress")
        return
    async with _VALIDATION_LOCK:
        await _run_health_check_batch()


def _stream_candidate_filters(cutoff: int) -> tuple:
    """Shared WHERE clauses for stale/unchecked movie+episode streams."""
    return (
        Media.server_id.like("xtream_%"),
        Media.type.in_(["movie", "episode"]),
        or_(
            Media.last_stream_check.is_(None),
            Media.last_stream_check < cutoff,
        ),
    )


async def _sample_stream_candidates(db, batch_size: int, cutoff: int) -> list:
    """Randomly sample up to `batch_size` stale/unchecked streams.

    CR-P06: the previous `ORDER BY random()` assigns a random key to and
    fully sorts *every* candidate row just to take the top `batch_size` — a
    full scan + filesort on every cron run. Instead, pick a random `rowid`
    anchor and scan forward from it: SQLite's implicit rowid is the table's
    natural B-tree order, so `rowid >= :anchor ORDER BY rowid` is an index
    range scan, not a sort. Wrap around to the start of the table when the
    forward scan doesn't yield enough matches (e.g. anchor lands near the
    end), so small/late candidate sets are still filled. A fresh anchor is
    picked every call, so the sample rotates across runs — good-enough
    randomness for a background sampler; coverage matters more than perfect
    uniformity.
    """
    filters = _stream_candidate_filters(cutoff)

    max_rowid = (await db.execute(text("SELECT MAX(rowid) FROM media"))).scalar()
    if not max_rowid:
        return []

    anchor = random.randint(0, max_rowid)

    fwd_stmt = (
        select(Media)
        .where(*filters, text("media.rowid >= :anchor").bindparams(anchor=anchor))
        .order_by(text("media.rowid ASC"))
        .limit(batch_size)
    )
    items = list((await db.execute(fwd_stmt)).scalars().all())

    if len(items) < batch_size:
        remaining = batch_size - len(items)
        wrap_stmt = (
            select(Media)
            .where(*filters, text("media.rowid < :anchor").bindparams(anchor=anchor))
            .order_by(text("media.rowid ASC"))
            .limit(remaining)
        )
        items.extend((await db.execute(wrap_stmt)).scalars().all())

    return items


async def _run_health_check_batch():
    """Check a batch of stream URLs for availability (cron job, random sample)."""
    batch_size = settings.HEALTH_CHECK_BATCH_SIZE
    timeout = float(settings.STREAM_VALIDATION_TIMEOUT)
    concurrency = settings.STREAM_VALIDATION_CONCURRENCY
    cutoff = now_ms() - 7 * 24 * 3600 * 1000  # 7 days ago

    logger.info(
        f"Starting health check (batch size: {batch_size}, "
        f"concurrency: ≤{concurrency}/account, timeout: {timeout}s)"
    )

    async with worker_session_factory() as db:
        items = await _sample_stream_candidates(db, batch_size, cutoff)

        if not items:
            logger.info("No streams to check")
            return

        # Pre-load all needed accounts in one query
        account_ids = {item.server_id.replace("xtream_", "") for item in items}
        acc_result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id.in_(list(account_ids)))
        )
        accounts = {acc.id: acc for acc in acc_result.scalars().all()}

        # One semaphore PER account, sized to its max_connections — a shared
        # global semaphore would let a low-limit provider be hammered by checks
        # destined for another account. See _account_concurrency.
        semaphores = {
            acc_id: asyncio.Semaphore(_account_concurrency(acc))
            for acc_id, acc in accounts.items()
        }
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
            tasks.append(_check_one(client, item, account, semaphores[account_id]))

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

    Holds _VALIDATION_LOCK for its whole duration so the hour=2 cron run()
    skips instead of contending for the SQLite write lock (dette CR-F24).
    """
    async with _VALIDATION_LOCK:
        await _run_pipeline_validation_impl()


async def _run_pipeline_validation_impl():
    if not settings.STREAM_VALIDATION_ENABLED:
        logger.info("Stream validation disabled (STREAM_VALIDATION_ENABLED=false)")
        return

    concurrency = settings.STREAM_VALIDATION_CONCURRENCY
    timeout = float(settings.STREAM_VALIDATION_TIMEOUT)
    recheck_cutoff = now_ms() - settings.STREAM_VALIDATION_RECHECK_HOURS * 3600 * 1000

    logger.info(
        f"Starting pipeline stream validation "
        f"(concurrency: ≤{concurrency}/account, timeout: {timeout}s, "
        f"recheck window: {settings.STREAM_VALIDATION_RECHECK_HOURS}h)"
    )

    total_checked = 0
    total_broken = 0
    total_recovered = 0
    diag_reasons: dict[str, int] = {}

    # Shared WHERE for the stale/unchecked candidate set (movies + episodes).
    candidate_filters = (
        Media.server_id.like("xtream_%"),
        Media.type.in_(["movie", "episode"]),
        Media.is_in_allowed_categories == True,  # noqa: E712
        or_(
            Media.last_stream_check.is_(None),
            Media.last_stream_check < recheck_cutoff,
        ),
    )

    async with worker_session_factory() as db:
        # CR-P05: the previous version selected EVERY stale/unchecked row for
        # ALL accounts with `.scalars().all()` into one Python list before
        # grouping by account — at hundreds of thousands of movie/episode
        # rows that's a large transient memory spike against the 2 GB
        # container cap. Processing is already sequential per account (one
        # account's tasks in flight at a time), so the full multi-account set
        # never needs to be resident at once: a narrow COUNT (for the log)
        # and a DISTINCT server_id projection (no row hydration) are enough
        # to know *what* to validate, and each account's rows are then
        # streamed (`yield_per`) and processed one account at a time — peak
        # memory is bounded by the largest single account's batch, not the
        # whole candidate set.
        total_result = await db.execute(
            select(func.count()).select_from(Media).where(*candidate_filters)
        )
        total_candidates = total_result.scalar_one()

        if not total_candidates:
            logger.info("No streams need validation")
            return

        logger.info(f"Found {total_candidates} streams to validate")

        distinct_result = await db.execute(
            select(Media.server_id).where(*candidate_filters).distinct()
        )
        candidate_server_ids = [row[0] for row in distinct_result.all()]

        # Pre-load all needed accounts
        account_ids = {sid.replace("xtream_", "") for sid in candidate_server_ids}
        acc_result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id.in_(list(account_ids)))
        )
        accounts = {acc.id: acc for acc in acc_result.scalars().all()}

        # Detach now: `accounts` (and, per account below, its streamed rows)
        # are only ever read from here on (URL building, concurrency clamp,
        # before/after values) — the actual writes go through separate
        # `update(Media)` Core statements, never through these ORM instances.
        # If left attached, a later circuit breaker trip on one account calls
        # `db.rollback()`, which expires every instance still tracked by the
        # session (all accounts' and all remaining items', not just the
        # tripped account's) — and the very next plain attribute read (e.g.
        # the next account's `.max_connections`) would then try to lazily
        # refresh from the DB, which raises `MissingGreenlet` outside an
        # explicit await. Detaching keeps the already-loaded values usable no
        # matter how many accounts trip later in this run (CR-F08: rolling
        # re-evaluation makes trips far more likely than the old
        # one-shot-at-50 check).
        db.expunge_all()

        commit_interval = 200  # Commit and log every N results
        log_interval = 50  # Log progress every N results
        # CR-F08: the breaker used to evaluate the failure rate a single time,
        # at exactly the 50th check — accounts with fewer than 50 streams
        # never tripped it, and an outage starting *after* the 50th check was
        # invisible to it. Instead, evaluate on a rolling basis (every check,
        # once a minimum sample has been gathered) so it reacts to outages at
        # any point in the run and covers small accounts too, while still
        # requiring enough samples to avoid over-tripping on a handful of
        # unlucky checks.
        circuit_breaker_min_sample = 10  # don't evaluate below this many checks
        circuit_breaker_threshold = 0.90  # abort if failure rate >= this
        circuit_tripped = False

        client = await _get_client()
        for server_id in candidate_server_ids:
            account_id = server_id.replace("xtream_", "")
            account = accounts.get(account_id)
            if not account:
                continue

            # Stream just THIS account's candidates (server-side cursor,
            # yield_per batches) instead of slicing a pre-loaded, all-account
            # list — bounds peak memory to one account's batch rather than
            # every account's combined candidate set.
            acc_stream = await db.stream(
                select(Media)
                .where(*candidate_filters, Media.server_id == server_id)
                .order_by(Media.last_stream_check.asc().nullsfirst())
                .execution_options(yield_per=1000)
            )
            account_items = [row async for row in acc_stream.scalars()]
            if not account_items:
                continue
            # See detach note above — this account's freshly streamed rows
            # must also be detached before its own commits (or a later
            # account's rollback) expire them.
            db.expunge_all()

            # Per-account semaphore clamped to the provider's max_connections —
            # checking wider than the provider allows only yields 503/timeout
            # throttle noise (false "broken"), not faster validation. See
            # _account_concurrency.
            acct_concurrency = _account_concurrency(account)
            semaphore = asyncio.Semaphore(acct_concurrency)
            logger.info(
                f"Validating account {account_id}: {len(account_items)} streams "
                f"@ concurrency {acct_concurrency} "
                f"(max_connections={account.max_connections})"
            )

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

                # Circuit breaker: once enough checks have accumulated for this
                # account, re-evaluate the failure rate after *every* check
                # (rolling, not a one-shot sample at a fixed count) — the
                # server is likely down if it stays at/above threshold.
                if account_checked >= circuit_breaker_min_sample:
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
                        f"Validation progress: {total_checked}/{total_candidates} "
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
