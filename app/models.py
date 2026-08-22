"""Import every ORM model exactly once, in one place.

SQLAlchemy only knows about a table when the module declaring it has been
imported. Alembic's autogenerate compares `Base.metadata` against the database,
so a model nobody imported is a table Alembic will happily *drop*. Importing
them here — and importing this module from `main` and from `alembic/env.py` —
makes that impossible to get wrong.
"""

from app.core.base_model import Base
from app.modules.audit.models import AuditLog
from app.modules.auth.models import RevokedToken, Session
from app.modules.projects.models import (
    Project,
    ProjectActivity,
    ProjectComment,
    ProjectMember,
    ProjectVersion,
)
from app.modules.users.models import User, UserCredentials

__all__ = [
    "AuditLog",
    "Base",
    "Project",
    "ProjectActivity",
    "ProjectComment",
    "ProjectMember",
    "ProjectVersion",
    "RevokedToken",
    "Session",
    "User",
    "UserCredentials",
]
