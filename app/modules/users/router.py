import uuid

from fastapi import APIRouter, Request, status

from app.core.envelope import set_response_message
from app.core.messages import ResponseMessage
from app.modules.auth import service as auth_service
from app.modules.auth.dependencies import CurrentUser, DbSession
from app.modules.auth.schemas import ApiTokenCreate, ApiTokenCreated, ApiTokenRead
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


# ── personal API tokens ──────────────────────────────────────────────────────
#
# For callers that are not a browser: CI, `weaver push`, curl. A token carries
# the account's full rights — there are no scopes — so the list screen exists to
# make "which of these is still live" answerable, and revoking is one call.


@router.post("/me/tokens", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    data: ApiTokenCreate, db: DbSession, current_user: CurrentUser, request: Request
) -> ApiTokenCreated:
    """Mint a token. The plaintext is in this response and nowhere else."""
    token, plaintext = await auth_service.create_api_token(db, current_user, data.name)
    set_response_message(request, ResponseMessage.TOKEN_CREATED)
    return ApiTokenCreated(**ApiTokenRead.model_validate(token).model_dump(), token=plaintext)


@router.get("/me/tokens", response_model=list[ApiTokenRead])
async def list_tokens(db: DbSession, current_user: CurrentUser) -> list[ApiTokenRead]:
    tokens = await auth_service.list_api_tokens(db, current_user)
    return [ApiTokenRead.model_validate(token) for token in tokens]


@router.delete("/me/tokens/{token_id}")
async def revoke_token(
    token_id: uuid.UUID, db: DbSession, current_user: CurrentUser, request: Request
) -> None:
    """Stops working immediately — the lookup that authenticates it filters on
    `revoked_at`, so there is no window like an access token's."""
    await auth_service.revoke_api_token(db, current_user, token_id)
    set_response_message(request, ResponseMessage.TOKEN_REVOKED)
