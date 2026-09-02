import logging
import secrets
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import UnaryExpression

from app.core.config import settings
from app.core.constants import ActivityType, MemberStatus, ProjectRole
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.mailer import send_project_invite
from app.core.messages import ErrorMessage
from app.core.pagination import PageParams, PageResult, paginated
from app.core.security import create_invite_token
from app.core.time import now
from app.modules.audit import service as audit_service
from app.modules.projects.access import ProjectAccess
from app.modules.projects.models import (
    Project,
    ProjectActivity,
    ProjectComment,
    ProjectMember,
    ProjectVersion,
)
from app.modules.projects.schemas import (
    CommentCreate,
    CommentUpdate,
    MemberInvite,
    ProjectCreate,
    ProjectDocSave,
    ProjectUpdate,
)
from app.modules.users import service as user_service
from app.modules.users.models import User

logger = logging.getLogger("app.projects")


# ── activity ─────────────────────────────────────────────────────────────────


def add_activity(
    db: AsyncSession,
    project: Project,
    actor: User | None,
    kind: ActivityType,
    summary: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """Stage a feed entry and mark the project as touched. Caller commits."""
    db.add(
        ProjectActivity(
            project_id=project.id,
            actor_id=actor.id if actor else None,
            actor_email=actor.email if actor else "",
            type=kind.value,
            summary=summary,
            meta=meta,
        )
    )
    project.last_activity_at = now()


# ── documents ────────────────────────────────────────────────────────────────


def count_doc(doc: dict[str, Any]) -> tuple[int, int]:
    """(screens, modules) in a document. Tolerant of a document missing either.

    Kept denormalised on the row so the projects list renders "12 screens"
    without loading twelve documents to count them.
    """
    screens = doc.get("screens")
    modules = doc.get("modules")
    return (
        len(screens) if isinstance(screens, list) else 0,
        len(modules) if isinstance(modules, list) else 0,
    )


def sync_counts(project: Project) -> None:
    project.screen_count, project.module_count = count_doc(project.doc or {})


# ── create / read ────────────────────────────────────────────────────────────


async def create_project(db: AsyncSession, user: User, data: ProjectCreate) -> Project:
    project = Project(
        name=data.name.strip(),
        description=data.description.strip(),
        doc=data.doc,
        schema_version=data.schema_version,
        doc_version=1,
        owner_id=user.id,
        created_by=user.id,
        updated_by=user.id,
        last_activity_at=now(),
    )
    sync_counts(project)
    db.add(project)
    await db.flush()

    db.add(
        ProjectMember(
            project_id=project.id,
            email=user.email,
            user_id=user.id,
            role=ProjectRole.OWNER,
            status=MemberStatus.ACTIVE,
            invited_by=user.id,
            invited_at=now(),
            joined_at=now(),
        )
    )
    add_activity(db, project, user, ActivityType.PROJECT_CREATED, f"created “{project.name}”")
    await audit_service.record(
        db,
        "project.create",
        entity_type="project",
        entity_id=project.id,
        summary=f"{user.email} created project {project.name}",
    )
    await db.commit()

    # Invitations are sent after the commit so a mail failure cannot roll back a
    # project that was successfully created. For the same reason a bad address
    # cannot be allowed to raise: the project, its owner row and its activity
    # are already durable, so answering 409 would tell a client "nothing
    # happened" — and the retry would create a second project.
    seen = {user.email}
    for email in data.member_emails:
        address = user_service.normalise_email(email)
        if address in seen:
            continue
        seen.add(address)
        try:
            await invite_member(db, project, user, MemberInvite(email=address))
        except ConflictError:
            continue

    await db.refresh(project, attribute_names=["members"])
    return project


def _visible_projects_query(user: User) -> Select[tuple[Project]]:
    """Projects this person may see: the ones their address is on. No exceptions.

    Joined on user id OR email so a project shared with an address moments ago
    is visible before the invitation has been claimed.
    """
    return (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            Project.is_deleted.is_(False),
            ProjectMember.status != MemberStatus.REVOKED,
            _membership_match(user),
        )
        .options(selectinload(Project.members))
        .distinct()
    )


