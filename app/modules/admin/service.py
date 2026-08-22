import secrets
import string
import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import UnaryExpression

from app.core.constants import GlobalRole, MemberStatus
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.mailer import send_welcome
from app.core.messages import ErrorMessage
from app.core.pagination import Page, PageParams, paginate
from app.core.security import hash_password
from app.core.time import now
from app.modules.admin.schemas import AdminProjectRow, PasswordIssued, PlatformStats
from app.modules.audit import service as audit_service
from app.modules.auth import service as auth_service
from app.modules.projects.models import Project, ProjectMember
from app.modules.users import service as user_service
from app.modules.users.models import User, UserCredentials
from app.modules.users.schemas import AdminUserCreate, AdminUserUpdate, UserListItem

# Ambiguous glyphs are left out: these passwords get read aloud and typed from
# a screenshot, and an l/1 mix-up turns into a support ticket.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def generate_password(length: int = 14) -> str:
    """A password with at least one digit, so it passes the strength check."""
    body = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length - 1))
    return body + secrets.choice(string.digits)


# ── users ────────────────────────────────────────────────────────────────────


def _user_query(
    search: str | None, role: GlobalRole | None, is_active: bool | None
) -> Select[tuple[User]]:
    statement = select(User).where(User.is_deleted.is_(False))
    if search:
        pattern = f"%{search.strip().lower()}%"
        statement = statement.where(
            or_(func.lower(User.email).like(pattern), func.lower(User.full_name).like(pattern))
        )
    if role is not None:
        statement = statement.where(User.role == role)
    if is_active is not None:
        statement = statement.where(User.is_active.is_(is_active))
    return statement


async def list_users(
    db: AsyncSession,
    params: PageParams,
    *,
    search: str | None = None,
    role: GlobalRole | None = None,
    is_active: bool | None = None,
    sort: str = "recent",
) -> Page[UserListItem]:
    orders: dict[str, UnaryExpression[Any]] = {
        "recent": User.created_at.desc(),
        "name": User.full_name.asc(),
        "email": User.email.asc(),
        "last_seen": User.last_seen_at.desc().nullslast(),
    }
    statement = _user_query(search, role, is_active).order_by(
        orders.get(sort, orders["recent"]), User.id
    )
    users, total = await paginate(db, statement, params)

    counts = await _project_counts(db, [user.id for user in users])
    items = [
        UserListItem(
            **{
                **{
                    field: getattr(user, field)
                    for field in (
                        "id",
                        "email",
                        "full_name",
                        "avatar_url",
                        "accent_color",
                        "role",
                        "is_active",
                        "is_superuser",
                        "email_verified_at",
                        "last_login_at",
                        "created_at",
                        "last_seen_at",
                    )
                },
                **counts.get(user.id, {"project_count": 0, "owned_project_count": 0}),
            }
        )
        for user in users
    ]
    return Page.build(items, total, params)


