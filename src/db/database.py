import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.logger import setup_logger

logger = setup_logger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://scraper:scraper@localhost:5432/scraper",
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=20, max_overflow=10)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    """Yield an async database session."""
    async with async_session() as session:
        yield session


async def init_db():
    """Create all tables defined in the metadata."""
    from src.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created.")


async def close_db():
    """Dispose of the engine connection pool."""
    await engine.dispose()
    logger.info("Database connection pool closed.")
