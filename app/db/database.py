import weakref

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings

DATABASE_URL = f"sqlite+aiosqlite:///{settings.DB_PATH}"

# API-facing engine — serves request handlers through get_db(). Explicit pool
# sizing gives a burst of concurrent reads real headroom (WAL readers are cheap
# and don't block each other), and an explicit, shorter pool_timeout makes a
# genuinely stuck checkout fail fast (10s) instead of hanging the default 30s.
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    # ``timeout`` is passed to sqlite3.connect() and sets the busy timeout on
    # EVERY pooled connection (busy_timeout is per-connection — the PRAGMA in
    # init_db only covered the init connection, so worker connections failed
    # immediately with "database is locked" while the long-running stream
    # validation held the single WAL writer lock). 60s lets a writer wait for
    # the gap between validation's per-batch commits instead of crashing.
    connect_args={"check_same_thread": False, "timeout": 60},
    pool_size=20,
    max_overflow=30,
    pool_timeout=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)

# Dedicated engine for long-running background workers (stream validation,
# health check). Physically separate connection pool so a multi-hour
# validation pass — which holds one session open across thousands of slow
# per-stream network checks — can NEVER consume the connection slots that
# serve API requests. This is the root-cause fix for the intermittent
# `QueuePool limit of size 5 overflow 10 reached` 500s seen on
# /api/media/episodes while a validation run was in flight. Small pool:
# SQLite has a single writer, so the workers need only a couple of connections.
worker_engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 60},
    pool_size=2,
    max_overflow=2,
    pool_timeout=30,
    pool_recycle=1800,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Session factory bound to the worker engine — import this (instead of
# async_session_factory) from background workers so their DB work stays off
# the API pool.
worker_session_factory = async_sessionmaker(
    worker_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# Module-level state recording whether sqlite-vec was successfully loaded on
# the most recent connect. Consumed by J3b's verify_api_key dependency to
# short-circuit AI endpoints with HTTP 503 when the extension is unavailable.
_VEC_LOADED: dict[str, object] = {"ok": False, "error": ""}
_PRAGMA_REGISTERED_ENGINES: weakref.WeakSet = weakref.WeakSet()


def register_sqlite_pragmas_listener(target_engine) -> None:
    """Attach an idempotent SQLAlchemy 'connect' listener applying SQLite
    performance PRAGMAs on every newly opened connection (API + worker pools).
    """
    from sqlalchemy import event

    sync_engine = getattr(target_engine, "sync_engine", target_engine)
    if sync_engine in _PRAGMA_REGISTERED_ENGINES:
        return
    _PRAGMA_REGISTERED_ENGINES.add(sync_engine)

    @event.listens_for(sync_engine, "connect")
    def _apply_sqlite_pragmas(dbapi_conn, _record):
        import logging

        logger = logging.getLogger("plexhub.db")

        async def _do_pragmas(aiosqlite_conn):
            try:
                await aiosqlite_conn.execute("PRAGMA synchronous=NORMAL")
                await aiosqlite_conn.execute("PRAGMA cache_size=-64000")
                await aiosqlite_conn.execute("PRAGMA temp_store=MEMORY")
                await aiosqlite_conn.execute("PRAGMA mmap_size=268435456")
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite connect PRAGMA setup failed: %s", exc)

        if hasattr(dbapi_conn, "run_async"):
            dbapi_conn.run_async(_do_pragmas)


register_sqlite_pragmas_listener(engine)
register_sqlite_pragmas_listener(worker_engine)


def register_sqlite_vec_listener(engine) -> None:
    """Attach a SQLAlchemy 'connect' event listener that loads the sqlite-vec
    extension on every raw aiosqlite connection. Defensive: never raises;
    failures are recorded in _VEC_LOADED for verify_api_key (J3b) to return 503.

    The aiosqlite driver runs the underlying sqlite3 connection in a worker
    thread, so the listener dispatches the load through ``run_async`` to
    execute on that thread.
    """
    from sqlalchemy import event
    import logging
    logger = logging.getLogger("plexhub.ai")

    @event.listens_for(engine.sync_engine, "connect")
    def _load_sqlite_vec(dbapi_conn, _record):
        try:
            import sqlite_vec

            async def _do_load(aiosqlite_conn):
                await aiosqlite_conn.enable_load_extension(True)
                await aiosqlite_conn._execute(sqlite_vec.load, aiosqlite_conn._conn)
                await aiosqlite_conn.enable_load_extension(False)

            dbapi_conn.run_async(_do_load)
            _VEC_LOADED["ok"] = True
            _VEC_LOADED["error"] = ""
        except Exception as exc:
            _VEC_LOADED["ok"] = False
            _VEC_LOADED["error"] = str(exc)
            logger.warning("sqlite-vec load failed: %s — AI endpoints will return 503", exc)


async def get_db() -> AsyncSession:
    """FastAPI dependency for DB sessions."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(db_url: str | None = None):
    """Create all tables with optimized SQLite settings."""
    import logging
    from sqlalchemy import text
    from app.models.database import Base
    from app.db.migrations import run_migrations

    logger = logging.getLogger("plexhub.db")

    if db_url is not None:
        configure_engines(db_url)

    register_sqlite_vec_listener(engine)

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.execute(text("PRAGMA cache_size=-64000"))  # 64MB cache
        await conn.execute(text("PRAGMA temp_store=MEMORY"))
        await conn.execute(text("PRAGMA busy_timeout=60000"))  # Wait 60s on lock (matches connect_args timeout)
        await conn.execute(text("PRAGMA mmap_size=268435456"))  # 256MB mmap for read perf
        await conn.run_sync(Base.metadata.create_all)

    # Run migrations after tables are created
    await run_migrations(engine)


def configure_engines(db_url: str | None = None) -> tuple:
    """Reconfigure module engines and session factories for a given DB URL (CR-T04)."""
    global DATABASE_URL, engine, worker_engine
    target_url = db_url or f"sqlite+aiosqlite:///{settings.DATA_DIR / 'plexhub.db'}"
    DATABASE_URL = target_url
    engine = create_async_engine(
        target_url,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 60},
        pool_size=20,
        max_overflow=30,
        pool_timeout=10,
        pool_recycle=1800,
        pool_pre_ping=True,
    )
    worker_engine = create_async_engine(
        target_url,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 60},
        pool_size=2,
        max_overflow=2,
        pool_timeout=30,
        pool_recycle=1800,
    )
    register_sqlite_pragmas_listener(engine)
    register_sqlite_pragmas_listener(worker_engine)
    register_sqlite_vec_listener(engine)
    async_session_factory.configure(bind=engine)
    worker_session_factory.configure(bind=worker_engine)
    return engine, worker_engine
