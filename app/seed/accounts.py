import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import GlobalRole
from app.modules.users import service as user_service

logger = logging.getLogger("app.seed")


async def seed_superadmin(db: AsyncSession) -> None:
    """Ensure the founding account exists.

    Idempotent by design — it runs on every container start. An existing
    account is left completely alone: re-applying the seeded password on each
    boot would silently undo a password change, and re-applying the role would
    undo a deliberate demotion.
    """
    email = user_service.normalise_email(settings.SUPERADMIN_EMAIL)
    existing = await user_service.get_by_email(db, email)
    if existing is not None:
        logger.info("[seed] superadmin %s already exists", email)
        return

    user = await user_service.create_user(
        db,
        email=email,
        password=settings.SUPERADMIN_PASSWORD,
        full_name=settings.SUPERADMIN_NAME,
        role=GlobalRole.SUPERADMIN,
        is_superuser=True,
        email_verified=True,
    )
    await db.commit()
    logger.info("[seed] created superadmin %s (%s)", user.email, user.id)
