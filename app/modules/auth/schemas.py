import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.constants import TOKEN_TYPE_BEARER
from app.modules.users.schemas import UserMe


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = TOKEN_TYPE_BEARER
    expires_in: int  # seconds until the access token dies


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: str = Field("", max_length=160)
    # Passed straight through from an invitation link. Present, it proves the
    # address really was invited, so the account skips email confirmation and
    # lands in the project immediately.
    invite_token: str | None = None


class LoginResponse(Token):
    user: UserMe
    # True while the password in use was issued rather than chosen. Login
    # SUCCEEDS (a token is needed to set the replacement) but every other route
    # answers 403 until `/auth/set-initial-password` clears it.
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    # Optional: signing out of a browser that has already dropped its refresh
    # token must still revoke the access token rather than 422.
    refresh_token: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


class SetInitialPasswordRequest(BaseModel):
    """Replace an issued password with one the owner chose.

    No `current_password`, unlike a change: the caller already proved possession
    of the issued one at sign-in, and asking again would mean the set-password
    screen had to hold it in memory and would break on a page refresh.
    """

    new_password: str = Field(min_length=8, max_length=72)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    # Outside production the token is returned directly so the flow is testable
    # with no mail delivery configured. In production it stays null and the only
    # copy is the one in the email.
    reset_token: str | None = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=72)


class VerifyEmailRequest(BaseModel):
    token: str


class SessionRead(BaseModel):
    """One signed-in device, for the "where am I signed in" screen."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_agent: str | None = None
    ip_address: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime
    is_current: bool = False


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="What this token is for")


class ApiTokenRead(BaseModel):
    """A token as it appears on the list — never its secret."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    revoked_at: datetime | None = None


class ApiTokenCreated(ApiTokenRead):
    """The one response that carries the plaintext. It is not stored, so this
    is the only chance anybody has to copy it."""

    token: str
