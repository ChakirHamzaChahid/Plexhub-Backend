import asyncio
import logging
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

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
    dav,
    downloads,
    enrichment,
    health,
    live,
    media,
    plex,
    plex_downloads,
    metadata_v1,
    scrape_v1,
    stream,
    sync,
    tv_auth,
)
from app.utils.request_context import RequestIdLogFilter, RequestIdMiddleware
from app.utils.job_health import mark_job_success, set_master, track_job

APP_VERSION = "1.17.6"

# First run of the opt-in `epg_sync` job, counted from boot (see lifespan).
_EPG_SYNC_FIRST_RUN_DELAY = timedelta(minutes=5)

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

# Explicit floor for httpx specifically (defense in depth, DAV-2 security
# review F3): at INFO, httpx logs the FULL request URL for every call it
# makes — including the DAV relay's (`app/dav/relay.py`) and the physical
# download service's Xtream stream URLs, which embed the account's
# username/password in the path/query. Root already sits at WARNING above,
# which today already filters this out (a logger with no explicit level
# inherits its nearest ancestor's), but that's implicit and one `root_logger
# .setLevel(logging.INFO)` edit away from silently leaking credentials into
# every log line. Pinning httpx's own level here means it stays WARNING
# regardless of what root/`plexhub` are ever changed to.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger.info("Logging configured: plexhub=DEBUG, third-party=WARNING")

# Mutual exclusion between the scheduled interval pipeline and the non-blocking
# boot-time initial run (CR-F04): APScheduler's max_instances=1 only serialises
# the interval job against itself, it does NOT know about the boot task started
# via create_background_task. Without this lock, a slow first sync can overlap
# with the first interval tick -> double TMDB budget spend + concurrent writers
# racing on the generated tree / .plex_mapping.json. Plain asyncio.Lock (no loop
# binding at import time on 3.10+), shared by both runners below.
_PIPELINE_LOCK = asyncio.Lock()


@asynccontextmanager
async def _pipeline_guard():
    """Hold `_PIPELINE_LOCK` AND flag the pipeline as owning the SQLite writer.

    Without the flag, the admin scraper's debounced snapshot rebuild competes
    with the pipeline for the single writer: its transaction reloads every row
    of a type and replaces ~25k snapshot rows, so the operator's own
    "Appliquer" can burn all three `write_with_retry` attempts (one full
    `busy_timeout=60s` each) and answer 500. Observed in production
    2026-09-20. Skipping costs nothing — every pipeline pass ends with
    `_rebuild_unified_groups`, which rebuilds every type."""
    from app.services import unified_group_service

    async with _PIPELINE_LOCK:
        unified_group_service.set_pipeline_active(True)
        try:
            yield
        finally:
            unified_group_service.set_pipeline_active(False)


