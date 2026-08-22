import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import GlobalRole, MemberStatus
from app.core.exceptions import ConflictError, NotFoundError
from app.core.messages import ErrorMessage
from app.core.security import assert_password_strong, hash_password
from app.core.time import now
from app.modules.projects.models import Project, ProjectMember
from app.modules.rbac.permissions import permissions_for
from app.modules.users.models import User, UserCredentials
from app.modules.users.schemas import UserMe, UserRead, UserUpdate

# Cursor colours handed out in rotation, so two people in the same project are
# very unlikely to share one. Picked for contrast against both canvas themes.
ACCENT_COLORS = (
    "#4f46e5",
    "#0ea5e9",
    "#10b981",
    "#f59e0b",
    "#ef4444",
    "#8b5cf6",
    "#ec4899",
    "#14b8a6",
)


def normalise_email(email: str) -> str:
    """The one definition of "the same address".

    Lower-cased and trimmed. Every write and every lookup goes through this —
    sign-in, registration, project invitations — so an invitation sent to
    `Faizan@X.com` is found by an account created as `faizan@x.com`. The local
    part is technically case-sensitive in the RFC; no mail provider anyone uses
    treats it that way, and honouring it here would strand invitations.
    """
    return email.strip().lower()


def pick_accent(email: str) -> str:
    """Stable per-address colour: the same person is the same colour on every
    device, with no column to keep in sync at signup time."""
    return ACCENT_COLORS[sum(email.encode()) % len(ACCENT_COLORS)]


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == normalise_email(email)))
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def require_by_id(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await get_by_id(db, user_id)
    if user is None or user.is_deleted:
        raise NotFoundError(ErrorMessage.USER_NOT_FOUND_OR_INACTIVE)
    return user


async def create_user(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str = "",
    role: GlobalRole = GlobalRole.MEMBER,
    is_active: bool = True,
    is_superuser: bool = False,
    must_change_password: bool = False,
    email_verified: bool = False,
) -> User:
    """Create an account. Does NOT commit — the caller decides the transaction.

    Left uncommitted because every caller has more to do in the same
    transaction: registration claims pending project invitations, admin
    creation writes an audit row. Committing here would make those partial.
    """
    address = normalise_email(email)
    if await get_by_email(db, address) is not None:
        raise ConflictError(ErrorMessage.EMAIL_ALREADY_EXISTS)
    assert_password_strong(password)

    user = User(
        email=address,
        full_name=full_name.strip(),
        role=role,
        is_active=is_active,
        is_superuser=is_superuser,
        accent_color=pick_accent(address),
        email_verified_at=now() if email_verified else None,
    )
    user.credentials = UserCredentials(
        hashed_password=hash_password(password),
        must_change_password=must_change_password,
        password_changed_at=now(),
    )
    db.add(user)
    await db.flush()
    return user


# Profile fields that may legitimately be set back to nothing. Everything else
# on `UserUpdate` maps to a NOT NULL column, so an explicit null there is a bad
# request rather than an instruction.
NULLABLE_PROFILE_FIELDS = frozenset({"avatar_url"})


async def update_profile(db: AsyncSession, user: User, data: UserUpdate) -> User:
    # `exclude_unset` distinguishes "not sent" from "sent as null"; `exclude_none`
    # would collapse them, which made removing an avatar impossible — the request
    # answered `success: true` and changed nothing, and no other route can clear
    # the column.
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is None and field not in NULLABLE_PROFILE_FIELDS:
            continue
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    return user


async def touch_last_seen(db: AsyncSession, user: User) -> None:
    """Cheap liveness marker for the admin list. Not committed by itself."""
    user.last_seen_at = now()


async def project_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Projects this person can actually open.

    Joined to `projects` and filtered on `is_deleted`, because a membership row
    survives a soft delete. Without the join `/users/me` reports "1 project"
    over a list the projects endpoint returns as empty — and the frontend
    bootstraps from `/users/me` on every mount.
    """
    result = await db.execute(
        select(func.count(ProjectMember.id))
        .join(Project, Project.id == ProjectMember.project_id)
        .where(
            ProjectMember.user_id == user_id,
            ProjectMember.status == MemberStatus.ACTIVE,
            Project.is_deleted.is_(False),
        )
    )
    return int(result.scalar_one())


def to_read(user: User) -> UserRead:
    return UserRead.model_validate(user)


def to_me(user: User, *, must_change_password: bool, project_count: int = 0) -> UserMe:
    return UserMe(
        **UserRead.model_validate(user).model_dump(),
        timezone=user.timezone,
        theme=user.theme,
        permissions=sorted(permissions_for(user.role, user.is_superuser)),
        must_change_password=must_change_password,
        project_count=project_count,
    )
