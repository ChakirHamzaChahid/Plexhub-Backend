from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings

DATABASE_URL = f"sqlite+aiosqlite:///{settings.DB_PATH}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# Module-level state recording whether sqlite-vec was successfully loaded on
# the most recent connect. Consumed by J3b's verify_api_key dependency to
# short-circuit AI endpoints with HTTP 503 when the extension is unavailable.
_VEC_LOADED: dict[str, object] = {"ok": False, "error": ""}


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


async def init_db():
    """Create all tables with optimized SQLite settings."""
    import logging
    from sqlalchemy import text
    from app.models.database import Base
    from app.db.migrations import run_migrations

    logger = logging.getLogger("plexhub.db")

    register_sqlite_vec_listener(engine)

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.execute(text("PRAGMA cache_size=-64000"))  # 64MB cache
        await conn.execute(text("PRAGMA temp_store=MEMORY"))
        await conn.execute(text("PRAGMA busy_timeout=5000"))  # Wait 5s on lock instead of failing
        await conn.execute(text("PRAGMA mmap_size=268435456"))  # 256MB mmap for read perf
        await conn.run_sync(Base.metadata.create_all)

    # Run migrations after tables are created
    await run_migrations(engine)
