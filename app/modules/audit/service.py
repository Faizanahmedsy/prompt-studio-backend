import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_context import get_audit_context
from app.core.pagination import PageParams, PageResult, paginated
from app.modules.audit.models import AuditLog

logger = logging.getLogger("app.audit")


async def record(
    db: AsyncSession,
    action: str,
    *,
    entity_type: str = "",
    entity_id: str | uuid.UUID = "",
    summary: str = "",
    meta: dict[str, Any] | None = None,
) -> AuditLog:
    """Stage one audit row. The caller commits.

    Staged rather than committed here so the event and the change it describes
    land in the same transaction: an audit entry for a role change that then
    rolled back would be a lie.
    """
    context = get_audit_context()
    entry = AuditLog(
        actor_id=context.actor_id,
        actor_email=context.actor_email or "",
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        summary=summary,
        ip_address=context.ip,
        user_agent=(context.user_agent or "")[:400] or None,
        meta=meta,
    )
    db.add(entry)
    return entry


async def list_logs(
    db: AsyncSession,
    params: PageParams,
    *,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> PageResult[AuditLog]:
    statement = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        # Prefix match, not equality. Actions are namespaced (`auth.login`,
        # `admin.user_created`), and the question an operator actually asks is
        # "show me everything auth did" — an exact-match filter answers that
        # with nothing and looks broken.
        statement = statement.where(AuditLog.action.startswith(action))
    if actor_id:
        statement = statement.where(AuditLog.actor_id == actor_id)
    if entity_type:
        statement = statement.where(AuditLog.entity_type == entity_type)
    if entity_id:
        statement = statement.where(AuditLog.entity_id == entity_id)
    return await paginated(db, statement, params)
