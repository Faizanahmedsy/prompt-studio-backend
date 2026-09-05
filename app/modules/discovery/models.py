"""Discovery: the question tree a generator asks about a project, and the
answers a human gives back.

Normalised rather than another JSON blob on `projects.doc`. The rows are
written incrementally by an agent, read one module at a time by the studio, and
every decision has to keep its own history — three things a single document
cannot do without rewriting all of it on every keystroke.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import Base, JSONType, TimestampMixin, UUIDMixin
from app.core.time import now


class DiscoveryRun(Base, UUIDMixin, TimestampMixin):
    """One pass of discovery over a project — a batch of questions with a name."""

    __tablename__ = "discovery_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # Where the questions came from — a prototype path, a commit, a tool name.
    source: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )


class DiscoveryItem(Base, UUIDMixin, TimestampMixin):
    """One question, rule, issue or missing-module note inside a run."""

    __tablename__ = "discovery_items"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The generator's own id — `R1`, `Q6`, `I-108`, `M-016`. Unique per run, so
    # a re-push of the same question updates nothing by accident and a client
    # can address an item by the key it already knows.
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    family: Mapped[str] = mapped_column(String(12), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    severity: Mapped[str | None] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Which modules of the product this touches. A list, because a decision
    # about auth is a decision about every screen behind it.
    modules: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    # `[{key, label, consequence, decision}]` — the choices offered, as written
    # by the generator. Stored whole: the studio renders them and nothing on the
    # server reasons about their contents.
    options: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    # The generator's own answer, if it had one. Accepting it is one click.
    proposed: Mapped[str | None] = mapped_column(Text)
    proposed_key: Mapped[str | None] = mapped_column(String(40))
    # True for the questions a human has to answer — the rest may be swept up
    # by a bulk accept without anybody reading them.
    needs_user: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    depends_on: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_discovery_items_run_id_key"),)


class DiscoveryAnswer(Base, UUIDMixin):
    """A decision on one item. **Append-only: the newest row wins.**

    Rows rather than a column on the item because a discovery run is an audit
    trail — "we decided soft delete, then changed our minds" is the useful part,
    and an UPDATE would erase it.
    """

    __tablename__ = "discovery_answers"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("discovery_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    choice_key: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, server_default=func.now(), nullable=False
    )


class DiscoveryArtifact(Base, UUIDMixin, TimestampMixin):
    """A generated file, addressed by its path under `weave/`.

    Hung off the **project**, not the run: `weaver push` writes files from a
    working tree and has no run id to quote, and the studio wants the current
    knowledge base rather than the one that happened to be produced alongside a
    particular batch of questions.
    """

    __tablename__ = "discovery_artifacts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # A path with slashes — `discovery/kb.md`.
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="md", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # SHA-256 of the body, computed on write. A client that already holds the
    # file can tell whether it is behind without downloading four megabytes.
    sha: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_discovery_artifacts_project_id_name"),
    )
