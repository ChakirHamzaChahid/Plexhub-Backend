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
