import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr

from app.core.constants import GlobalRole


class AdminProjectRow(BaseModel):
    """A project as the admin surface sees it: **metadata only**.

    Deliberately without `doc`. The product's promise is that a project is
    visible to the addresses on it and nobody else, and an admin screen that
    rendered the diagram would quietly break that. Operators need to know a
    project exists, how big it is and who owns it — not what it says.
    """

    id: uuid.UUID
    name: str
    owner_email: str | None = None
    member_count: int
    screen_count: int
    module_count: int
    doc_version: int
    is_archived: bool
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime | None = None


class PlatformStats(BaseModel):
    users_total: int
    users_active: int
    users_pending_invite: int
    admins: int
    projects_total: int
    projects_archived: int
    projects_deleted: int
    screens_total: int
    signups_last_7_days: int
    active_last_24_hours: int


class PasswordIssued(BaseModel):
    """Returned when the platform generates a password rather than taking one.

    The plaintext is shown **once**, here, because the operator may need to read
    it out to the person. It is never stored in readable form and never
    retrievable again; the account is flagged so it must be replaced on first
    use.
    """

    user_id: uuid.UUID
    email: EmailStr
    temporary_password: str | None = None
    emailed: bool = False


class RoleDescription(BaseModel):
    role: GlobalRole
    permissions: list[str]
