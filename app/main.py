import asyncio
import logging
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import settings
from app.db.database import init_db
from app.api import accounts, categories, health, media, plex, stream, sync

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
log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# Console handler (INFO level - less verbose)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(log_format))

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

# Apply to root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)  # Capture all DEBUG and above
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger.info("Logging configured: Console=INFO, File=DEBUG")


async def _auto_provision_xtream_account():
    """Create an Xtream account from env vars if it doesn't already exist."""
    import hashlib
    from sqlalchemy import select
    from app.db.database import async_session_factory
    from app.models.database import XtreamAccount
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

        # Authenticate to get server info
        class _Acc:
            base_url = settings.XTREAM_BASE_URL
            port = settings.XTREAM_PORT
            username = settings.XTREAM_USERNAME
            password = settings.XTREAM_PASSWORD

        try:
            auth_data = await xtream_service.authenticate(_Acc())
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Master Worker election via lock file."""
    lock_file = settings.DATA_DIR / "server_start.lock"
    is_master = False
    scheduler = None

    try:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Initialize database
        await init_db()
        logger.info("Database initialized")

        # Master election
        try:
            with open(lock_file, "x") as f:
                f.write(str(os.getpid()))
            is_master = True
        except FileExistsError:
            if time.time() - lock_file.stat().st_mtime > 1200:
                lock_file.unlink(missing_ok=True)
                with open(lock_file, "x") as f:
                    f.write(str(os.getpid()))
                is_master = True
            else:
                is_master = False

        # Auto-provision Xtream account from env vars
        if settings.has_xtream_env:
            await _auto_provision_xtream_account()

        if is_master:
            logger.info(f"[Worker {os.getpid()}] Master — Starting scheduler")

            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from app.workers import sync_worker, enrichment_worker, health_check_worker

            scheduler = AsyncIOScheduler()
            scheduler.add_job(
                sync_worker.run_all_accounts,
                "interval",
                hours=settings.SYNC_INTERVAL_HOURS,
                id="xtream_sync",
            )
            scheduler.add_job(
                enrichment_worker.run,
                "interval",
                hours=settings.SYNC_INTERVAL_HOURS,
                id="tmdb_enrichment",
            )
            scheduler.add_job(
                health_check_worker.run,
                "cron",
                hour=2,
                id="health_check",
            )
            scheduler.start()

            # Non-blocking initial sync, then enrichment
            async def initial_sync_then_enrich():
                await sync_worker.run_all_accounts()
                logger.info("Initial sync done — starting enrichment")
                await enrichment_worker.run()

            asyncio.create_task(initial_sync_then_enrich())
        else:
            logger.info(f"[Worker {os.getpid()}] Slave — Passive mode")

        yield

    finally:
        if is_master:
            lock_file.unlink(missing_ok=True)
            if scheduler:
                scheduler.shutdown(wait=False)

        # Close service clients
        from app.services.xtream_service import xtream_service
        from app.services.tmdb_service import tmdb_service

        await xtream_service.close()
        await tmdb_service.close()


app = FastAPI(
    title="PlexHub Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Routes
app.include_router(health.router, prefix="/api")
app.include_router(accounts.router, prefix="/api")
app.include_router(categories.router, prefix="/api")
app.include_router(media.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(sync.router, prefix="/api")
app.include_router(plex.router, prefix="/api")
