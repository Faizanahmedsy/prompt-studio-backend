import asyncio
import logging

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging import configure_logging
from app.seed.accounts import seed_superadmin

logger = logging.getLogger("app.seed")


async def main() -> None:
    configure_logging(settings.LOG_LEVEL)
    async with AsyncSessionLocal() as db:
        await seed_superadmin(db)


if __name__ == "__main__":
    asyncio.run(main())
