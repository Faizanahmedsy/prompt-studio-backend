import logging
import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Query, Request, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ProjectRole
from app.core.envelope import set_response_message
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.messages import ErrorMessage, ResponseMessage
from app.core.pagination import Page, PageQuery
from app.core.rate_limit import PUBLIC_READ, limit
from app.modules.auth.dependencies import CurrentUser, DbSession
from app.modules.projects import serializers, service
from app.modules.projects.access import (
    ProjectCommenter,
    ProjectEditor,
    ProjectOwner,
    ProjectViewer,
)
from app.modules.projects.models import Project
from app.modules.projects.schemas import (
    ActivityRead,
    CommentCreate,
    CommentRead,
    CommentUpdate,
    MemberBulkInvite,
    MemberInvite,
    MemberRead,
    MemberUpdate,
    PresenceMember,
    ProjectCreate,
    ProjectDetail,
    ProjectDocSave,
    ProjectSummary,
    ProjectUpdate,
    PublicLinkRead,
    PublicProjectRead,
    VersionCreate,
    VersionDetail,
    VersionSummary,
)
from app.modules.users.models import User

logger = logging.getLogger("app.projects")

router = APIRouter(prefix="/projects", tags=["Projects"])


@router.get("", response_model=Page[ProjectSummary])
async def list_projects(
    db: DbSession,
    current_user: CurrentUser,
    params: PageQuery,
    search: str | None = Query(None, description="Match on name or description"),
    status_filter: str = Query(
        "active",
        alias="status",
        pattern="^(active|archived|all)$",
        description="active (default), archived, or all",
    ),
    owned_only: bool = Query(False, description="Only projects you own"),
    sort: str = Query("recent", pattern="^(recent|updated|created|name|screens)$"),
) -> Page[ProjectSummary]:
    """Projects your address is on. Nothing else is visible, to anyone."""
    page = await service.list_projects(
        db,
        current_user,
        params,
        search=search,
        archived={"active": False, "archived": True, "all": None}[status_filter],
        owned_only=owned_only,
        sort=sort,
    )
    roles = await service.role_map(db, current_user, [p.id for p in page.items])
    owners = await _owner_map(db, page.items)
    return Page.build(
        [
            serializers.summary(
                project,
                my_role=roles.get(project.id),
                owner=owners.get(project.owner_id) if project.owner_id else None,
            )
            for project in page.items
        ],
        page.total,
        params,
    )


@router.post("", response_model=ProjectDetail, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate, db: DbSession, current_user: CurrentUser, request: Request
) -> ProjectDetail:
    project = await service.create_project(db, current_user, data)
    members = await service.list_members(db, project.id)
    set_response_message(request, ResponseMessage.PROJECT_CREATED)
    return serializers.detail(project, members, my_role=ProjectRole.OWNER, owner=current_user)


@router.get("/{project_id}", response_model=ProjectDetail)
async def read_project(access: ProjectViewer, db: DbSession) -> ProjectDetail:
    """The project including its full document."""
    await service.mark_opened(db, access)
    members = await service.list_members(db, access.project.id)
    owner = await db.get(User, access.project.owner_id) if access.project.owner_id else None
    return serializers.detail(access.project, members, my_role=access.role, owner=owner)