async def list_projects(
    db: AsyncSession,
    user: User,
    params: PageParams,
    *,
    search: str | None = None,
    archived: bool | None = False,
    owned_only: bool = False,
    sort: str = "recent",
) -> PageResult[Project]:
    statement = _visible_projects_query(user)
    if archived is not None:
        statement = statement.where(Project.is_archived.is_(archived))
    if owned_only:
        statement = statement.where(Project.owner_id == user.id)
    if search:
        pattern = f"%{search.strip().lower()}%"
        statement = statement.where(
            or_(
                func.lower(Project.name).like(pattern),
                func.lower(Project.description).like(pattern),
            )
        )

    orders: dict[str, UnaryExpression[Any]] = {
        "recent": Project.last_activity_at.desc().nullslast(),
        "updated": Project.updated_at.desc(),
        "created": Project.created_at.desc(),
        "name": Project.name.asc(),
        "screens": Project.screen_count.desc(),
    }
    statement = statement.order_by(orders.get(sort, orders["recent"]), Project.id)
    return await paginated(db, statement, params)


async def role_map(
    db: AsyncSession, user: User, project_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ProjectRole]:
    """This person's role on each of `project_ids`, in one query.

    Exists so serialising a page of 25 projects does not run 25 membership
    lookups — the N+1 that a naive `my_role` property would create.
    """
    if not project_ids:
        return {}
    result = await db.execute(
        select(ProjectMember.project_id, ProjectMember.role).where(
            ProjectMember.project_id.in_(project_ids),
            ProjectMember.status != MemberStatus.REVOKED,
            _membership_match(user),
        )
    )
    return {row[0]: ProjectRole(row[1]) for row in result.all()}


