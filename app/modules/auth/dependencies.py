import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_context import set_audit_actor
from app.core.config import settings
from app.core.constants import API_TOKEN_PREFIX, TokenType
from app.core.database import AsyncSessionLocal, get_db
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.messages import ErrorMessage
from app.modules.auth import service as auth_service
from app.modules.rbac.permissions import role_has
from app.modules.users.models import User, UserCredentials

bearer_scheme = HTTPBearer(description="Paste the `access_token` returned by POST /auth/login.")
optional_bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_access_payload(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: DbSession,
) -> dict[str, Any]:
    """The claims behind the bearer, whoever minted it.

    Two kinds of credential arrive here. A JWT, which is decoded; and a personal
    API token, which is a database lookup that answers with the same claims. The
    branch is on the prefix rather than on a failed decode, so a malformed JWT
    still reads as a malformed JWT.

    Websockets deliberately do not pass through here: `authenticate_websocket`
    stays JWT-only, because a socket takes its token from the query string and
    a long-lived credential does not belong in an access log.
    """
    if credentials.credentials.startswith(API_TOKEN_PREFIX):
        return await auth_service.api_token_payload(db, credentials.credentials)
    try:
        payload: dict[str, Any] = jwt.decode(
            credentials.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError() from exc
    if payload.get("type") != TokenType.ACCESS:
        raise AuthenticationError(ErrorMessage.INVALID_TOKEN_TYPE)
    return payload


AccessPayload = Annotated[dict[str, Any], Depends(get_access_payload)]


# Routes a person holding an *issued* password may still reach. Everything else
# answers 403 until they choose their own.
#
#   set-initial-password  the route that clears the flag; blocking it would make
#                         the state unescapable
#   logout                walking away must always work
#   users/me              the frontend bootstraps the session with this on every
#                         mount, including on the set-password screen itself
_PASSWORD_CHANGE_ALLOWED = (
    "/auth/set-initial-password",
    "/auth/logout",
    "/users/me",
)


async def get_current_user(request: Request, payload: AccessPayload, db: DbSession) -> User:
    user = await _resolve_user(db, payload)
    await _assert_password_is_chosen(request, db, user)
    set_audit_actor(user.id, user.email)
    return user


async def _resolve_user(db: AsyncSession, payload: dict[str, Any]) -> User:
    subject = payload.get("user_id")
    if not isinstance(subject, str):
        raise AuthenticationError()
    try:
        user_id = uuid.UUID(subject)
    except ValueError as exc:
        raise AuthenticationError() from exc
    if await auth_service.is_token_revoked(db, payload.get("jti", "")):
        raise AuthenticationError(ErrorMessage.TOKEN_REVOKED)
    user = await db.get(User, user_id)
    if user is None or not user.is_active or user.is_deleted:
        raise AuthenticationError(ErrorMessage.USER_NOT_FOUND_OR_INACTIVE)
    return user


async def _assert_password_is_chosen(request: Request, db: AsyncSession, user: User) -> None:
    """Hold an account still using an issued password.

    **This, not the frontend redirect, is the boundary.** Sign-in has to succeed
    for the owner to be able to set a replacement, which means a working access
    token exists while an emailed credential is still live. A redirect alone
    would leave that token usable by anyone who closed the dialog — or who read
    the email.

    It lives in `get_current_user` because that is the one dependency every
    authenticated route already passes through: one check, all callers, and no
    router can forget it.
    """
    credentials = await db.get(UserCredentials, user.id)
    if credentials is None or not credentials.must_change_password:
        return
    if request.url.path.endswith(_PASSWORD_CHANGE_ALLOWED):
        return
    raise AuthorizationError("Set your own password before using the app")


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(optional_bearer_scheme)],
    db: DbSession,
) -> User | None:
    """The caller if they are signed in, None if not. Never raises.

    Used by routes that are public but richer when authenticated — nothing today
    depends on it being strict, and a 401 from an optional dependency would be a
    contradiction.
    """
    if credentials is None:
        return None
    try:
        payload: dict[str, Any] = jwt.decode(
            credentials.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        if payload.get("type") != TokenType.ACCESS:
            return None
        user = await _resolve_user(db, payload)
    except (jwt.PyJWTError, AuthenticationError):
        return None
    set_audit_actor(user.id, user.email)
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def require_permission(code: str) -> Callable[[User], Awaitable[User]]:
    """Dependency factory gating on one platform permission.

    Reads the role from the database row rather than the token, so a demotion
    takes effect on the next request instead of when the token happens to expire.
    """

    async def checker(current_user: CurrentUser) -> User:
        if not role_has(current_user.role, code, current_user.is_superuser):
            raise AuthorizationError()
        return current_user

    return checker


async def get_current_admin(current_user: CurrentUser) -> User:
    """Anyone who may reach the admin surface at all."""
    if not current_user.is_admin:
        raise AuthorizationError()
    return current_user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]


async def get_current_superadmin(current_user: CurrentUser) -> User:
    from app.core.constants import GlobalRole

    if not (current_user.is_superuser or current_user.role == GlobalRole.SUPERADMIN):
        raise AuthorizationError()
    return current_user


CurrentSuperadmin = Annotated[User, Depends(get_current_superadmin)]


# ── websocket authentication ─────────────────────────────────────────────────


async def authenticate_websocket(token: str) -> tuple[User, dict[str, Any]] | None:
    """Resolve a websocket's access token to a user and its claims, or None.

    The claims come back because the socket has to keep the token's `jti`: a
    websocket authenticates once and then lives for hours, so it needs a way to
    ask later whether that token has since been revoked.

    Websockets cannot carry an `Authorization` header from the browser, so the
    token arrives as a query parameter. That means it can land in access logs —
    which is why access tokens are short-lived and why the refresh token is
    never accepted here.

    Opens its own session: this runs outside the request dependency graph, and
    the socket then holds no connection for its whole lifetime (which may be
    hours) — it takes one only when it needs to write.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != TokenType.ACCESS:
        return None
    async with AsyncSessionLocal() as db:
        try:
            user = await _resolve_user(db, payload)
        except AuthenticationError:
            return None
        # The issued-password wall lives in `get_current_user`, which a socket
        # never passes through — so the one route that reads and writes the
        # whole document was the one route the wall did not cover. Someone who
        # only read the emailed password got 403 on `GET /projects` and a full
        # `hello` frame here.
        credentials = await db.get(UserCredentials, user.id)
        if credentials is not None and credentials.must_change_password:
            return None
        # Detach so the caller can read attributes after the session closes.
        db.expunge(user)
        return user, payload
