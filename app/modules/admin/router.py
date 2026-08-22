import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.constants import GlobalRole
from app.core.envelope import set_response_message
from app.core.messages import ResponseMessage
from app.core.pagination import Page, PageQuery
from app.modules.admin import service
from app.modules.admin.schemas import (
    AdminProjectRow,
    PasswordIssued,
    PlatformStats,
    RoleDescription,
)
from app.modules.audit import service as audit_service
from app.modules.audit.schemas import AuditLogRead
from app.modules.auth.dependencies import (
    CurrentAdmin,
    CurrentSuperadmin,
    DbSession,
    require_permission,
)
from app.modules.rbac.permissions import ROLE_PERMISSIONS, Permission
from app.modules.users.schemas import AdminUserCreate, AdminUserUpdate, UserListItem

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/stats", response_model=PlatformStats)
async def platform_stats(db: DbSession, _: CurrentAdmin) -> PlatformStats:
    """Dashboard numbers for the admin panel."""
    return await service.stats(db)


@router.get("/roles", response_model=list[RoleDescription])
async def list_roles(_: CurrentAdmin) -> list[RoleDescription]:
    """The role/permission matrix, so the panel can render it instead of
    hardcoding a second copy that drifts from the server's."""
    return [
        RoleDescription(role=GlobalRole(role), permissions=sorted(codes))
        for role, codes in ROLE_PERMISSIONS.items()
    ]


# ── user management ──────────────────────────────────────────────────────────


@router.get("/users", response_model=Page[UserListItem])
async def list_users(
    db: DbSession,
    params: PageQuery,
    _: CurrentAdmin,
    search: str | None = Query(None, description="Match on email or name"),
    role: GlobalRole | None = None,
    is_active: bool | None = None,
    sort: str = Query("recent", pattern="^(recent|name|email|last_seen)$"),
) -> Page[UserListItem]:
    return await service.list_users(
        db, params, search=search, role=role, is_active=is_active, sort=sort
    )


@router.post("/users", response_model=PasswordIssued, status_code=status.HTTP_201_CREATED)
async def create_user(
    data: AdminUserCreate, db: DbSession, actor: CurrentAdmin, request: Request
) -> PasswordIssued:
    """Create an account.

    Leave `password` out and one is generated: it comes back **once** in the
    response and is emailed, and the account must replace it before it can use
    anything else.
    """
    result = await service.create_user(db, actor, data)
    set_response_message(request, ResponseMessage.USER_CREATED)
    return result


@router.patch("/users/{user_id}", response_model=PasswordIssued)
async def update_user(
    user_id: uuid.UUID,
    data: AdminUserUpdate,
    db: DbSession,
    actor: CurrentAdmin,
    request: Request,
) -> PasswordIssued:
    """Rename, change role, activate/deactivate, or issue a new password.

    Deactivating or re-issuing a password also ends every session that account
    has open — otherwise the change would not take effect until the token
    happened to expire.
    """
    result = await service.update_user(db, actor, user_id, data)
    set_response_message(request, ResponseMessage.USER_UPDATED)
    return result


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: uuid.UUID, db: DbSession, actor: CurrentSuperadmin, request: Request
) -> None:
    await service.delete_user(db, actor, user_id)
    set_response_message(request, ResponseMessage.USER_DELETED)


# ── projects ─────────────────────────────────────────────────────────────────


@router.get("/projects", response_model=Page[AdminProjectRow])
async def list_projects(
    db: DbSession,
    params: PageQuery,
    _: CurrentAdmin,
    search: str | None = None,
    include_deleted: bool = False,
) -> Page[AdminProjectRow]:
    """Every project on the platform — **metadata only**.

    The document is never exposed here. A project is visible to the addresses
    on it and to nobody else, admins included; this endpoint answers "does it
    exist, how big is it, who owns it", which is what operating the platform
    actually needs.
    """
    return await service.list_projects(db, params, search=search, include_deleted=include_deleted)


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: uuid.UUID, db: DbSession, actor: CurrentSuperadmin, request: Request
) -> None:
    await service.delete_project(db, actor, project_id)
    set_response_message(request, ResponseMessage.PROJECT_DELETED)


# ── audit trail ──────────────────────────────────────────────────────────────


@router.get(
    "/audit",
    response_model=Page[AuditLogRead],
    dependencies=[Depends(require_permission(Permission.AUDIT_READ))],
)
async def list_audit(
    db: DbSession,
    params: PageQuery,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> Page[AuditLogRead]:
    page = await audit_service.list_logs(
        db, params, action=action, actor_id=actor_id, entity_type=entity_type, entity_id=entity_id
    )
    return Page.build(
        [AuditLogRead.model_validate(item) for item in page.items], page.total, params
    )