async def _lock(db: AsyncSession, project: Project) -> Project:
    """Take a row lock for the rest of this transaction.

    The optimistic-concurrency check is a read, a comparison and a write. Left
    unlocked they are three separate steps, so two saves that both read version
    N both write N+1: both return 200, both report the same version, and one
    document is silently gone — with no 409 and no snapshot holding the lost
    content, because both snapshots capture the same pre-race document.

    `populate_existing` is what makes this work: without it the identity map
    hands back the instance already in the session and the lock buys nothing.
    """
    await db.execute(
        select(Project)
        .where(Project.id == project.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return project


# ── update ───────────────────────────────────────────────────────────────────


async def update_project(
    db: AsyncSession, access: ProjectAccess, user: User, data: ProjectUpdate
) -> Project:
    project = access.project
    changes = data.model_dump(exclude_unset=True, exclude_none=True)

    # Archiving is an owner's decision, not an editor's.
    #
    # `project_by_public_token` refuses an archived project, so an EDITOR
    # setting this took down every public link the owner had handed out — an
    # owner-only capability, revoked by a non-owner, with nothing recorded
    # anywhere to say who did it. Editing the document is what EDITOR means;
    # taking the project off the air is not.
    if "is_archived" in changes and access.role != ProjectRole.OWNER:
        raise AuthorizationError(ErrorMessage.NOT_PROJECT_OWNER)

    renamed = "name" in changes and changes["name"].strip() != project.name

    for field, value in changes.items():
        setattr(project, field, value.strip() if isinstance(value, str) else value)
    project.updated_by = user.id

    if renamed:
        add_activity(
            db, project, user, ActivityType.PROJECT_RENAMED, f"renamed it to “{project.name}”"
        )
    await db.commit()
    await db.refresh(project)
    return project


async def save_doc(
    db: AsyncSession,
    access: ProjectAccess,
    user: User,
    data: ProjectDocSave,
    *,
    snapshot: bool = True,
    activity: bool = True,
) -> Project:
    """Persist a new document, refusing to overwrite someone else's newer one.

    `base_version` is the version the client started from. If the stored
    document has moved past it, the save is a 409 carrying the current version
    and who last touched it, so the client can offer a reload instead of
    silently discarding whatever the other person did. Omitting `base_version`
    is an explicit "write it anyway" — used by the collaboration socket, which
    has already reconciled.
    """
    project = await _lock(db, access.project)
    if data.base_version is not None and data.base_version != project.doc_version:
        raise ConflictError(
            ErrorMessage.STALE_DOCUMENT,
            errors=[
                {
                    "field": "base_version",
                    "message": ErrorMessage.STALE_DOCUMENT,
                    "current_version": project.doc_version,
                    "sent_version": data.base_version,
                }
            ],
        )

    # A snapshot of what is about to be replaced, so an accidental overwrite is
    # recoverable. Auto-snapshots are pruned; named ones are not.
    #
    # `snapshot=False` is for the collaboration socket, which saves as fast as
    # someone drags a node: one snapshot per keystroke would bury the useful
    # history under a thousand identical rows. It takes them on a timer instead.
    if snapshot or data.label:
        await _snapshot(db, project, user, label=data.label or "", is_auto=data.label is None)

    project.doc = data.doc
    project.doc_version += 1
    if data.schema_version is not None:
        project.schema_version = data.schema_version
    project.updated_by = user.id
    sync_counts(project)
    project.last_activity_at = now()
    if activity:
        add_activity(
            db,
            project,
            user,
            ActivityType.PROJECT_UPDATED,
            "saved changes",
            {"doc_version": project.doc_version},
        )
    await db.commit()
    await db.refresh(project)
    return project


async def delete_project(db: AsyncSession, project: Project, user: User) -> None:
    """Soft delete. A shared project must be recoverable — one member pressing
    delete cannot be allowed to vaporise work the other four can see."""
    project.is_deleted = True
    project.deleted_at = now()
    project.updated_by = user.id
    members = list(project.members)
    await audit_service.record(
        db,
        "project.delete",
        entity_type="project",
        entity_id=project.id,
        summary=f"{user.email} deleted project {project.name}",
    )
    await db.commit()
    # A trashed project is unreachable over HTTP; an editor left holding it open
    # would otherwise keep saving into it.
    for member in members:
        await _evict_open_sockets(project.id, member.user_id)


async def restore_project(db: AsyncSession, project_id: uuid.UUID, user: User) -> Project:
    project = await db.get(Project, project_id)
    if project is None or not project.is_deleted:
        raise NotFoundError(ErrorMessage.PROJECT_NOT_FOUND)
    project.is_deleted = False
    project.deleted_at = None
    project.updated_by = user.id
    add_activity(db, project, user, ActivityType.PROJECT_RESTORED, "restored the project")
    await db.commit()
    await db.refresh(project)
    return project


# ── membership ───────────────────────────────────────────────────────────────


def _membership_match(user: User) -> Any:
    """Which membership rows belong to this account.

    The email half only applies once the address is confirmed — see the note in
    `access.find_membership`. Kept here as one expression so the project list,
    the role lookup and the access check cannot answer the question three
    different ways, which is how a project appears in somebody's list and then
    404s when they open it.
    """
    if user.email_verified_at is None:
        return ProjectMember.user_id == user.id
    return or_(ProjectMember.user_id == user.id, ProjectMember.email == user.email)


async def invite_member(
    db: AsyncSession, project: Project, actor: User, data: MemberInvite
) -> ProjectMember:
    """Add one address to a project.

    The address need not have an account. That is the whole design: sharing is
    by email, the row is written INVITED with a null `user_id`, and registering
    with that address claims it.
    """
    email = user_service.normalise_email(data.email)
    existing = await member_by_email(db, project.id, email)
    if existing is not None and existing.status != MemberStatus.REVOKED:
        raise ConflictError(ErrorMessage.MEMBER_ALREADY_ADDED)

    account = await user_service.get_by_email(db, email)
    # An account only counts once it has proved the address.
    #
    # Anyone can register any address — there is no confirmation step before an
    # account is usable — so binding the membership to whatever account happens
    # to hold the string meant registering `finance@theirclient.com` in advance
    # was enough to be handed EDITOR on a project shared with it later. The row
    # stays INVITED until the address is confirmed, and confirming it claims
    # the invitation.
    claimed = account if account is not None and account.email_verified_at is not None else None
    member = existing or ProjectMember(project_id=project.id, email=email)
    member.role = data.role
    member.user_id = claimed.id if claimed else None
    member.status = MemberStatus.ACTIVE if claimed else MemberStatus.INVITED
    member.invited_by = actor.id
    member.invited_at = now()
    member.joined_at = now() if claimed else None
    db.add(member)

    add_activity(
        db, project, actor, ActivityType.MEMBER_INVITED, f"added {email} as {data.role.lower()}"
    )
    await audit_service.record(
        db,
        "project.member_invited",
        entity_type="project",
        entity_id=project.id,
        summary=f"{actor.email} added {email} to {project.name}",
        meta={"role": data.role.value},
    )
    await db.commit()
    await db.refresh(member)

    await send_project_invite(
        email,
        project.name,
        actor.display_name,
        create_invite_token(str(project.id), email),
        is_new_user=account is None,
    )
    return member


async def member_by_email(
    db: AsyncSession, project_id: uuid.UUID, email: str
) -> ProjectMember | None:
    """The membership row for one address on one project, revoked ones included."""
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.email == email
        )
    )
    return result.scalar_one_or_none()


