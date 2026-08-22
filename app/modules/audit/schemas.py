import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_email: str = ""
    action: str
    entity_type: str = ""
    entity_id: str = ""
    summary: str = ""
    ip_address: str | None = None
    user_agent: str | None = None
    meta: dict[str, Any] | None = None
    created_at: datetime
