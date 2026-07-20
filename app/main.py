import asyncio
import logging
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import settings
from app.db.database import init_db
from app.api import (
    accounts,
    admin,
    admin_downloads,
    admin_plex_downloads,
    admin_unified_downloads,
    ai,
    api_keys,
    categories,
    downloads,
    enrichment,
    health,
    live,
    media,
    plex,
    plex_downloads,
    stream,
    sync,
    tv_auth,
)
from app.utils.request_context import RequestIdLogFilter, RequestIdMiddleware

APP_VERSION = "1.6.1"

logger = logging.getLogger("plexhub")


class SafeRotatingFileHandler(_RotatingFileHandler):
    """RotatingFileHandler that won't crash on Windows PermissionError.

    On Windows, log rotation fails if another process (e.g. VSCode, tail)
    has the file open. This handler catches the error and continues
    logging to the current file instead of crashing.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            # Another process holds the file — skip rotation, keep writing
            pass


# Configure logging (console + rotating file)
# request_id is "-" outside an HTTP request; injected by RequestIdLogFilter.
log_format = "%(asctime)s [%(request_id)s] [%(name)s] %(levelname)s: %(message)s"
_request_id_filter = RequestIdLogFilter()

# Console handler (INFO level - less verbose)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(log_format))
console_handler.addFilter(_request_id_filter)

# File handler with rotation (DEBUG level - detailed logs)
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
file_handler = SafeRotatingFileHandler(
    settings.LOG_DIR / "plexhub.log",
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=5,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(log_format))
file_handler.addFilter(_request_id_filter)

# Apply to plexhub logger (not root — avoids flooding with SQLAlchemy/httpx DEBUG)
plexhub_logger = logging.getLogger("plexhub")
plexhub_logger.setLevel(logging.DEBUG)
plexhub_logger.addHandler(console_handler)
plexhub_logger.addHandler(file_handler)
plexhub_logger.propagate = False  # Don't propagate to root (avoids duplicate logs)

# Root logger only for WARNING+ (third-party libraries)
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger.info("Logging configured: plexhub=DEBUG, third-party=WARNING")

# Mutual exclusion between the scheduled interval pipeline and the non-blocking
# boot-time initial run (CR-F04): APScheduler's max_instances=1 only serialises
# the interval job against itself, it does NOT know about the boot task started
# via create_background_task. Without this lock, a slow first sync can overlap
# with the first interval tick -> double TMDB budget spend + concurrent writers
# racing on the generated tree / .plex_mapping.json. Plain asyncio.Lock (no loop
# binding at import time on 3.10+), shared by both runners below.
_PIPELINE_LOCK = asyncio.Lock()


async def _auto_generate_plex_library():
    """Auto-generate Plex library for all active accounts if PLEX_LIBRARY_DIR is set.

    Thin wrapper (CR-A02): the generation wiring (`DatabaseSource` ->
    `PlexLibraryGenerator` -> `LocalStorage` -> `generate()`) plus the
    boot/schedule-time safety gating (skip if unconfigured / no active
    accounts) now live in `app.services.plex_generation_service`, shared with
    `app.api.sync`'s `/full-pipeline` endpoint — which used to reach into this
    private coroutine directly (layering inversion) and now calls the service
    instead.
    """
    from app.services.plex_generation_service import generate_plex_library_auto

    await generate_plex_library_auto()


async def _rebuild_unified_groups():
    """CR-P01: rebuild the precomputed media_group snapshot the unfiltered
    /movies|shows/unified browse endpoints page over.

    Runs at the end of the pipeline (after enrichment/generation) so the
    snapshot reflects the fully-enriched catalog — the same freshness as the
    generated Plex library. Non-fatal: the unified list falls back to live
    aggregation if this fails or hasn't run yet, so a failure never breaks
    browsing.
    """
    from app.services import unified_group_service
    from app.db.database import async_session_factory

    try:
        counts = await unified_group_service.rebuild_all(async_session_factory)
        logger.info("Unified-group snapshot rebuilt: %s", counts)
    except Exception as e:
        logger.error("Unified-group snapshot rebuild failed: %s", e, exc_info=True)


async def _auto_provision_xtream_account():
    """Create an Xtream account from env vars if it doesn't already exist."""
    import hashlib
    from sqlalchemy import select
    from app.db.database import async_session_factory
    from app.models.database import XtreamAccount
    from app.services.xtream_credentials import XtreamCredentials
    from app.services.xtream_service import xtream_service

    account_id = hashlib.md5(
        f"{settings.XTREAM_BASE_URL}{settings.XTREAM_USERNAME}".encode()
    ).hexdigest()[:8]

    async with async_session_factory() as db:
        result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id == account_id)
        )
        if result.scalars().first():
            logger.info(f"Xtream account {account_id} already exists (from env)")
            return

        # Authenticate to get server info (CR-C10: shared typed credentials
        # holder instead of a throwaway anonymous class).
        credentials = XtreamCredentials(
            base_url=settings.XTREAM_BASE_URL,
            port=settings.XTREAM_PORT,
            username=settings.XTREAM_USERNAME,
            password=settings.XTREAM_PASSWORD,
        )

        try:
            auth_data = await xtream_service.authenticate(credentials)
            user_info = auth_data.get("user_info", {})
            server_info = auth_data.get("server_info", {})
        except Exception as e:
            logger.error(f"Xtream auto-provision auth failed: {e}")
            return

        account = XtreamAccount(
            id=account_id,
            label="Auto (env)",
            base_url=settings.XTREAM_BASE_URL,
            port=settings.XTREAM_PORT,
            username=settings.XTREAM_USERNAME,
            password=settings.XTREAM_PASSWORD,
            status=user_info.get("status", "Unknown"),
            expiration_date=int(user_info["exp_date"]) * 1000
            if user_info.get("exp_date") else None,
            max_connections=int(user_info.get("max_connections", 1)),
            allowed_formats=",".join(user_info.get("allowed_output_formats", [])),
            server_url=server_info.get("url"),
            https_port=int(server_info["https_port"])
            if server_info.get("https_port") else None,
            is_active=True,
            created_at=int(time.time() * 1000),
        )
        db.add(account)
        await db.commit()
        logger.info(f"Xtream account auto-provisioned from env: {account_id}")