async def list_members(
    db: AsyncSession, project_id: uuid.UUID
) -> list[tuple[ProjectMember, User | None]]:
    """Members plus the account behind each, in one query.

    An outer join, because a member with no account yet is the normal case for
    a pending invitation — an inner join would silently hide exactly the rows
    the invite screen exists to show.
    """
    result = await db.execute(
        select(ProjectMember, User)
        .outerjoin(User, User.id == ProjectMember.user_id)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.status != MemberStatus.REVOKED,
        )
        .order_by(ProjectMember.role, ProjectMember.email)
    )
    return [(member, account) for member, account in result.all()]


async def _evict_open_sockets(project_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    """Close this person's live editor sockets on this project.

    Membership is checked once, when the socket opens. Every route that takes
    access away has to say so, or the connection keeps receiving the document
    it is no longer entitled to. Imported here rather than at module scope to
    keep the module graph acyclic — the hub imports nothing from this package.
    """
    if user_id is None:
        return
    from app.modules.collab.hub import hub

    await hub.disconnect_user(project_id, user_id)


async def update_member_role(
    db: AsyncSession, project: Project, actor: User, member_id: uuid.UUID, role: ProjectRole
) -> ProjectMember:
    member = await db.get(ProjectMember, member_id)
    if member is None or member.project_id != project.id:
        raise NotFoundError(ErrorMessage.MEMBER_NOT_FOUND)
    if member.user_id == actor.id:
        # Not paranoia: the check below counts owners, and an owner demoting
        # themselves while another owner exists would still leave the project
        # with nobody who invited them able to undo it by accident.
        raise ConflictError(ErrorMessage.CANNOT_CHANGE_OWN_ROLE)
    if member.role == ProjectRole.OWNER and role != ProjectRole.OWNER:
        await _assert_another_owner_remains(db, project.id, member.id)

    was = ProjectRole(member.role)
    member.role = role
    add_activity(
        db,
        project,
        actor,
        ActivityType.MEMBER_ROLE_CHANGED,
        f"made {member.email} a {role.lower()}",
    )
    await db.commit()
    await db.refresh(member)
    # Only on the way down. A socket carries the role it opened with, so a
    # demotion has to close it; a promotion is picked up on the next save
    # because the write path re-resolves access, and closing the editor of
    # someone who was just *given* more rights would be perverse.
    if not role.at_least(was):
        await _evict_open_sockets(project.id, member.user_id)
    return member


async def remove_member(
    db: AsyncSession, project: Project, actor: User, member_id: uuid.UUID
) -> None:
    member = await db.get(ProjectMember, member_id)
    if member is None or member.project_id != project.id:
        raise NotFoundError(ErrorMessage.MEMBER_NOT_FOUND)
    if member.role == ProjectRole.OWNER:
        await _assert_another_owner_remains(db, project.id, member.id)

    email = member.email
    evicted = member.user_id
    await db.delete(member)
    add_activity(db, project, actor, ActivityType.MEMBER_REMOVED, f"removed {email}")
    await audit_service.record(
        db,
        "project.member_removed",
        entity_type="project",
        entity_id=project.id,
        summary=f"{actor.email} removed {email} from {project.name}",
    )
    await db.commit()
    await _evict_open_sockets(project.id, evicted)


async def leave_project(db: AsyncSession, access: ProjectAccess, user: User) -> None:
    if access.role == ProjectRole.OWNER:
        await _assert_another_owner_remains(db, access.project.id, access.member.id)
    await db.delete(access.member)
    add_activity(db, access.project, user, ActivityType.MEMBER_REMOVED, "left the project")
    await db.commit()
    await _evict_open_sockets(access.project.id, user.id)


async def _assert_another_owner_remains(
    db: AsyncSession, project_id: uuid.UUID, excluding: uuid.UUID
) -> None:
    """A project with no owner is unadministrable — nobody can invite, rename or
    delete it, and no API call can fix it. Refuse the step that would do it."""
    result = await db.execute(
        select(func.count(ProjectMember.id)).where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == ProjectRole.OWNER,
            ProjectMember.status != MemberStatus.REVOKED,
            # An invited address that never registered cannot administer
            # anything. Counting it meant you could hand OWNER to a typo, leave,
            # and strand the project where no account at all can reach it.
            ProjectMember.user_id.is_not(None),
            ProjectMember.id != excluding,
        )
    )
    if int(result.scalar_one()) == 0:
        raise ConflictError(ErrorMessage.CANNOT_REMOVE_LAST_OWNER)