async def _project_counts(
    db: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    """Membership and ownership counts for a page of users, in two queries.

    Two aggregates rather than a correlated subquery per row: an admin list of
    50 users would otherwise fire 100 counts.
    """
    if not user_ids:
        return {}
    membership = await db.execute(
        select(ProjectMember.user_id, func.count(ProjectMember.id))
        .join(Project, Project.id == ProjectMember.project_id)
        .where(
            ProjectMember.user_id.in_(user_ids),
            ProjectMember.status == MemberStatus.ACTIVE,
            # Same filter as the `owned` aggregate three lines below. Without it
            # the two columns of the same row disagree about the same project.
            Project.is_deleted.is_(False),
        )
        .group_by(ProjectMember.user_id)
    )
    owned = await db.execute(
        select(Project.owner_id, func.count(Project.id))
        .where(Project.owner_id.in_(user_ids), Project.is_deleted.is_(False))
        .group_by(Project.owner_id)
    )
    counts: dict[uuid.UUID, dict[str, int]] = {
        user_id: {"project_count": 0, "owned_project_count": 0} for user_id in user_ids
    }
    for user_id, count in membership.all():
        if user_id in counts:
            counts[user_id]["project_count"] = int(count)
    for user_id, count in owned.all():
        if user_id in counts:
            counts[user_id]["owned_project_count"] = int(count)
    return counts


async def create_user(db: AsyncSession, actor: User, data: AdminUserCreate) -> PasswordIssued:
    """Create an account on someone's behalf.

    With no password given, one is generated and the account is flagged
    `must_change_password` — so the credential the operator saw (or emailed)
    stops working the moment its owner chooses their own, and cannot be used to
    browse the app before that.
    """
    _assert_may_set_role(actor, data.role)
    issued = data.password is None
    password = data.password or generate_password()

    user = await user_service.create_user(
        db,
        email=data.email,
        password=password,
        full_name=data.full_name,
        role=data.role,
        is_active=data.is_active,
        must_change_password=issued,
        # An operator vouching for the address is as good as a confirmation
        # click; making them chase a link they cannot receive helps nobody.
        email_verified=True,
    )
    # Same step self-registration takes. Without it the membership rows written
    # for this address keep `user_id = NULL` forever — access still works,
    # because it is matched on the email, but every eviction path is keyed on
    # the user id and silently does nothing. Removing this person from a project
    # would leave their editor open on it.
    from app.modules.projects import service as project_service

    await project_service.claim_invites(db, user)
    await audit_service.record(
        db,
        "admin.user_created",
        entity_type="user",
        entity_id=user.id,
        summary=f"{actor.email} created {user.email} as {data.role}",
        meta={"role": data.role.value, "password_generated": issued},
    )
    await db.commit()
    await db.refresh(user)

    emailed = False
    if data.send_invite_email:
        await send_welcome(user.email, user.display_name, password if issued else None)
        emailed = True

    return PasswordIssued(
        user_id=user.id,
        email=user.email,
        # Only ever returned for a password the platform generated. One the
        # operator typed is theirs already and echoing it back is pure exposure.
        temporary_password=password if issued else None,
        emailed=emailed,
    )


async def update_user(
    db: AsyncSession, actor: User, user_id: uuid.UUID, data: AdminUserUpdate
) -> PasswordIssued:
    user = await _load(db, user_id)
    _assert_may_manage(actor, user)
    if data.role is not None and data.role != user.role:
        _assert_may_set_role(actor, data.role)
        if user.id == actor.id:
            raise ConflictError("You cannot change your own role")
        if user.role == GlobalRole.SUPERADMIN:
            await _assert_another_superadmin_remains(db, user.id)
        old = user.role
        user.role = data.role
        await audit_service.record(
            db,
            "admin.role_changed",
            entity_type="user",
            entity_id=user.id,
            summary=f"{actor.email} changed {user.email} from {old} to {data.role}",
        )

    if data.full_name is not None:
        user.full_name = data.full_name.strip()

    if data.is_active is not None and data.is_active != user.is_active:
        if user.id == actor.id:
            raise ConflictError(ErrorMessage.CANNOT_DEACTIVATE_SELF)
        if not data.is_active and user.role == GlobalRole.SUPERADMIN:
            await _assert_another_superadmin_remains(db, user.id)
        user.is_active = data.is_active
        await audit_service.record(
            db,
            "admin.user_activated" if data.is_active else "admin.user_deactivated",
            entity_type="user",
            entity_id=user.id,
            summary=(
                f"{actor.email} {'activated' if data.is_active else 'deactivated'} {user.email}"
            ),
        )

    password: str | None = None
    if data.reset_password:
        password = generate_password()
        credentials = await db.get(UserCredentials, user.id)
        if credentials is None:
            credentials = UserCredentials(user_id=user.id, hashed_password="")
            db.add(credentials)
        credentials.hashed_password = hash_password(password)
        credentials.must_change_password = True
        credentials.password_changed_at = now()
        await audit_service.record(
            db,
            "admin.password_reset",
            entity_type="user",
            entity_id=user.id,
            summary=f"{actor.email} issued a new password for {user.email}",
        )

    await db.commit()
    await db.refresh(user)

    # Deactivating or re-issuing a password has to end the sessions that are
    # already open, or the change means nothing until the token expires. That
    # includes websockets: they authenticate once at the handshake, so a
    # revoked session keeps reading the document until the tab is closed.
    if data.is_active is False or data.reset_password:
        await auth_service.logout_everywhere(db, user)
        await _close_all_sockets(db, user)
    if password:
        await send_welcome(user.email, user.display_name, password)

    return PasswordIssued(
        user_id=user.id, email=user.email, temporary_password=password, emailed=bool(password)
    )


async def delete_user(db: AsyncSession, actor: User, user_id: uuid.UUID) -> None:
    """Soft delete. The account stops working; its authorship of past work
    stays readable, which a hard delete would erase."""
    user = await _load(db, user_id)
    _assert_may_manage(actor, user)  # belt and braces: the route is already superadmin-only
    if user.id == actor.id:
        raise ConflictError(ErrorMessage.CANNOT_DEACTIVATE_SELF)
    if user.role == GlobalRole.SUPERADMIN or user.is_superuser:
        await _assert_another_superadmin_remains(db, user.id)

    user.is_deleted = True
    user.deleted_at = now()
    user.is_active = False
    await audit_service.record(
        db,
        "admin.user_deleted",
        entity_type="user",
        entity_id=user.id,
        summary=f"{actor.email} deleted {user.email}",
    )
    await db.commit()
    await auth_service.logout_everywhere(db, user)
    await _close_all_sockets(db, user)


async def _close_all_sockets(db: AsyncSession, user: User) -> None:
    """Close every collaboration socket this account holds, on every project.

    `logout_everywhere` ends the HTTP session; a websocket was authenticated at
    its handshake and knows nothing about that until it next tries to write.
    """
    from app.modules.collab.hub import hub

    projects = (
        (await db.execute(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)))
        .scalars()
        .all()
    )
    for project_id in projects:
        await hub.disconnect_user(project_id, user.id)


