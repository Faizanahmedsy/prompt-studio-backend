import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import Base, TimestampMixin, UUIDMixin


class RevokedToken(Base):
    """Denylist of revoked JWT ids.

    A token whose ``jti`` appears here is rejected even while unexpired — this
    is what makes "sign out" and "deactivate this user" take effect before the
    access token would have died on its own. ``expires_at`` mirrors the token's
    own expiry so rows can be purged once the token is worthless anyway.
    """

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class Session(Base, UUIDMixin, TimestampMixin):
    """One signed-in device.

    Only the refresh token's **fingerprint** is stored (SHA-256), never the
    token: a dump of this table hands an attacker nothing they can present.
    Rotating on every refresh means a stolen refresh token is usable exactly
    once before the legitimate client's next refresh invalidates it.
    """

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # The access token minted alongside this refresh token.
    #
    # A session row was the only record of a live sign-in, and it held nothing
    # that could reach the access token it issued — so "sign out everywhere",
    # revoking a device from the sessions screen, and an operator re-issuing a
    # password all stamped `revoked_at` and left the intruder reading and
    # editing for up to an hour. The last of those is the worst: it is the
    # incident-response action, and it handed a stolen access token the right to
    # call `set-initial-password` and take the account permanently.
    #
    # Nullable for rows written before this column existed.
    access_jti: Mapped[str | None] = mapped_column(String(64), index=True)
    # When that access token would have expired anyway. Kept so a revocation
    # row is not left on the denylist for the refresh token's full lifetime.
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_live(self) -> bool:
        from app.core.time import now

        return self.revoked_at is None and self.expires_at > now()
