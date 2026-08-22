import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base_model import AuditMixin, Base, JSONType, TimestampMixin, UUIDMixin
from app.core.constants import MemberStatus, ProjectRole


class Project(Base, UUIDMixin, AuditMixin):
    """A Prompt Studio diagram, in full.

    ``doc`` is the entire ProjectDoc the editor works on — views, screens,
    modules, edges, theme, per-surface stacks, conventions, requirements. It is
    stored as one JSON document rather than shredded into tables on purpose:
    the frontend already owns that schema (`types/project.ts`, versioned and
    migrated by zod), the editor reads and writes it whole, and a normalised
    mirror here would have to be migrated in lockstep with the client to add a
    single field. The columns beside it are the ones the *server* needs to
    answer questions the JSON cannot: who may see it, what changed, and whether
    the copy being saved is stale.
    """

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    doc: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    # Mirrors SCHEMA_VERSION in the frontend's types/project.ts. Stored so an
    # older client can be told its document shape is behind rather than silently
    # overwriting fields it does not know about.
    schema_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    # Bumped on every accepted save. A client sends the version it started from;
    # a mismatch is a 409 rather than a silent overwrite of someone else's work.
    doc_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)

    # Denormalised counts, so a project list does not run three aggregates per
    # row. Refreshed by `service.sync_counts` whenever the document is saved.
    screen_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    module_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False, index=True
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_projects_owner_deleted", "owner_id", "is_deleted"),)


class ProjectMember(Base, UUIDMixin, TimestampMixin):
    """Who may open one project, addressed by **email**.

    The email is the identity, not the user id. A project is shared with people
    who may not have signed up yet, so a row can exist with ``user_id IS NULL``
    and ``status = INVITED``; registering with that address claims every such
    row (`service.claim_invites`). Storing only a user id would mean an invite
    could not be written until the invitee registered, and the person doing the
    inviting would have no way to express "let this address in".
    """

    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Lower-cased at write time — same normalisation as `users.email`, so an
    # invitation to Faizan@x.com is claimed by an account created as faizan@x.com.
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(
        String(20), default=ProjectRole.VIEWER, server_default=ProjectRole.VIEWER, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default=MemberStatus.INVITED,
        server_default=MemberStatus.INVITED,
        nullable=False,
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped["Project"] = relationship(back_populates="members")

    __table_args__ = (
        # One row per address per project. Without this, inviting the same
        # person twice creates a second membership and a role change updates
        # only one of them.
        UniqueConstraint("project_id", "email", name="uq_project_members_project_id_email"),
        Index("ix_project_members_user_status", "user_id", "status"),
    )


class ProjectVersion(Base, UUIDMixin, TimestampMixin):
    """A named snapshot of the document, kept so work can be rolled back.

    Separate rows rather than an array inside `projects.doc`: snapshots are
    written often and read rarely, and keeping them in the live document would
    mean every open of the editor downloads every historical copy of itself.
    """

    __tablename__ = "project_versions"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    doc: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    doc_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # True for snapshots the server took by itself before overwriting a
    # document, as opposed to ones a person deliberately named.
    is_auto: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )


class ProjectActivity(Base, UUIDMixin, TimestampMixin):
    """Per-project feed: who did what, in order.

    The actor's email is copied in rather than joined at read time so the feed
    still reads correctly after the account is deleted — "removed by
    someone@gone.com" is history, and a NULL there would erase it.
    """

    __tablename__ = "project_activity"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


class ProjectComment(Base, UUIDMixin, AuditMixin):
    """A note pinned to the canvas, a screen or a module.

    ``target_kind`` / ``target_key`` address the thing being discussed using the
    document's own stable keys rather than database ids, because screens live
    inside the JSON document and have no row of their own. A comment on a
    deleted screen keeps its key and simply stops resolving — deliberately
    kept rather than cascaded away, so the discussion survives an undo.
    """

    __tablename__ = "project_comments"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    author_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    target_kind: Mapped[str] = mapped_column(String(20), default="canvas", nullable=False)
    target_key: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
