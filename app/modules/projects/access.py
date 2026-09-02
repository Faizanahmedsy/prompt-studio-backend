"""Who may open a project, and with what standing.

The rule the product is built on: **a project is visible only to the addresses
added to it.** Nothing else grants sight of a document — not being a platform
ADMIN, not being a SUPERADMIN. Those roles administer *accounts*; letting them
read project content would quietly undo the guarantee the sharing model makes.

Where the admin surface needs to see projects it gets metadata only (name,
owner, member count, sizes) from `admin.service`, never `projects.doc`. The one
platform power that reaches into a project is deletion, which is destructive
rather than revealing and is audited.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Path, params
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import MemberStatus, ProjectRole
from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.messages import ErrorMessage
from app.core.time import now
from app.modules.auth.dependencies import CurrentUser, DbSession
from app.modules.projects.models import Project, ProjectMember
from app.modules.users.models import User


@dataclass(frozen=True)
class ProjectAccess:
    """A resolved answer to "may this person do this, here?".

    Passed around instead of a bare `Project` so a service never has to re-ask
    the question — and can't answer it differently the second time.
    """

    project: Project
    member: ProjectMember
    role: ProjectRole

    @property
    def can_edit(self) -> bool:
        return self.role.at_least(ProjectRole.EDITOR)

    @property
    def is_owner(self) -> bool:
        return self.role == ProjectRole.OWNER


async def load_project(db: AsyncSession, project_id: uuid.UUID) -> Project:
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.members))
        .where(Project.id == project_id, Project.is_deleted.is_(False))
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise NotFoundError(ErrorMessage.PROJECT_NOT_FOUND)
    return project


async def find_membership(
    db: AsyncSession, project_id: uuid.UUID, user: User
) -> ProjectMember | None:
    """The caller's row on this project, matched by user id **or** address.

    Both, because the two can disagree for exactly as long as it takes an
    invitation to be claimed: the row is written with an email and no user id,
    and a member who signed in before the claim ran would otherwise be told
    they have no access to the project they were just invited to.
    """
    # The address half of that only counts for an account that has confirmed
    # the address. Without the check, membership was granted to whoever held
    # the email string — so an attacker who registered an address before it was
    # ever invited walked into the project the moment somebody shared with it,
    # and the real owner of the address could never register at all.
    from app.modules.projects.service import _membership_match

    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.status != MemberStatus.REVOKED,
            _membership_match(user),
        )
    )
    member = result.scalars().first()
    if member is not None and member.user_id is None:
        # The row was written before this address had an account, and matching
        # on the email is what let them in. `user_id` is the key every eviction
        # path uses, so leaving it NULL means a removal quietly does nothing —
        # fill it in the moment we know who this is.
        member.user_id = user.id
        member.status = MemberStatus.ACTIVE
        member.joined_at = member.joined_at or now()
        await db.commit()
    return member


async def resolve_access(
    db: AsyncSession, project_id: uuid.UUID, user: User, minimum: ProjectRole
) -> ProjectAccess:
    project = await load_project(db, project_id)
    member = await find_membership(db, project_id, user)
    if member is None:
        # 404, not 403. Answering "forbidden" would confirm that a project with
        # this id exists, which is a fact only its members are entitled to.
        raise NotFoundError(ErrorMessage.PROJECT_NOT_FOUND)

    role = ProjectRole(member.role)
    if not role.at_least(minimum):
        raise AuthorizationError(ErrorMessage.PROJECT_ROLE_TOO_LOW)
    return ProjectAccess(project=project, member=member, role=role)


def require_project(minimum: ProjectRole) -> params.Depends:
    """Dependency factory: resolve `{project_id}` and demand at least `minimum`.

    Returned as a dependency rather than called inside each handler so the check
    cannot be forgotten — a route without it simply has no project to work on.
    """

    async def dependency(
        db: DbSession,
        current_user: CurrentUser,
        project_id: Annotated[uuid.UUID, Path()],
    ) -> ProjectAccess:
        return await resolve_access(db, project_id, current_user, minimum)

    return cast(params.Depends, Depends(dependency))


ProjectViewer = Annotated[ProjectAccess, require_project(ProjectRole.VIEWER)]
ProjectCommenter = Annotated[ProjectAccess, require_project(ProjectRole.COMMENTER)]
ProjectEditor = Annotated[ProjectAccess, require_project(ProjectRole.EDITOR)]
ProjectOwner = Annotated[ProjectAccess, require_project(ProjectRole.OWNER)]