@router.patch("/{project_id}", response_model=ProjectSummary)
async def update_project(
    data: ProjectUpdate,
    access: ProjectEditor,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> ProjectSummary:
    project = await service.update_project(db, access, current_user, data)
    set_response_message(request, ResponseMessage.PROJECT_UPDATED)
    owner = await db.get(User, project.owner_id) if project.owner_id else None
    return serializers.summary(project, my_role=access.role, owner=owner)


@router.put("/{project_id}/document", response_model=ProjectSummary)
async def save_document(
    data: ProjectDocSave,
    access: ProjectEditor,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> ProjectSummary:
    """Save the diagram.

    Send `base_version` (the `doc_version` you loaded) and a save that would
    overwrite someone else's newer work is refused with 409 and the current
    version number, rather than silently winning.
    """
    project = await service.save_doc(db, access, current_user, data)
    set_response_message(request, ResponseMessage.PROJECT_UPDATED)
    return serializers.summary(project, my_role=access.role)


@router.delete("/{project_id}", status_code=status.HTTP_200_OK)
async def delete_project(
    access: ProjectOwner, db: DbSession, current_user: CurrentUser, request: Request
) -> None:
    """Move to trash. Recoverable — a shared project must not vanish because
    one member pressed delete."""
    await service.delete_project(db, access.project, current_user)
    set_response_message(request, ResponseMessage.PROJECT_DELETED)


@router.post("/{project_id}/restore", response_model=ProjectSummary)
async def restore_project(
    project_id: uuid.UUID, db: DbSession, current_user: CurrentUser, request: Request
) -> ProjectSummary:
    """Bring a trashed project back.

    Resolved by hand rather than through the usual dependency: that one filters
    deleted projects out, which is exactly the row this route needs. Ownership
    is therefore checked here.
    """
    project = await db.get(Project, project_id)
    if project is None:
        raise NotFoundError(ErrorMessage.PROJECT_NOT_FOUND)
    member = await service.member_by_email(db, project_id, current_user.email)
    if member is None:
        # 404, matching every other project route: "forbidden" would confirm
        # that a project with this id exists to someone not on it.
        raise NotFoundError(ErrorMessage.PROJECT_NOT_FOUND)
    if member.role != ProjectRole.OWNER:
        raise AuthorizationError(ErrorMessage.NOT_PROJECT_OWNER)

    restored = await service.restore_project(db, project_id, current_user)
    set_response_message(request, ResponseMessage.PROJECT_RESTORED)
    return serializers.summary(restored, my_role=ProjectRole.OWNER)


# ── membership ───────────────────────────────────────────────────────────────


@router.get("/{project_id}/members", response_model=list[MemberRead])
async def list_members(access: ProjectViewer, db: DbSession) -> list[MemberRead]:
    return [
        serializers.member_read(member, account)
        for member, account in await service.list_members(db, access.project.id)
    ]


@router.post(
    "/{project_id}/members", response_model=MemberRead, status_code=status.HTTP_201_CREATED
)
async def add_member(
    data: MemberInvite,
    access: ProjectOwner,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> MemberRead:
    """Share the project with one email address.

    The address does not need an account yet — the invitation waits for it, and
    is claimed the moment someone registers with that address.
    """
    member = await service.invite_member(db, access.project, current_user, data)
    account = await db.get(User, member.user_id) if member.user_id else None
    set_response_message(request, ResponseMessage.MEMBER_ADDED)
    return serializers.member_read(member, account)


@router.post("/{project_id}/members/bulk", response_model=list[MemberRead])
async def add_members(
    data: MemberBulkInvite,
    access: ProjectOwner,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> list[MemberRead]:
    """Share with several addresses at once.

    An address that is already on the project is skipped rather than failing the
    batch — pasting a team list twice should not be an error.
    """
    added: list[MemberRead] = []
    for email in data.emails:
        try:
            member = await service.invite_member(
                db, access.project, current_user, MemberInvite(email=email, role=data.role)
            )
        except ConflictError:
            continue
        account = await db.get(User, member.user_id) if member.user_id else None
        added.append(serializers.member_read(member, account))
    set_response_message(request, ResponseMessage.MEMBER_ADDED)
    return added


@router.patch("/{project_id}/members/{member_id}", response_model=MemberRead)
async def update_member(
    member_id: uuid.UUID,
    data: MemberUpdate,
    access: ProjectOwner,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> MemberRead:
    member = await service.update_member_role(
        db, access.project, current_user, member_id, data.role
    )
    account = await db.get(User, member.user_id) if member.user_id else None
    set_response_message(request, ResponseMessage.MEMBER_UPDATED)
    return serializers.member_read(member, account)


@router.delete("/{project_id}/members/{member_id}")
async def remove_member(
    member_id: uuid.UUID,
    access: ProjectOwner,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> None:
    await service.remove_member(db, access.project, current_user, member_id)
    set_response_message(request, ResponseMessage.MEMBER_REMOVED)


@router.post("/{project_id}/leave")
async def leave_project(
    access: ProjectViewer, db: DbSession, current_user: CurrentUser, request: Request
) -> None:
    await service.leave_project(db, access, current_user)
    set_response_message(request, ResponseMessage.LEFT_PROJECT)


@router.get("/{project_id}/presence", response_model=list[PresenceMember])
async def read_presence(access: ProjectViewer) -> list[PresenceMember]:
    """Who has this project open right now, for clients that poll instead of
    holding the websocket open.

    Reads the shared snapshot rather than this worker's own rooms: the request
    is answered by whichever worker the load balancer picked, which is usually
    not the one holding the sockets.
    """
    from app.modules.collab.hub import hub

    people: list[PresenceMember] = []
    for entry in await hub.presence_snapshot(access.project.id):
        try:
            people.append(PresenceMember(**entry))
        except PydanticValidationError:
            # These rows are read back out of Redis, so a malformed one is data
            # rather than a bug in this request. Unguarded it escapes as a raw
            # 500 outside the response envelope — one bad row would break the
            # avatar stack for everyone in the project.
            logger.warning("Skipping an unreadable presence row on %s", access.project.id)
    return people


# ── versions ─────────────────────────────────────────────────────────────────


@router.get("/{project_id}/versions", response_model=Page[VersionSummary])
async def list_versions(
    access: ProjectViewer, db: DbSession, params: PageQuery
) -> Page[VersionSummary]:
    page = await service.list_versions(db, access.project.id, params)
    authors = await service.authors_of(db, page.items)
    return Page.build(
        [
            serializers.version_summary(
                item, authors.get(item.created_by) if item.created_by else None
            )
            for item in page.items
        ],
        page.total,
        params,
    )


@router.post("/{project_id}/versions", response_model=VersionSummary, status_code=201)
async def save_version(
    access: ProjectEditor,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
    data: VersionCreate | None = None,
) -> VersionSummary:
    # Defaulted in the body, not the signature: a model instance in a default
    # argument is built once at import and then shared by every request.
    version = await service.save_version(db, access, current_user, (data or VersionCreate()).label)
    set_response_message(request, ResponseMessage.VERSION_SAVED)
    return serializers.version_summary(version, current_user)


@router.get("/{project_id}/versions/{version_id}", response_model=VersionDetail)
async def read_version(
    version_id: uuid.UUID, access: ProjectViewer, db: DbSession
) -> VersionDetail:
    version = await service.get_version(db, access.project.id, version_id)
    author = (await service.authors_of(db, [version])).get(version.created_by or uuid.uuid4())
    return VersionDetail(
        **serializers.version_summary(version, author).model_dump(),
        doc=version.doc,
    )


@router.post("/{project_id}/versions/{version_id}/restore", response_model=ProjectSummary)
async def restore_version(
    version_id: uuid.UUID,
    access: ProjectEditor,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> ProjectSummary:
    project = await service.restore_version(db, access, current_user, version_id)
    set_response_message(request, ResponseMessage.VERSION_RESTORED)
    return serializers.summary(project, my_role=access.role)


# ── activity & comments ──────────────────────────────────────────────────────


@router.get("/{project_id}/activity", response_model=Page[ActivityRead])
async def list_activity(
    access: ProjectViewer, db: DbSession, params: PageQuery
) -> Page[ActivityRead]:
    page = await service.list_activity(db, access.project.id, params)
    return Page.build(
        [ActivityRead.model_validate(item) for item in page.items], page.total, params
    )


@router.get("/{project_id}/comments", response_model=list[CommentRead])
async def list_comments(
    access: ProjectViewer,
    db: DbSession,
    include_resolved: bool = Query(False),
) -> list[CommentRead]:
    comments = await service.list_comments(db, access.project.id, include_resolved=include_resolved)
    return [CommentRead.model_validate(item) for item in comments]


@router.post("/{project_id}/comments", response_model=CommentRead, status_code=201)
async def add_comment(
    data: CommentCreate, access: ProjectCommenter, db: DbSession, current_user: CurrentUser
) -> CommentRead:
    comment = await service.add_comment(db, access, current_user, data)
    return CommentRead.model_validate(comment)


@router.patch("/{project_id}/comments/{comment_id}", response_model=CommentRead)
async def update_comment(
    comment_id: uuid.UUID,
    data: CommentUpdate,
    access: ProjectCommenter,
    db: DbSession,
    current_user: CurrentUser,
) -> CommentRead:
    comment = await service.update_comment(db, access, current_user, comment_id, data)
    return CommentRead.model_validate(comment)


@router.delete("/{project_id}/comments/{comment_id}")
async def delete_comment(
    comment_id: uuid.UUID, access: ProjectCommenter, db: DbSession, current_user: CurrentUser
) -> None:
    await service.delete_comment(db, access, current_user, comment_id)


async def _owner_map(db: AsyncSession, projects: Sequence[Project]) -> dict[uuid.UUID, User]:
    """Load every owner in one query rather than one per row."""
    owner_ids = {p.owner_id for p in projects if p.owner_id}
    if not owner_ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(owner_ids)))
    return {user.id: user for user in result.scalars()}


# ── the public read-only link ────────────────────────────────────────────────
#
# Owner-only to manage, anonymous to read. The read route lives on its own
# router (`public_router`, mounted without the auth dependency) because
# everything under `/projects` requires a signed-in caller by design, and
# poking a hole in that prefix is how the hole ends up somewhere else later.


@router.get("/{project_id}/public-link", response_model=PublicLinkRead)
async def read_public_link(access: ProjectOwner) -> PublicLinkRead:
    """Whether this project has a live public link, and what it is."""
    return serializers.public_link(access.project)


@router.post("/{project_id}/public-link", response_model=PublicLinkRead)
async def enable_public_link(
    access: ProjectOwner, db: DbSession, current_user: CurrentUser, request: Request
) -> PublicLinkRead:
    """Turn the link on. Idempotent — clicking twice returns the same token."""
    project = await service.enable_public_link(db, access.project, current_user)
    set_response_message(request, ResponseMessage.PUBLIC_LINK_ENABLED)
    return serializers.public_link(project)


@router.post("/{project_id}/public-link/rotate", response_model=PublicLinkRead)
async def rotate_public_link(
    access: ProjectOwner, db: DbSession, current_user: CurrentUser, request: Request
) -> PublicLinkRead:
    """Replace the token, which is what revokes links already handed out."""
    project = await service.rotate_public_link(db, access.project, current_user)
    set_response_message(request, ResponseMessage.PUBLIC_LINK_ROTATED)
    return serializers.public_link(project)


@router.delete("/{project_id}/public-link", response_model=PublicLinkRead)
async def disable_public_link(
    access: ProjectOwner, db: DbSession, current_user: CurrentUser, request: Request
) -> PublicLinkRead:
    """Take it down. Idempotent."""
    project = await service.disable_public_link(db, access.project, current_user)
    set_response_message(request, ResponseMessage.PUBLIC_LINK_DISABLED)
    return serializers.public_link(project)


public_router = APIRouter(prefix="/public", tags=["Public"])


@public_router.get(
    "/projects/{token}",
    response_model=PublicProjectRead,
    dependencies=[limit(PUBLIC_READ)],
)
async def read_public_project(token: str, db: DbSession) -> PublicProjectRead:
    """One shared diagram, to anyone holding the link. No account required.

    Read-only by construction: there is no counterpart write route, and the
    token is never accepted anywhere a document is saved.
    """
    project = await service.project_by_public_token(db, token)
    return serializers.public_project(project)
