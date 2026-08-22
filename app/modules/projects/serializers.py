"""Turning ORM rows into the shapes the API promises.

Kept out of the service so the service can stay about *rules* and the router
about *routes*. Every function here is deliberately given everything it needs
as an argument — none of them touch the session, so no serialiser can trigger a
lazy load in the middle of building a response.
"""

import uuid
from collections.abc import Sequence

from app.core.constants import MemberStatus, ProjectRole
from app.modules.projects.models import Project, ProjectMember
from app.modules.projects.schemas import MemberRead, ProjectDetail, ProjectSummary
from app.modules.users.models import User
from app.modules.users.schemas import UserSummary


def member_read(member: ProjectMember, account: User | None) -> MemberRead:
    return MemberRead(
        id=member.id,
        email=member.email,
        role=ProjectRole(member.role),
        status=MemberStatus(member.status),
        user=UserSummary.model_validate(account) if account else None,
        invited_at=member.invited_at,
        joined_at=member.joined_at,
        last_opened_at=member.last_opened_at,
    )


def _active_members(project: Project) -> list[ProjectMember]:
    return [m for m in project.members if m.status != MemberStatus.REVOKED]


def summary(
    project: Project,
    *,
    my_role: ProjectRole | None = None,
    owner: User | None = None,
) -> ProjectSummary:
    return ProjectSummary(
        id=project.id,
        name=project.name,
        description=project.description,
        schema_version=project.schema_version,
        doc_version=project.doc_version,
        screen_count=project.screen_count,
        module_count=project.module_count,
        is_archived=project.is_archived,
        owner=UserSummary.model_validate(owner) if owner else None,
        member_count=len(_active_members(project)),
        my_role=my_role,
        created_at=project.created_at,
        updated_at=project.updated_at,
        last_activity_at=project.last_activity_at,
    )


def detail(
    project: Project,
    members: Sequence[tuple[ProjectMember, User | None]],
    *,
    my_role: ProjectRole | None = None,
    owner: User | None = None,
) -> ProjectDetail:
    base = summary(project, my_role=my_role, owner=owner)
    return ProjectDetail(
        **base.model_dump(),
        doc=project.doc or {},
        members=[member_read(member, account) for member, account in members],
    )


def owner_map(users: Sequence[User]) -> dict[uuid.UUID, User]:
    return {user.id: user for user in users}
