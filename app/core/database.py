from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


def _engine_kwargs() -> dict[str, object]:
    url = settings.DATABASE_URL
    if url.startswith("sqlite"):
        # The test suite runs on one in-memory database shared by every
        # connection; without StaticPool each connection gets its own empty one.
        from sqlalchemy.pool import StaticPool

        return {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    return {
        # Drop connections killed by a DB or network restart instead of handing
        # them out to the next request.
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
        # Postgres and most poolers drop idle connections well before an hour;
        # recycling first means the app never discovers it the hard way.
        "pool_recycle": 1800,
    }


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    future=True,
    **_engine_kwargs(),
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