def _assert_may_manage(actor: User, target: User) -> None:
    """Refuse an ADMIN aiming a privileged action at another admin account.

    `_assert_may_set_role` guards the role being *granted*; nothing guarded the
    role being *targeted*, and re-issuing a password hands over a live
    credential. So an ADMIN could reset a SUPERADMIN's password, read the
    plaintext out of the response, sign in as them, clear the
    must-change-password flag through the route that exists for exactly that,
    and hold the whole platform — while the audit log recorded it as routine
    user management. That made the ADMIN/SUPERADMIN split decorative.

    Managing your own account is still allowed; the individual branches have
    their own self-targeting rules.
    """
    if target.id == actor.id:
        return
    privileged = target.is_superuser or target.role in (
        GlobalRole.SUPERADMIN,
        GlobalRole.ADMIN,
    )
    if privileged and not (actor.is_superuser or actor.role == GlobalRole.SUPERADMIN):
        raise AuthorizationError("Only a superadmin can manage another admin account")


def _assert_may_set_role(actor: User, role: GlobalRole) -> None:
    """Only a superadmin may hand out admin roles.

    Without this an ADMIN could promote themselves to SUPERADMIN through the
    very screen they were given, which makes the distinction between the two
    roles decorative.
    """
    if role in (GlobalRole.ADMIN, GlobalRole.SUPERADMIN) and not (
        actor.is_superuser or actor.role == GlobalRole.SUPERADMIN
    ):
        raise ValidationError("Only a superadmin can grant admin roles")


async def _assert_another_superadmin_remains(db: AsyncSession, excluding: uuid.UUID) -> None:
    result = await db.execute(
        select(func.count(User.id)).where(
            User.id != excluding,
            User.is_deleted.is_(False),
            User.is_active.is_(True),
            or_(User.role == GlobalRole.SUPERADMIN, User.is_superuser.is_(True)),
        )
    )
    if int(result.scalar_one()) == 0:
        raise ConflictError(ErrorMessage.CANNOT_DEMOTE_LAST_SUPERADMIN)


