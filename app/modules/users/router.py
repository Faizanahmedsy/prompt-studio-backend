from fastapi import APIRouter, Request

from app.modules.auth.dependencies import CurrentUser, DbSession
from app.modules.users import service
from app.modules.users.models import UserCredentials
from app.modules.users.schemas import UserMe, UserUpdate

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me", response_model=UserMe)
async def read_me(db: DbSession, current_user: CurrentUser) -> UserMe:
    """The signed-in account. The frontend calls this on every mount to
    bootstrap the session, so it also carries the permission list the UI uses
    to decide what to render."""
    credentials = await db.get(UserCredentials, current_user.id)
    await service.touch_last_seen(db, current_user)
    count = await service.project_count(db, current_user.id)
    await db.commit()
    return service.to_me(
        current_user,
        must_change_password=bool(credentials and credentials.must_change_password),
        project_count=count,
    )


@router.patch("/me", response_model=UserMe)
async def update_me(
    data: UserUpdate, db: DbSession, current_user: CurrentUser, request: Request
) -> UserMe:
    user = await service.update_profile(db, current_user, data)
    credentials = await db.get(UserCredentials, user.id)
    count = await service.project_count(db, user.id)
    return service.to_me(
        user,
        must_change_password=bool(credentials and credentials.must_change_password),
        project_count=count,
    )
