"""Turning ORM rows into the shapes the API promises.

Kept out of the service so the service can stay about *rules* and the router
about *routes*. Every function here is deliberately given everything it needs
as an argument — none of them touch the session, so no serialiser can trigger a
lazy load in the middle of building a response.
"""

import uuid
from collections.abc import Sequence

from app.core.constants import MemberStatus, ProjectRole
from app.modules.projects.models import Project, ProjectMember, ProjectVersion
from app.modules.projects.schemas import (
    MemberRead,
    ProjectDetail,
    ProjectSummary,
    PublicLinkRead,
    PublicProjectRead,
    VersionSummary,
)
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
        # The owner's alone. Minting, rotating and revoking the public link are
        # all owner-only routes precisely because publishing the project to the
        # internet is the owner's decision — and handing the token to every
        # VIEWER let the least-privileged member make that decision instead, by
        # pasting a URL, with nothing in the activity feed to say who did.
        #
        # Also the reason it must never be added to `ProjectSummary`, which the
        # admin surface and the list endpoint reuse.
        public_token=project.public_token if my_role == ProjectRole.OWNER else None,
    )


def public_project(project: Project) -> PublicProjectRead:
    """The anonymous view: the diagram, and nothing about the team behind it."""
    return PublicProjectRead(
        id=project.id,
        name=project.name,
        description=project.description,
        schema_version=project.schema_version,
        doc_version=project.doc_version,
        doc=project.doc or {},
        updated_at=project.updated_at,
    )


def public_link(project: Project) -> PublicLinkRead:
    return PublicLinkRead(
        enabled=project.public_token is not None,
        token=project.public_token,
        enabled_at=project.public_enabled_at,
    )


def owner_map(users: Sequence[User]) -> dict[uuid.UUID, User]:
    return {user.id: user for user in users}


def version_summary(version: ProjectVersion, author: User | None) -> VersionSummary:
    """A snapshot with its author's name attached."""
    return VersionSummary(
        id=version.id,
        label=version.label,
        doc_version=version.doc_version,
        is_auto=version.is_auto,
        created_at=version.created_at,
        created_by=version.created_by,
        created_by_name=author.display_name if author else "",
        created_by_email=author.email if author else "",
    )