async def _load(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None or user.is_deleted:
        raise NotFoundError(ErrorMessage.USER_NOT_FOUND_OR_INACTIVE)
    return user


# ── projects (metadata only) ─────────────────────────────────────────────────


async def list_projects(
    db: AsyncSession,
    params: PageParams,
    *,
    search: str | None = None,
    include_deleted: bool = False,
) -> Page[AdminProjectRow]:
    statement = select(Project)
    if not include_deleted:
        statement = statement.where(Project.is_deleted.is_(False))
    if search:
        statement = statement.where(func.lower(Project.name).like(f"%{search.strip().lower()}%"))
    statement = statement.order_by(Project.last_activity_at.desc().nullslast(), Project.id)

    projects, total = await paginate(db, statement, params)
    owner_ids = {p.owner_id for p in projects if p.owner_id}
    owners = (
        {
            u.id: u.email
            for u in (await db.execute(select(User).where(User.id.in_(owner_ids)))).scalars()
        }
        if owner_ids
        else {}
    )
    member_counts = await _member_counts(db, [p.id for p in projects])

    return Page.build(
        [
            AdminProjectRow(
                id=project.id,
                name=project.name,
                owner_email=owners.get(project.owner_id) if project.owner_id else None,
                member_count=member_counts.get(project.id, 0),
                screen_count=project.screen_count,
                module_count=project.module_count,
                doc_version=project.doc_version,
                is_archived=project.is_archived,
                is_deleted=project.is_deleted,
                created_at=project.created_at,
                updated_at=project.updated_at,
                last_activity_at=project.last_activity_at,
            )
            for project in projects
        ],
        total,
        params,
    )


async def _member_counts(db: AsyncSession, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not project_ids:
        return {}
    result = await db.execute(
        select(ProjectMember.project_id, func.count(ProjectMember.id))
        .where(
            ProjectMember.project_id.in_(project_ids),
            ProjectMember.status != MemberStatus.REVOKED,
        )
        .group_by(ProjectMember.project_id)
    )
    return {project_id: int(count) for project_id, count in result.all()}


async def delete_project(db: AsyncSession, actor: User, project_id: uuid.UUID) -> None:
    """The one platform power that reaches inside a project.

    Destructive rather than revealing: it removes a project without ever showing
    its contents, which keeps the "only members can see it" rule intact while
    still letting an operator deal with something that must not exist.
    """
    project = await db.get(Project, project_id)
    if project is None or project.is_deleted:
        raise NotFoundError(ErrorMessage.PROJECT_NOT_FOUND)
    # Captured before the commit, and used after it: the project route already
    # does this, and without it an admin deletion leaves every editor's room
    # open — presence, cursors and the document all still running on something
    # that no longer exists.
    open_rooms = (
        (
            await db.execute(
                select(ProjectMember.user_id).where(ProjectMember.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    project.is_deleted = True
    project.deleted_at = now()
    await audit_service.record(
        db,
        "admin.project_deleted",
        entity_type="project",
        entity_id=project.id,
        summary=f"{actor.email} deleted project {project.name}",
    )
    await db.commit()

    from app.modules.collab.hub import hub

    for member_id in open_rooms:
        if member_id is not None:
            await hub.disconnect_user(project.id, member_id)


# ── dashboard ────────────────────────────────────────────────────────────────


async def stats(db: AsyncSession) -> PlatformStats:
    from datetime import timedelta

    day_ago = now() - timedelta(hours=24)
    week_ago = now() - timedelta(days=7)

    async def count(statement: Select[tuple[int]]) -> int:
        return int((await db.execute(statement)).scalar_one())

    return PlatformStats(
        users_total=await count(select(func.count(User.id)).where(User.is_deleted.is_(False))),
        users_active=await count(
            select(func.count(User.id)).where(User.is_deleted.is_(False), User.is_active.is_(True))
        ),
        users_pending_invite=await count(
            select(func.count(func.distinct(ProjectMember.email))).where(
                ProjectMember.user_id.is_(None), ProjectMember.status == MemberStatus.INVITED
            )
        ),
        admins=await count(
            select(func.count(User.id)).where(
                User.is_deleted.is_(False),
                or_(
                    User.role.in_([GlobalRole.ADMIN, GlobalRole.SUPERADMIN]),
                    User.is_superuser.is_(True),
                ),
            )
        ),
        projects_total=await count(
            select(func.count(Project.id)).where(Project.is_deleted.is_(False))
        ),
        projects_archived=await count(
            select(func.count(Project.id)).where(
                Project.is_deleted.is_(False), Project.is_archived.is_(True)
            )
        ),
        projects_deleted=await count(
            select(func.count(Project.id)).where(Project.is_deleted.is_(True))
        ),
        screens_total=await count(
            select(func.coalesce(func.sum(Project.screen_count), 0)).where(
                Project.is_deleted.is_(False)
            )
        ),
        signups_last_7_days=await count(
            select(func.count(User.id)).where(
                User.is_deleted.is_(False), User.created_at >= week_ago
            )
        ),
        active_last_24_hours=await count(
            select(func.count(User.id)).where(
                # Every other counter here excludes deleted accounts; this one
                # did not, so the dashboard overstated activity by whoever had
                # been removed since they last signed in.
                User.is_deleted.is_(False),
                User.last_seen_at >= day_ago,
            )
        ),
    )