async def _cleanup_stale_epg():
    """Delete EPG entries whose end_time is in the past (stale programs)."""
    from sqlalchemy import delete
    from app.db.database import async_session_factory
    from app.models.database import EpgEntry
    from app.utils.db_retry import run_with_retry

    cutoff = int(time.time() * 1000)  # now in ms

    # This cron runs at hour=3, right after the hour=2 stream-validation cron,
    # whose long write transaction can still hold SQLite's single writer lock.
    # The DELETE below *opens* the write transaction, so contention raises
    # "database is locked" on the `execute` itself — before any commit — which
    # is why wrapping only the commit was not enough (the job crashed nightly).
    # Retry the whole open-a-session → delete → commit unit; each attempt gets a
    # fresh session so a failed attempt is cleanly rolled back and closed.
    async def _prune() -> int:
        async with async_session_factory() as db:
            result = await db.execute(
                delete(EpgEntry).where(EpgEntry.end_time < cutoff)
            )
            await db.commit()
            return result.rowcount

    rowcount = await run_with_retry(_prune, op="epg_cleanup")
    logger.info(f"EPG cleanup: removed {rowcount} stale entries")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Master Worker election via file lock (fcntl.flock)."""
    import fcntl

    lock_file = settings.DATA_DIR / "server_start.lock"
    is_master = False
    scheduler = None
    lock_fd = None

    try:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Initialize database
        await init_db()
        logger.info("Database initialized")

        # Boot summary — version, route count, sanitized config (no secrets).
        api_routes = sum(1 for r in app.routes if hasattr(r, "methods"))
        logger.info(
            f"PlexHub Backend v{APP_VERSION} starting — "
            f"{api_routes} routes, "
            f"sync_interval={settings.SYNC_INTERVAL_HOURS}h, "
            f"enrichment_daily_limit={settings.ENRICHMENT_DAILY_LIMIT}, "
            f"stream_validation={'on' if settings.STREAM_VALIDATION_ENABLED else 'off'} "
            f"(concurrency={settings.STREAM_VALIDATION_CONCURRENCY}), "
            f"plex_library_dir={'set' if settings.PLEX_LIBRARY_DIR else 'unset'}, "
            f"tmdb={'configured' if settings.TMDB_API_KEY else 'unset'}"
        )

        # Master election — atomic via fcntl.flock (no race condition)
        try:
            lock_fd = open(lock_file, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fd.write(str(os.getpid()))
            lock_fd.flush()
            is_master = True
        except OSError:
            # Another process holds the lock
            if lock_fd:
                lock_fd.close()
                lock_fd = None
            is_master = False

        # Auto-provision Xtream account from env vars
        if settings.has_xtream_env:
            await _auto_provision_xtream_account()

        if is_master:
            logger.info(f"[Worker {os.getpid()}] Master — Starting scheduler")

            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from app.workers import sync_worker, enrichment_worker, health_check_worker

            async def scheduled_sync_enrich_generate():
                """Periodic pipeline: sync -> enrichment -> validation -> Plex generation."""
                if _PIPELINE_LOCK.locked():
                    logger.warning(
                        "Scheduled pipeline skipped — a pipeline run (boot or previous "
                        "interval tick) is already in progress"
                    )
                    return
                async with _PIPELINE_LOCK:
                    try:
                        await sync_worker.run_all_accounts()
                        logger.info("Scheduled sync done — starting enrichment")
                        await enrichment_worker.run()
                        logger.info("Scheduled enrichment done — starting stream validation")
                        await health_check_worker.run_pipeline_validation()
                        logger.info("Scheduled validation done — starting Plex generation")
                        await _auto_generate_plex_library()
                        logger.info("Scheduled generation done — rebuilding unified-group snapshot")
                        await _rebuild_unified_groups()
                    except Exception as e:
                        logger.error(f"Scheduled sync pipeline failed: {e}", exc_info=True)

            scheduler = AsyncIOScheduler()
            # max_instances=1 prevents pipeline overlap if a run exceeds the interval.
            # coalesce=True collapses missed runs into a single one rather than queuing.
            # misfire_grace_time=300 still fires if the scheduler was late by < 5 min.
            scheduler.add_job(
                scheduled_sync_enrich_generate,
                "interval",
                hours=settings.SYNC_INTERVAL_HOURS,
                id="sync_enrich_generate",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
            scheduler.add_job(
                health_check_worker.run,
                "cron",
                hour=2,
                id="health_check",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            scheduler.add_job(
                _cleanup_stale_epg,
                "cron",
                hour=3,
                id="epg_cleanup",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

            async def _subtitle_cache_cleanup():
                from app.services import subtitle_service
                from app.db.database import async_session_factory
                await subtitle_service.cleanup_cache(async_session_factory)

            scheduler.add_job(
                _subtitle_cache_cleanup,
                "cron",
                hour=3,
                id="subtitle_cache_cleanup",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

            if settings.BACKUP_ENABLED:
                from app.scripts.backup_db import run_backup as _run_backup

                async def _scheduled_backup():
                    # sqlite3.Connection.backup is blocking — run in a thread to keep loop free.
                    await asyncio.to_thread(_run_backup)

                scheduler.add_job(
                    _scheduled_backup,
                    "cron",
                    hour=settings.BACKUP_HOUR,
                    id="db_backup",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=3600,
                )

            # Plex catalogue sync cron (feature "Télécharger Plex", ticket C7)
            # — OPTIONAL: only registered when BOTH the feature is configured
            # (`PLEX_ACCOUNT_TOKEN`) AND a periodic interval is requested
            # (`PLEX_SYNC_INTERVAL_HOURS > 0`, default 0 = manual-only via the
            # admin UI's "Sync" button, `plex_sync_service.run_full_sync`'s
            # own default). Master-only: this whole block sits under the
            # `if is_master:` guard that owns `scheduler` — same election as
            # every other cron here (house-law piège 7/17). No-op by
            # construction when the feature is disabled: nothing is added to
            # `scheduler` at all, so there's no job to skip/no-op at runtime.
            if settings.PLEX_ACCOUNT_TOKEN and settings.PLEX_SYNC_INTERVAL_HOURS > 0:

                async def _scheduled_plex_sync():
                    from app.services import plex_sync_service
                    from app.db.database import async_session_factory
                    # `run_full_sync` claims `plex_sync_status` itself (idle ->
                    # running) and returns early with status="already_running"
                    # if a sync is already in flight — no extra mutex needed
                    # here even though `max_instances=1` already prevents this
                    # job's own overlap.
                    await plex_sync_service.run_full_sync(async_session_factory)

                scheduler.add_job(
                    _scheduled_plex_sync,
                    "interval",
                    hours=settings.PLEX_SYNC_INTERVAL_HOURS,
                    id="plex_catalogue_sync",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                )

            scheduler.start()

            # Physical media download queue drain (PH-DL-06) — master-only,
            # same election as the scheduler above. Reap-then-drain is
            # combined into ONE coroutine handed to `create_background_task`
            # (never awaited directly here) so it never blocks lifespan
            # startup on a DB write, matching the non-blocking
            # `initial_sync_then_enrich` pattern just below; it also means
            # nothing touches the DB when `DOWNLOAD_DIR` is unset (feature
            # disabled — mirrors `run_drain_loop`'s own guard) or while this
            # coroutine sits unawaited in a test double.
            async def _run_download_worker():
                if not settings.DOWNLOAD_DIR:
                    logger.info("Download worker disabled: DOWNLOAD_DIR is not configured")
                    return
                from app.workers import download_worker
                from app.db.database import async_session_factory

                # CR-MIN-1 (review): `run_drain_loop` already reaps orphans
                # itself as its first step — an explicit call here was a
                # redundant double-reap on every boot. `run_drain_loop`'s own
                # `DOWNLOAD_DIR` guard makes this coroutine's own check above
                # technically redundant too, but it's kept so the log line
                # ("Download worker disabled...") fires without importing
                # `download_worker`/`async_session_factory` at all.
                await download_worker.run_drain_loop(async_session_factory)

            from app.utils.tasks import create_background_task
            create_background_task(_run_download_worker(), name="download_worker")

            # Plex catalogue sync status reap (feature "Télécharger Plex",
            # ticket C6) — a `plex_sync_status` row left `running` belongs to
            # a previous process instance that is definitely dead (mirrors
            # `plex_sync_service.reap_sync_status`'s own docstring); reap it
            # to `idle` at master boot so a stale "synchronisation…" badge
            # never gets stuck in the admin UI. Guarded by `PLEX_ACCOUNT_TOKEN`
            # so nothing touches the DB when the feature is unconfigured
            # (mirrors `run_full_sync`'s own no-op guard), and dispatched via
            # `create_background_task` so it never blocks lifespan startup —
            # same non-blocking convention as `_run_download_worker` above.
            # Deliberately independent of `DOWNLOAD_DIR`/`_run_download_worker`
            # (the Plex sync status can need reaping even when the physical
            # download queue itself is disabled).
            async def _reap_plex_sync():
                if not settings.PLEX_ACCOUNT_TOKEN:
                    return
                from app.services import plex_sync_service
                from app.db.database import async_session_factory

                await plex_sync_service.reap_sync_status(async_session_factory)

            create_background_task(_reap_plex_sync(), name="plex_sync_reap")

            # Non-blocking initial sync, then enrichment, then Plex generation
            async def initial_sync_then_enrich():
                if _PIPELINE_LOCK.locked():
                    logger.warning(
                        "Initial sync skipped — a pipeline run is already in progress"
                    )
                    return
                async with _PIPELINE_LOCK:
                    await sync_worker.run_all_accounts()
                    logger.info("Initial sync done — starting enrichment")
                    await enrichment_worker.run()
                    logger.info("Enrichment done — starting stream validation")
                    await health_check_worker.run_pipeline_validation()
                    logger.info("Validation done — starting Plex library generation")
                    await _auto_generate_plex_library()
                    logger.info("Generation done — rebuilding unified-group snapshot")
                    await _rebuild_unified_groups()

            from app.utils.tasks import create_background_task
            create_background_task(initial_sync_then_enrich(), name="initial_sync")
        else:
            logger.info(f"[Worker {os.getpid()}] Slave — Passive mode")

        yield

    finally:
        # Cancel and await background tasks before shutting down
        from app.utils.tasks import cancel_all_background_tasks
        await cancel_all_background_tasks()

        if is_master:
            if lock_fd:
                lock_fd.close()  # Releasing fd also releases flock
            lock_file.unlink(missing_ok=True)
            if scheduler:
                scheduler.shutdown(wait=False)

        # Close service clients
        from app.services.xtream_service import xtream_service
        from app.services.tmdb_service import tmdb_service
        from app.workers import health_check_worker

        await xtream_service.close()
        await tmdb_service.close()
        await health_check_worker.close()

        # Shutdown image download thread pool
        from app.plex_generator.storage import shutdown_image_pool
        shutdown_image_pool()


app = FastAPI(
    title="PlexHub Backend",
    version=APP_VERSION,
    lifespan=lifespan,
    # Public tunnel — disable the *unauthenticated* default docs so the API
    # surface isn't advertised. /docs + /openapi.json are re-added below behind
    # Basic Auth; /redoc stays off (404).
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

if settings.CORS_ORIGINS == ["*"]:
    logger.warning(
        "CORS_ORIGINS is '*' (default) — restrict to explicit origins in production "
        "deployments via the CORS_ORIGINS env var"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    # Explicit lists (CR-S06) instead of wildcards: covers the JSON API's real
    # verbs (GET/POST/PUT/PATCH/DELETE + the CORS preflight OPTIONS) and the
    # headers actually sent — X-API-Key (Android client + per-user keys),
    # Content-Type (JSON bodies), Authorization (admin/docs Basic Auth).
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type", "Authorization"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
# Added last so it wraps the others — request_id is set before any other middleware runs.
app.add_middleware(RequestIdMiddleware)

# ─── Routes / auth-per-router (CR-A04) ───────────────────────────────────
# Three mounting patterns coexist here by necessity (a public health check, a
# browser-facing admin UI needing Basic Auth instead of a custom header, and
# two routers that self-guard because they own a broader path prefix than
# "/api"). They are grouped and labelled explicitly below so the guard for
# every router is visible in this ONE place, without changing any path or
# which guard applies to which router. Tracked follow-up (not done here):
# a startup assertion walking `app.routes` to assert every `/api/*` route
# carries an auth dependency — see CR-A04,
# docs/audit/cleanroom-2026-07-11/10-architecture.md.
from app.api.deps import verify_admin_basic_auth, verify_backend_secret  # noqa: E402

# Shared X-API-Key guard for the JSON API (fail-closed, constant-time).
_guard = [Depends(verify_backend_secret)]

app.include_router(health.router, prefix="/api")  # Pattern B — public: monitoring

# Pattern A — guard attached at the mount site via `dependencies=_guard`.
app.include_router(accounts.router, prefix="/api", dependencies=_guard)
app.include_router(categories.router, prefix="/api", dependencies=_guard)
app.include_router(live.router, prefix="/api", dependencies=_guard)
app.include_router(media.router, prefix="/api", dependencies=_guard)
app.include_router(stream.router, prefix="/api", dependencies=_guard)
app.include_router(sync.router, prefix="/api", dependencies=_guard)
app.include_router(plex.router, prefix="/api", dependencies=_guard)
# Pattern B — public at router level (the TV has no key yet); /approve
# is individually protected inside tv_auth via verify_pairing_api_key.
app.include_router(tv_auth.router, prefix="/api")

# Admin web UI (HTML / HTMX) — no /api prefix. Browser-facing, so HTTP Basic
# Auth instead of the X-API-Key header (a navigation can't carry custom
# headers). Same mount-site-guard style as Pattern A, just a different
# dependency and no "/api" prefix.
app.include_router(admin.router, dependencies=[Depends(verify_admin_basic_auth)])
# Admin "Télécharger" tab (PH-DL-06) — already self-prefixed "/admin/downloads"
# by the router itself; same Basic Auth guard applied at the same mount site
# as the rest of /admin (identical convention, separate router module per the
# feature's disjoint-file-ownership rule, docs/20-impl-media-download.md §7.3).
app.include_router(admin_downloads.router, dependencies=[Depends(verify_admin_basic_auth)])
# Admin "Télécharger Plex" tab (ticket C6) — mirror of the block above, same
# self-prefixed "/admin/plex-downloads" + same Basic Auth guard at the same
# mount site; separate router module per the feature's disjoint-file-
# ownership convention.
app.include_router(admin_plex_downloads.router, dependencies=[Depends(verify_admin_basic_auth)])
# Admin unified "Téléchargements" tab (Vague W3) — merges the Xtream + Plex
# catalogues into one deduplicated browse screen; browse-only, delegates
# per-source enqueue to the two routers above. Same self-prefixed
# "/admin/unified-downloads" + Basic Auth guard convention.
app.include_router(admin_unified_downloads.router, dependencies=[Depends(verify_admin_basic_auth)])

# Interactive API docs — kept off the public default URLs above (docs_url=None …)
# and re-exposed here behind the SAME HTTP Basic Auth as /admin, so they're
# reachable for debugging on the LAN but never advertised to the public tunnel.
# The browser reuses the /admin Basic-Auth credentials for the Swagger UI's
# same-origin fetch of /openapi.json.
from fastapi.openapi.docs import get_swagger_ui_html  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402


@app.get("/docs", include_in_schema=False, dependencies=[Depends(verify_admin_basic_auth)])
async def protected_swagger_ui():
    return get_swagger_ui_html(openapi_url="/openapi.json", title="PlexHub Backend — API docs")


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(verify_admin_basic_auth)])
async def protected_openapi():
    return JSONResponse(app.openapi())

# Pattern C — bare mount, self-prefixed + self-guarded: the router module
# itself declares its full path prefix AND a module-level
# `dependencies=[Depends(...)]`, so no dependency is passed at the mount site
# (unlike Pattern A). All three already enforce their own auth on every route:
#   - `ai.router` — its own /api/ai prefix + module-level verify_api_key.
#   - `api_keys.router` — its own /api/admin/keys prefix + module-level
#     verify_master_key (master secret only).
#   - `downloads.router` (PH-DL-06) — its own /api/admin/downloads prefix +
#     module-level verify_master_key (JSON read mirror of the download
#     queue, master secret only — same convention as api_keys.router).
#   - `plex_downloads.router` (feature "Télécharger Plex", ticket C7) — its
#     own /api/admin/plex-downloads prefix + module-level verify_master_key
#     (JSON read-only mirror of the Plex catalogue/servers, master secret
#     only — same convention as downloads.router above).
#   - `enrichment.router` (dual-provider enrichment refacto, Wave 3) — its
#     own /api/admin/enrichment prefix + module-level verify_master_key
#     (manual OMDb ratings backfill: spends the OMDb budget + mutates the
#     whole catalog, master secret only — same convention as above). Not
#     master-gated at the worker level (admin-triggered, not scheduler/cron —
#     any process may run it, unlike the download workers).
app.include_router(ai.router)
app.include_router(api_keys.router)
app.include_router(downloads.router)
app.include_router(plex_downloads.router)
app.include_router(enrichment.router)

# Prometheus /metrics + per-request HTTP metrics
from app.utils.metrics import setup_instrumentator  # noqa: E402
setup_instrumentator(app)
