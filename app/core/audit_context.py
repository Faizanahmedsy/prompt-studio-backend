"""Request-scoped actor context for the audit trail.

Set by the authenticated-user dependency, read later in the same request when a
row is written. Contextvars are task-local and every request is its own asyncio
task, so there is no cross-request leakage.
"""

import contextvars
import uuid
from dataclasses import dataclass

_actor_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "audit_actor_id", default=None
)
_actor_email: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "audit_actor_email", default=None
)
_actor_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "audit_actor_ip", default=None
)
_user_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "audit_user_agent", default=None
)


@dataclass(frozen=True)
class AuditContext:
    actor_id: uuid.UUID | None
    actor_email: str | None
    ip: str | None
    user_agent: str | None


def set_audit_actor(user_id: uuid.UUID | None, email: str | None) -> None:
    _actor_id.set(user_id)
    _actor_email.set(email)


def set_request_meta(ip: str | None, user_agent: str | None) -> None:
    """Bound separately from the actor so anonymous requests are captured too."""
    _actor_ip.set(ip)
    _user_agent.set(user_agent)


def get_audit_context() -> AuditContext:
    return AuditContext(_actor_id.get(), _actor_email.get(), _actor_ip.get(), _user_agent.get())