async def claim_invites(db: AsyncSession, user: User) -> int:
    """Attach every pending invitation for this address to the new account.

    Run at registration. Without it, someone invited before they signed up would
    create an account and find none of the projects they were told about — the
    rows exist, but nothing has ever linked them to a user id.
    """
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.email == user.email, ProjectMember.user_id.is_(None)
        )
    )
    pending = list(result.scalars())
    for member in pending:
        member.user_id = user.id
        member.status = MemberStatus.ACTIVE
        member.joined_at = now()
        project = await db.get(Project, member.project_id)
        if project is not None:
            add_activity(
                db, project, user, ActivityType.MEMBER_JOINED, f"{user.email} joined the project"
            )
    return len(pending)


async def mark_opened(db: AsyncSession, access: ProjectAccess) -> None:
    access.member.last_opened_at = now()
    await db.commit()


# ── versions ─────────────────────────────────────────────────────────────────


async def _snapshot(
    db: AsyncSession, project: Project, user: User, *, label: str, is_auto: bool
) -> ProjectVersion | None:
    """Store the document as it stands *before* the write about to happen.

    Skipped for a document that is still empty — snapshotting the blank state a
    project is born with just fills the history with nothing.
    """
    if not project.doc:
        return None
    version = ProjectVersion(
        project_id=project.id,
        label=label,
        doc=project.doc,
        doc_version=project.doc_version,
        is_auto=is_auto,
        # Whoever wrote the content being snapshotted, not whoever is
        # overwriting it. `user` here is the incoming writer, so stamping them
        # made the history say Bob authored Alice's document — the one column
        # anybody would read to answer "who changed this" named the wrong
        # person every time two people worked on a project.
        created_by=project.updated_by or user.id,
    )
    db.add(version)
    if is_auto:
        # Flushed first: `autoflush=False`, so without this the pending INSERT
        # is invisible to the prune's OFFSET and the cap keeps one row more
        # than it says it does.
        await db.flush()
        await _prune_auto_versions(db, project.id)
    return version


async def _prune_auto_versions(db: AsyncSession, project_id: uuid.UUID) -> None:
    """Keep the most recent automatic snapshots and drop the rest.

    Named snapshots are never pruned — someone chose to keep those. Automatic
    ones exist to make the last few saves undoable, and unbounded they would
    grow the table by a full document on every keystroke-driven autosave.
    """
    result = await db.execute(
        select(ProjectVersion.id)
        .where(ProjectVersion.project_id == project_id, ProjectVersion.is_auto.is_(True))
        .order_by(ProjectVersion.created_at.desc())
        .offset(settings.PROJECT_VERSION_LIMIT)
    )
    stale = [row[0] for row in result.all()]
    for version_id in stale:
        version = await db.get(ProjectVersion, version_id)
        if version is not None:
            await db.delete(version)


