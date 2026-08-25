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
    kwargs: dict[str, object] = {
        # Drop connections killed by a DB or network restart instead of handing
        # them out to the next request. This is also what makes a serverless
        # Postgres that suspends when idle — Neon and friends — survive the
        # first request after it wakes, rather than answering it with a dead
        # connection from the pool.
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
        # Postgres and most poolers drop idle connections well before an hour;
        # recycling first means the app never discovers it the hard way.
        "pool_recycle": 1800,
    }
    connect_args = settings.db_connect_args
    if connect_args:
        kwargs["connect_args"] = connect_args
    return kwargs


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
