import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, MetaData, Uuid, false, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

from app.core.time import now

# Deterministic constraint names so Alembic can always drop them (downgrades work).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# JSONB on Postgres (indexable, typed) and plain JSON everywhere else, so the
# test suite can run the same models on SQLite. Declared once: a model that
# picks JSONB directly silently stops being testable off Postgres.
JSONType: TypeEngine[Any] = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDMixin:
    """Plain UUID primary key, generated client-side so the id is known before
    the INSERT — which is what lets a service build related rows in one flush."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """created_at / updated_at only, for lean or high-volume tables.

    The values are generated **in Python**, with the SQL defaults kept only as a
    backstop for rows written outside the app. This is not a style choice: a
    column whose value is produced by the server is marked expired after the
    INSERT or UPDATE, because SQLAlchemy does not know what the database put
    there. Reading it then triggers a lazy SELECT — and in async code a lazy
    SELECT from a plain function is a `MissingGreenlet` crash. Every response
    that serialises `updated_at` right after a save would hit it.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
        onupdate=now,
        server_default=func.now(),
        nullable=False,
    )


class AuditMixin(TimestampMixin):
    """Timestamps + soft delete + who touched it, for every business table.

    Soft delete rather than a real DELETE because a project is shared: one
    member pressing delete must not vaporise work the other four can see, and
    "restore" has to be a button rather than a database restore.
    """

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False, index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