async def save_version(
    db: AsyncSession, access: ProjectAccess, user: User, label: str
) -> ProjectVersion:
    version = await _snapshot(db, access.project, user, label=label or "Manual save", is_auto=False)
    if version is None:
        raise ValidationError("There is nothing saved yet to snapshot")
    add_activity(
        db, access.project, user, ActivityType.VERSION_SAVED, f"saved a version — {version.label}"
    )
    await db.commit()
    await db.refresh(version)
    return version


async def list_versions(
    db: AsyncSession, project_id: uuid.UUID, params: PageParams
) -> PageResult[ProjectVersion]:
    statement = (
        select(ProjectVersion)
        .where(ProjectVersion.project_id == project_id)
        .order_by(ProjectVersion.created_at.desc())
    )
    return await paginated(db, statement, params)


async def get_version(
    db: AsyncSession, project_id: uuid.UUID, version_id: uuid.UUID
) -> ProjectVersion:
    version = await db.get(ProjectVersion, version_id)
    if version is None or version.project_id != project_id:
        raise NotFoundError(ErrorMessage.VERSION_NOT_FOUND)
    return version


async def restore_version(
    db: AsyncSession, access: ProjectAccess, user: User, version_id: uuid.UUID
) -> Project:
    version = await get_version(db, access.project.id, version_id)
    project = await _lock(db, access.project)
    # Snapshot the live document first, so restoring is itself undoable.
    await _snapshot(db, project, user, label="Before restore", is_auto=True)
    project.doc = version.doc
    project.doc_version += 1
    project.updated_by = user.id
    sync_counts(project)
    add_activity(
        db,
        project,
        user,
        ActivityType.VERSION_RESTORED,
        f"restored the version from {version.created_at:%d %b %Y %H:%M}",
        {"version_id": str(version.id)},
    )
    await db.commit()
    await db.refresh(project)
    return project


# ── activity feed ────────────────────────────────────────────────────────────


async def list_activity(
    db: AsyncSession, project_id: uuid.UUID, params: PageParams
) -> PageResult[ProjectActivity]:
    statement = (
        select(ProjectActivity)
        .where(ProjectActivity.project_id == project_id)
        .order_by(ProjectActivity.created_at.desc())
    )
    return await paginated(db, statement, params)


# ── comments ─────────────────────────────────────────────────────────────────


async def add_comment(
    db: AsyncSession, access: ProjectAccess, user: User, data: CommentCreate
) -> ProjectComment:
    comment = ProjectComment(
        project_id=access.project.id,
        author_id=user.id,
        author_email=user.email,
        target_kind=data.target_kind,
        target_key=data.target_key,
        body=data.body,
        created_by=user.id,
    )
    db.add(comment)
    add_activity(
        db,
        access.project,
        user,
        ActivityType.COMMENT_ADDED,
        f"commented on {data.target_key or 'the canvas'}",
    )
    await db.commit()
    await db.refresh(comment)
    return comment


async def list_comments(
    db: AsyncSession, project_id: uuid.UUID, *, include_resolved: bool = False
) -> list[ProjectComment]:
    statement = select(ProjectComment).where(
        ProjectComment.project_id == project_id, ProjectComment.is_deleted.is_(False)
    )
    if not include_resolved:
        statement = statement.where(ProjectComment.resolved_at.is_(None))
    result = await db.execute(statement.order_by(ProjectComment.created_at.asc()))
    return list(result.scalars())


async def update_comment(
    db: AsyncSession, access: ProjectAccess, user: User, comment_id: uuid.UUID, data: CommentUpdate
) -> ProjectComment:
    comment = await db.get(ProjectComment, comment_id)
    if comment is None or comment.project_id != access.project.id or comment.is_deleted:
        raise NotFoundError("Comment not found")
    # Anyone on the project may resolve a thread; only its author may reword it.
    if data.body is not None:
        if comment.author_id != user.id:
            raise ConflictError("Only the author can edit a comment")
        comment.body = data.body
    if data.resolved is not None:
        comment.resolved_at = now() if data.resolved else None
        comment.resolved_by = user.id if data.resolved else None
    comment.updated_by = user.id
    await db.commit()
    await db.refresh(comment)
    return comment