from app.services.startup_tasks import (  # noqa: E402
    auto_generate_plex_library as _auto_generate_plex_library,
    auto_provision_xtream_account as _auto_provision_xtream_account,
    cleanup_stale_epg as _cleanup_stale_epg,
    rebuild_unified_groups as _rebuild_unified_groups,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Master Worker election via file lock (fcntl.flock)."""
    lock_file = settings.DATA_DIR / "server_start.lock"
    is_master = False
    scheduler = None
    lock_fd = None

    try:
        settings.ensure_dirs()

        # CR-O05 / CR-P04: intra-process locks, rate-limiters, and caches
        # assume a single worker process (--workers 1). Refuse or warn loudly
        # if WEB_CONCURRENCY > 1 is configured.
        web_concurrency = int(os.getenv("WEB_CONCURRENCY", "1") or "1")
        if web_concurrency > 1:
            raise RuntimeError(
                f"WEB_CONCURRENCY={web_concurrency} is unsupported: PlexHub requires "
                "--workers 1 (single-process SQLite/in-memory state, CR-O05/CR-P04)"
            )

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

        if not settings.BACKGROUND_JOBS_ENABLED:
            # Lot B2 (ADR 0012 D2.4): throwaway TEST instance (plexhubTMM). No
            # election at all — hence no `import fcntl`, which does not exist on
            # Windows — so no scheduler, no boot pipeline, no download worker, no
            # Plex reap; `is_master` stays False. `POST /api/accounts` still starts
            # its own account sync (independent of this lifespan). Default `true`.
            logger.warning(
                "BACKGROUND_JOBS_ENABLED=false — test instance: no master election, "
                "scheduler or background workers"
            )
        else:
            import fcntl

            # Master election — atomic via fcntl.flock (no race condition)
            try:
                lock_fd = open(lock_file, "w")
            except PermissionError as exc:
                logger.error(
                    "Cannot open master lock file %s (%s) — check host volume "
                    "permissions (expected UID:GID 1000:1000 on DATA_DIR); "
                    "refusing silent slave fallback",
                    lock_file,
                    exc,
                )
                raise

            try:
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

        # S5.1 (AUDIT-P8-005): publish the REAL election result verbatim —
        # `plexhub_is_master` must reflect a slave/OSError fallback exactly,
        # never re-decide mastership itself (piège §9-7 / AUDIT-P1-003).
        set_master(is_master)

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
                async with _pipeline_guard():
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
                        mark_job_success("pipeline")  # S5.1: only on a full, exception-free run
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
                track_job("health_check")(health_check_worker.run),
                "cron",
                hour=2,
                id="health_check",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            scheduler.add_job(
                track_job("epg_cleanup")(_cleanup_stale_epg),
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
                track_job("subtitle_cache_cleanup")(_subtitle_cache_cleanup),
                "cron",
                hour=3,
                id="subtitle_cache_cleanup",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

            # Lot A trailers — nightly LRU purge safety net (the same purge
            # already runs right after every successful download,
            # trailer_service._download_and_release; this cron catches drift
            # from a lowered TRAILER_CACHE_MAX_MB or a crash mid-download).
            # No-op by construction when the feature is disabled
            # (trailer_service.purge_lru returns 0 immediately).
            async def _trailer_cache_purge():
                from app.services import trailer_service
                await trailer_service.purge_lru()

            scheduler.add_job(
                track_job("trailer_cache_purge")(_trailer_cache_purge),
                "cron",
                hour=3,
                id="trailer_cache_purge",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

            from app.services.startup_tasks import cleanup_retention_caches as _cleanup_retention_caches

            scheduler.add_job(
                track_job("retention_cleanup")(_cleanup_retention_caches),
                "cron",
                hour=3,
                minute=30,
                id="retention_cleanup",
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
                    track_job("db_backup")(_scheduled_backup),
                    "cron",
                    hour=settings.BACKUP_HOUR,
                    id="db_backup",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=3600,
                )

            # Bulk EPG sync (Lot 10 sub-lot D, ADR 0006) -- OPT-IN: the job is
            # not even registered unless the operator sets EPG_SYNC_ENABLED=true
            # (no-op by default, same convention as DAV / backups). Master-only
            # by construction: this whole block sits under `if is_master:`.
            if settings.EPG_SYNC_ENABLED:
                from app.services import epg_sync_service

                scheduler.add_job(
                    track_job("epg_sync")(epg_sync_service.run_epg_sync_job),
                    "interval",
                    hours=max(1, settings.EPG_SYNC_INTERVAL_HOURS),
                    id="epg_sync",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=3600,
                    # A bare `interval` trigger first fires one full interval
                    # after boot: a container restarted more often than every
                    # EPG_SYNC_INTERVAL_HOURS would never sync. First run 5 min
                    # after boot instead (the live_channels it maps onto come
                    # from the boot-time sync).
                    next_run_time=datetime.now(timezone.utc) + _EPG_SYNC_FIRST_RUN_DELAY,
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
                    track_job("plex_catalogue_sync")(_scheduled_plex_sync),
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
                async with _pipeline_guard():
                    await sync_worker.run_all_accounts()
                    logger.info("Initial sync done — starting enrichment")
                    await enrichment_worker.run()
                    logger.info("Enrichment done — starting stream validation")
                    await health_check_worker.run_pipeline_validation()
                    logger.info("Validation done — starting Plex library generation")
                    await _auto_generate_plex_library()
                    logger.info("Generation done — rebuilding unified-group snapshot")
                    await _rebuild_unified_groups()
                    mark_job_success("pipeline")  # S5.1: same freshness gauge as the interval job

            from app.utils.tasks import create_background_task
            create_background_task(initial_sync_then_enrich(), name="initial_sync")
        elif settings.BACKGROUND_JOBS_ENABLED:
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
        from app.dav import relay as dav_relay
        from app.services import poster_match_service, ollama_service, notification_service

        await xtream_service.close()
        await tmdb_service.close()
        await health_check_worker.close()
        # DAV relay's pooled client (`app/dav/relay.py::get_client`) — a
        # no-op if `/dav` was never enabled/hit (lazy client, stays `None`).
        await dav_relay.close_client()
        # Manual-scrape poster-match's dedicated image client (ADR 0005 D5) —
        # a no-op if no poster was ever compared (lazy client, stays `None`).
        await poster_match_service.close()
        await ollama_service.close_client()
        await notification_service.close_client()

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
        "CORS_ORIGINS is '*' (wildcard) — restrict to explicit origins in production "
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
# Body size cap (64 KiB) for /api/scrape/v1/ ONLY — pure ASGI, see app/api/body_limit.py.
# Inserted between GZip and RequestId so the relative order of the existing middlewares is unchanged.
from app.api.body_limit import (  # noqa: E402
    DEFAULT_API_MAX_BODY_BYTES,
    METADATA_MAX_BODY_BYTES,
    METADATA_PREFIX,
    ScrapeBodyLimitMiddleware,
)
# ONE instance, per-prefix caps (B2: /api/metadata/v1/ 256 KiB, Lot 4 CR-S06: /api/* mutations 4 MiB).
app.add_middleware(
    ScrapeBodyLimitMiddleware,
    extra_limits={METADATA_PREFIX: METADATA_MAX_BODY_BYTES},
    default_api_limit=DEFAULT_API_MAX_BODY_BYTES,
)
# Added last so it wraps the others — request_id is set before any other middleware runs.
app.add_middleware(RequestIdMiddleware)
# CSRF guard for POST/PUT/PATCH/DELETE /admin* (S4.3, AUDIT-P2-005/CR-S07) — see app/api/csrf.py.
from app.api.csrf import AdminCsrfMiddleware  # noqa: E402
app.add_middleware(AdminCsrfMiddleware)

# ─── Routes / auth-per-router (CR-A02 / CR-A04) ──────────────────────────
# Two canonical mounting conventions (verified at boot by route_audit.py):
#   1. Mount-guarded routers (`_MOUNT_GUARDED_ROUTERS`): router carries no
#      module-level auth; the guard dependency is attached here at mount time.
#   2. Self- or route-guarded routers (`_SELF_OR_ROUTE_GUARDED_ROUTERS`):
#      router defines its own prefix + module-level or per-route guard
#      (or is explicitly allow-listed in `PUBLIC_API_ALLOWLIST`).
from app.api.deps import (  # noqa: E402
    verify_admin_basic_auth,
    verify_backend_secret,
    verify_metrics_basic_auth,
)

_MOUNT_GUARDED_ROUTERS = (
    (accounts.router, "/api", verify_backend_secret),
    (categories.router, "/api", verify_backend_secret),
    (live.router, "/api", verify_backend_secret),
    (media.router, "/api", verify_backend_secret),
    (stream.router, "/api", verify_backend_secret),
    (sync.router, "/api", verify_backend_secret),
    (plex.router, "/api", verify_backend_secret),
    (admin.router, "", verify_admin_basic_auth),
    (admin_downloads.router, "", verify_admin_basic_auth),
    (admin_plex_downloads.router, "", verify_admin_basic_auth),
    (admin_unified_downloads.router, "", verify_admin_basic_auth),
)

for _router, _prefix, _guard_fn in _MOUNT_GUARDED_ROUTERS:
    app.include_router(_router, prefix=_prefix, dependencies=[Depends(_guard_fn)])

_SELF_OR_ROUTE_GUARDED_ROUTERS = (
    (health.router, "/api"),
    (tv_auth.router, "/api"),
    (ai.router, ""),
    (api_keys.router, ""),
    (downloads.router, ""),
    (plex_downloads.router, ""),
    (enrichment.router, ""),
    (scrape_v1.router, ""),
    (metadata_v1.router, ""),
    (dav.router, ""),
)

for _router, _prefix in _SELF_OR_ROUTE_GUARDED_ROUTERS:
    app.include_router(_router, prefix=_prefix)

# Interactive API docs — kept off the public default URLs above (docs_url=None …)
# and re-exposed here behind the SAME HTTP Basic Auth as /admin.
from fastapi.openapi.docs import get_swagger_ui_html  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402


@app.get("/docs", include_in_schema=False, dependencies=[Depends(verify_admin_basic_auth)])
async def protected_swagger_ui():
    return get_swagger_ui_html(openapi_url="/openapi.json", title="PlexHub Backend — API docs")


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(verify_admin_basic_auth)])
async def protected_openapi():
    return JSONResponse(app.openapi())

# Boot-time route-auth audit (AUDIT-P4-001 / CR-A04): fails startup if any
# `/api/*` route above lacks a recognised auth dependency. All logic lives in
# app/api/route_audit.py; this call must stay LAST in the mounting block.
from app.api.route_audit import assert_api_routes_authenticated  # noqa: E402
assert_api_routes_authenticated(app)

# Prometheus /metrics + per-request HTTP metrics. Basic Auth guard (S4.1,
# AUDIT-P2-001/CR-S02): METRICS_USERNAME/METRICS_PASSWORD, escape hatch
# METRICS_PUBLIC=true — see app/api/deps.py::verify_metrics_basic_auth.
from app.utils.metrics import setup_instrumentator  # noqa: E402
setup_instrumentator(app, dependencies=[Depends(verify_metrics_basic_auth)])
