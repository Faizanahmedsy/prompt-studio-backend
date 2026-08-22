import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.constants import GlobalRole


class UserSummary(BaseModel):
    """The shape a user takes when they appear *inside* something else — a
    project member row, an activity entry, a presence list. Deliberately small:
    it is embedded many times per response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str = ""
    avatar_url: str | None = None
    accent_color: str = "#4f46e5"


class UserRead(UserSummary):
    role: GlobalRole
    is_active: bool
    is_superuser: bool
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime


class UserMe(UserRead):
    """`/users/me` — the caller, plus what the UI needs to decide what to render.

    `permissions` is sent so the frontend can hide an admin link it would only
    be 403'd on. It is a convenience, never the boundary: the same check runs
    server-side on every admin route.
    """

    timezone: str = "Asia/Kolkata"
    theme: str = "system"
    permissions: list[str] = []
    must_change_password: bool = False
    project_count: int = 0


class UserUpdate(BaseModel):
    """Self-service profile edits. Role and status are deliberately absent —
    those live on the admin routes so a member cannot promote themselves."""

    full_name: str | None = Field(None, max_length=160)
    avatar_url: str | None = Field(None, max_length=512)
    accent_color: str | None = Field(None, pattern=r"^#(?:[0-9a-fA-F]{3}){1,2}$")
    timezone: str | None = Field(None, max_length=64)
    theme: str | None = Field(None, pattern=r"^(light|dark|system)$")


class AdminUserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field("", max_length=160)
    role: GlobalRole = GlobalRole.MEMBER
    # Omitted means "generate one and email it". The account is then flagged
    # `must_change_password`, so the issued secret cannot be used to browse.
    password: str | None = Field(None, min_length=8, max_length=72)
    is_active: bool = True
    send_invite_email: bool = True


class AdminUserUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=160)
    role: GlobalRole | None = None
    is_active: bool | None = None
    # Setting this issues a new password and re-flags the account.
    reset_password: bool = False


class UserListItem(UserRead):
    """Admin list row: the account plus the numbers an operator scans for."""

    project_count: int = 0
    owned_project_count: int = 0
    last_seen_at: datetime | None = None
