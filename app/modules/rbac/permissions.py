"""What each platform role may do, as data.

A table rather than scattered `if user.role == "ADMIN"` checks: the admin panel
can render it, a test can assert on it, and adding a capability is one line in
one place instead of a grep across the routers.

Per-**project** access is a different axis and is not modelled here — see
`ProjectRole` and `app.modules.projects.access`. A platform ADMIN can administer
accounts; that does not make them a member of anyone's project, and this file
must never be read as if it did.
"""

from app.core.constants import GlobalRole


class Permission:
    # accounts
    USERS_READ = "users.read"
    USERS_CREATE = "users.create"
    USERS_UPDATE = "users.update"
    USERS_DELETE = "users.delete"
    USERS_IMPERSONATE_ROLE = "users.set_role"
    # platform
    PROJECTS_READ_ALL = "projects.read_all"
    PROJECTS_DELETE_ANY = "projects.delete_any"
    AUDIT_READ = "audit.read"
    STATS_READ = "stats.read"


_MEMBER: frozenset[str] = frozenset()

_ADMIN: frozenset[str] = frozenset(
    {
        Permission.USERS_READ,
        Permission.USERS_CREATE,
        Permission.USERS_UPDATE,
        Permission.PROJECTS_READ_ALL,
        Permission.AUDIT_READ,
        Permission.STATS_READ,
    }
)

# Everything an admin may do, plus the irreversible half: deleting accounts and
# projects, and changing what role someone holds. Splitting it this way means an
# ADMIN can run day-to-day user management without being able to promote
# themselves.
_SUPERADMIN: frozenset[str] = _ADMIN | frozenset(
    {
        Permission.USERS_DELETE,
        Permission.USERS_IMPERSONATE_ROLE,
        Permission.PROJECTS_DELETE_ANY,
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    GlobalRole.MEMBER: _MEMBER,
    GlobalRole.ADMIN: _ADMIN,
    GlobalRole.SUPERADMIN: _SUPERADMIN,
}

ALL_PERMISSIONS: tuple[str, ...] = tuple(
    sorted({code for codes in ROLE_PERMISSIONS.values() for code in codes})
)


def permissions_for(role: str, is_superuser: bool = False) -> frozenset[str]:
    """Everything this role may do. The break-glass flag grants all of it."""
    if is_superuser:
        return _SUPERADMIN
    return ROLE_PERMISSIONS.get(role, _MEMBER)


def role_has(role: str, code: str, is_superuser: bool = False) -> bool:
    return code in permissions_for(role, is_superuser)
