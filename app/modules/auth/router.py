import uuid

from fastapi import APIRouter, Request, status

from app.core.config import settings
from app.core.envelope import set_response_message
from app.core.messages import ResponseMessage
from app.core.rate_limit import REGISTER, RESET, SIGN_IN, limit
from app.modules.auth import service
from app.modules.auth.dependencies import AccessPayload, CurrentUser, DbSession
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SessionRead,
    SetInitialPasswordRequest,
    Token,
    VerifyEmailRequest,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/register",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[limit(REGISTER)],
)
async def register(data: RegisterRequest, db: DbSession, request: Request) -> LoginResponse:
    """Create an account and sign in.

    Pass `invite_token` from a project invitation link and the address is
    treated as confirmed — the invitation is proof the mailbox was reachable.
    """
    result = await service.register(db, data)
    set_response_message(request, ResponseMessage.REGISTERED)
    return result


@router.post("/login", response_model=LoginResponse, dependencies=[limit(SIGN_IN)])
async def login(data: LoginRequest, db: DbSession, request: Request) -> LoginResponse:
    result = await service.authenticate(db, data.email, data.password)
    set_response_message(request, ResponseMessage.LOGIN_SUCCESS)
    return result


@router.post("/refresh", response_model=Token)
async def refresh(data: RefreshRequest, db: DbSession, request: Request) -> Token:
    """Exchange a refresh token for a new pair. The old one stops working."""
    result = await service.refresh_tokens(db, data.refresh_token)
    set_response_message(request, ResponseMessage.TOKEN_REFRESHED)
    return result


@router.post("/logout")
async def logout(
    db: DbSession,
    payload: AccessPayload,
    request: Request,
    data: LogoutRequest | None = None,
) -> None:
    """Signing out with no body still works — a browser that already dropped
    its refresh token must not be answered with a validation error."""
    await service.logout(db, payload, data.refresh_token if data else None)
    set_response_message(request, ResponseMessage.LOGGED_OUT)


@router.post("/logout-everywhere")
async def logout_everywhere(
    db: DbSession, current_user: CurrentUser, request: Request
) -> dict[str, int]:
    revoked = await service.logout_everywhere(db, current_user)
    set_response_message(request, ResponseMessage.LOGGED_OUT)
    return {"sessions_revoked": revoked}


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(
    db: DbSession, current_user: CurrentUser, payload: AccessPayload
) -> list[SessionRead]:
    """Every device currently signed in as you.

    `is_current` marks the one you are reading this from, so the screen can say
    "this device" rather than leaving you to guess which row not to revoke —
    which now matters, because revoking a row ends its access token at once.
    """
    here = str(payload.get("jti", ""))
    return [
        SessionRead.model_validate(item).model_copy(
            update={"is_current": bool(item.access_jti) and item.access_jti == here}
        )
        for item in await service.list_sessions(db, current_user)
    ]


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: uuid.UUID, db: DbSession, current_user: CurrentUser, request: Request
) -> None:
    """200, not 204. Every other response in this API is enveloped, and one
    endpoint that answers with an empty body makes the client special-case it."""
    await service.revoke_session(db, current_user, session_id)
    set_response_message(request, ResponseMessage.LOGGED_OUT)


@router.post("/change-password")
async def change_password(
    data: ChangePasswordRequest, db: DbSession, current_user: CurrentUser, request: Request
) -> None:
    await service.change_password(db, current_user, data.current_password, data.new_password)
    set_response_message(request, ResponseMessage.PASSWORD_CHANGED)


@router.post("/set-initial-password")
async def set_initial_password(
    data: SetInitialPasswordRequest,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
    payload: AccessPayload,
) -> None:
    """Replace an admin-issued password with one you chose.

    The only authenticated route a flagged account may reach — everything else
    answers 403 until this succeeds.
    """
    # The caller's own `jti` is passed through so clearing the wall ends every
    # OTHER session minted from the emailed password, without signing this one
    # out mid-form.
    await service.set_initial_password(
        db, current_user, data.new_password, keep_jti=str(payload.get("jti", ""))
    )
    set_response_message(request, ResponseMessage.PASSWORD_CHANGED)


@router.post("/forgot-password", response_model=ForgotPasswordResponse, dependencies=[limit(RESET)])
async def forgot_password(
    data: ForgotPasswordRequest, db: DbSession, request: Request
) -> ForgotPasswordResponse:
    """Always answers the same, whether or not the address has an account."""
    token = await service.forgot_password(db, data.email)
    set_response_message(request, ResponseMessage.FORGOT_PASSWORD_SENT)
    # Only on a developer's own machine and in the test suite. This hands the
    # caller a working reset token for ANY address they name, so anywhere it is
    # reachable by someone else — staging included — it is account takeover,
    # not a convenience.
    #
    # "development" was in this set, and it is the default value of
    # ENVIRONMENT — so a staging box, a compose stack on an office LAN, or a
    # deploy where the variable was dropped handed anyone a working reset token
    # for any address, with no mailbox needed. A developer on their own machine
    # already has the token: the console mail transport prints it.
    if settings.ENVIRONMENT == "test":
        return ForgotPasswordResponse(reset_token=token)
    return ForgotPasswordResponse()


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest, db: DbSession, request: Request) -> None:
    await service.reset_password(db, data.token, data.new_password)
    set_response_message(request, ResponseMessage.PASSWORD_RESET)


@router.post("/verify-email")
async def verify_email(data: VerifyEmailRequest, db: DbSession, request: Request) -> None:
    await service.verify_email(db, data.token)
    set_response_message(request, ResponseMessage.EMAIL_VERIFIED)


@router.post("/resend-verification", dependencies=[limit(RESET)])
async def resend_verification(db: DbSession, current_user: CurrentUser, request: Request) -> None:
    await service.resend_verification(db, current_user)
    set_response_message(request, ResponseMessage.VERIFICATION_SENT)
