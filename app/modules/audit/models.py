import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import Base, JSONType, TimestampMixin, UUIDMixin


class AuditLog(Base, UUIDMixin, TimestampMixin):
    """Security-relevant events: sign-ins, role changes, deletions, invites.

    Written explicitly by services rather than by ORM event hooks. Hooks catch
    every UPDATE, including the noise ones (`last_seen_at` on each request), and
    a log nobody can read is a log nobody reads. Deliberate calls mean each row
    is an event a human chose to record.

    ``actor_email`` is copied rather than joined so a deleted account leaves a
    readable trail instead of a NULL where the actor used to be.
    """

    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    __table_args__ = (Index("ix_audit_logs_entity", "entity_type", "entity_id"),)
