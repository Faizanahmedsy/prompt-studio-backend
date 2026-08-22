from fastapi import APIRouter

from app.core.constants import GlobalRole, ProjectRole
from app.modules.auth.dependencies import CurrentUser
from app.modules.rbac.permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS

router = APIRouter(prefix="/rbac", tags=["Roles"])


@router.get("/roles")
async def list_roles(_: CurrentUser) -> dict[str, object]:
    """The vocabulary of roles, for populating pickers.

    Two independent axes, and keeping them in one response is the clearest way
    to say so: `platform` decides who administers accounts, `project` decides
    who may open and edit one diagram. Holding a platform role grants nothing
    on any project.
    """
    return {
        "platform": [
            {"key": role.value, "permissions": sorted(ROLE_PERMISSIONS[role])}
            for role in GlobalRole
        ],
        "project": [
            {"key": role.value, "rank": role.rank}
            for role in sorted(ProjectRole, key=lambda r: -r.rank)
        ],
        "permissions": list(ALL_PERMISSIONS),
    }
