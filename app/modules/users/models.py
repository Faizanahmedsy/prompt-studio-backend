import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base_model import AuditMixin, Base, UUIDMixin
from app.core.constants import GlobalRole


class User(Base, UUIDMixin, AuditMixin):
    """Core identity.

    The platform role lives here as a single column rather than in role /
    permission join tables. This product has exactly three platform roles and a
    fixed matrix of what each may do (`app.modules.rbac.permissions`); a
    normalised RBAC schema would add four tables, four joins per request and a
    second source of truth that drifts from the code checking it. Per-project
    standing is a different question entirely and lives on `project_members`.
    """

    __tablename__ = "users"

    # Stored lower-cased (see `users.service.normalise_email`) so sign-in,
    # invitations and duplicate checks all agree on what "the same address"
    # means. A unique index over a mixed-case column would let Faizan@x.com and
    # faizan@x.com both register and then fight over one project membership.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    # Deterministic display colour for collaboration cursors, assigned at signup.
    accent_color: Mapped[str] = mapped_column(String(9), default="#4f46e5", nullable=False)

    role: Mapped[str] = mapped_column(
        String(20), default=GlobalRole.MEMBER, server_default=GlobalRole.MEMBER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Kept alongside `role` because it is a different claim: SUPERADMIN is a job
    # title that an admin may grant, `is_superuser` is the break-glass flag on
    # the seeded founder account that nothing in the API can revoke.
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Throttling state for password guessing. Reset on any successful sign-in.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    theme: Mapped[str] = mapped_column(String(20), default="system", nullable=False)

    credentials: Mapped["UserCredentials"] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="UserCredentials.user_id",
        lazy="selectin",
    )

    @property
    def display_name(self) -> str:
        return self.full_name.strip() or self.email

    @property
    def is_admin(self) -> bool:
        """Reaches the admin surface. Superusers always do."""
        return self.is_superuser or self.role in (GlobalRole.SUPERADMIN, GlobalRole.ADMIN)


class UserCredentials(Base, AuditMixin):
    """Authentication secrets, isolated from everything that reads user data.

    A separate table so the common `SELECT * FROM users` — done on every
    authenticated request, serialised into every member list — never carries a
    password hash into memory it did not need.
    """

    __tablename__ = "user_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # True when the password was issued *to* the person rather than chosen *by*
    # them — an admin-created account. While set, every authenticated route
    # except the one that clears it responds 403, so an emailed credential
    # cannot be used to browse the app.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="credentials", foreign_keys=[user_id])
