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
from app.api import accounts, admin, ai, api_keys, categories, health, live, media, plex, stream, sync, tv_auth
from app.utils.request_context import RequestIdLogFilter, RequestIdMiddleware

APP_VERSION = "1.1.5"

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

    cutoff = int(time.time() * 1000)  # now in ms
    async with async_session_factory() as db:
        result = await db.execute(
            delete(EpgEntry).where(EpgEntry.end_time < cutoff)
        )
        await db.commit()
        logger.info(f"EPG cleanup: removed {result.rowcount} stale entries")


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
            scheduler.start()

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
# (unlike Pattern A). Both already enforce their own auth on every route:
#   - `ai.router` — its own /api/ai prefix + module-level verify_api_key.
#   - `api_keys.router` — its own /api/admin/keys prefix + module-level
#     verify_master_key (master secret only).
app.include_router(ai.router)
app.include_router(api_keys.router)

# Prometheus /metrics + per-request HTTP metrics
from app.utils.metrics import setup_instrumentator  # noqa: E402
setup_instrumentator(app)