async def delete_comment(
    db: AsyncSession, access: ProjectAccess, user: User, comment_id: uuid.UUID
) -> None:
    comment = await db.get(ProjectComment, comment_id)
    if comment is None or comment.project_id != access.project.id or comment.is_deleted:
        raise NotFoundError("Comment not found")
    if comment.author_id != user.id and not access.is_owner:
        raise ConflictError("Only the author or a project owner can delete a comment")
    comment.is_deleted = True
    comment.deleted_at = now()
    comment.updated_by = user.id
    await db.commit()


# ── the public read-only link ────────────────────────────────────────────────
#
# A capability, not an identity. Holding the token lets anyone READ one
# document with no account; it never confers membership, never appears in the
# member list, and no write path accepts it. Everything in this section is
# owner-only except `project_by_public_token`, which is the anonymous read.


def _mint_public_token() -> str:
    """A token wide enough that guessing is not a strategy.

    32 bytes of `secrets` entropy, url-safe, so it survives being pasted into
    chat and back out again. `token_urlsafe` is variable-length near the end,
    which is fine — the column allows 64 and uniqueness is enforced there.
    """
    return secrets.token_urlsafe(32)


async def enable_public_link(db: AsyncSession, project: Project, user: User) -> Project:
    """Turn the link on, or hand back the one already in force.

    Deliberately idempotent. An owner who clicks "share" twice wants the link,
    not a new link — silently rotating here would break the URL they had
    already pasted into a message. Rotation is its own explicit action below.
    """
    if project.public_token:
        return project
    project.public_token = _mint_public_token()
    project.public_enabled_at = now()
    project.public_enabled_by_id = user.id
    add_activity(
        db,
        project,
        user,
        ActivityType.PUBLIC_LINK_ENABLED,
        f"{user.email} made this project readable by anyone with the link",
    )
    await db.commit()
    await db.refresh(project)
    return project


async def rotate_public_link(db: AsyncSession, project: Project, user: User) -> Project:
    """Mint a new token, which is what revokes every link already handed out."""
    if not project.public_token:
        return await enable_public_link(db, project, user)
    project.public_token = _mint_public_token()
    project.public_enabled_at = now()
    project.public_enabled_by_id = user.id
    add_activity(
        db,
        project,
        user,
        ActivityType.PUBLIC_LINK_ROTATED,
        f"{user.email} replaced the public link — the previous one no longer works",
    )
    await db.commit()
    await db.refresh(project)
    return project


async def disable_public_link(db: AsyncSession, project: Project, user: User) -> Project:
    """Take it down. Idempotent, so a double click is not an error."""
    if not project.public_token:
        return project
    project.public_token = None
    project.public_enabled_at = None
    project.public_enabled_by_id = None
    add_activity(
        db,
        project,
        user,
        ActivityType.PUBLIC_LINK_DISABLED,
        f"{user.email} turned off the public link",
    )
    await db.commit()
    await db.refresh(project)
    return project


async def project_by_public_token(db: AsyncSession, token: str) -> Project:
    """The anonymous read. No user, no membership, no session.

    A deleted or archived project is treated as absent rather than served: the
    owner's last action was to take it away, and a link handed out beforehand
    must not outlive that. Every failure raises the same NotFoundError, so the
    endpoint cannot be used to tell "wrong token" from "link switched off".
    """
    # An empty token would otherwise match every row whose column is NULL and
    # hand out a private project.
    if not token or not token.strip():
        raise NotFoundError(ErrorMessage.PUBLIC_LINK_NOT_FOUND)
    result = await db.execute(
        select(Project).where(
            Project.public_token == token,
            Project.is_deleted.is_(False),
            Project.is_archived.is_(False),
        )
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise NotFoundError(ErrorMessage.PUBLIC_LINK_NOT_FOUND)
    return project
