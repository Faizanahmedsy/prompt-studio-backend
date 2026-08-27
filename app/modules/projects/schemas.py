import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.constants import ActivityType, MemberStatus, ProjectRole
from app.modules.users.schemas import UserSummary

# The document is the frontend's `ProjectDoc` (types/project.ts), validated
# there by zod and versioned by `SCHEMA_VERSION`. The server stores it whole and
# checks only the parts it actually reasons about — the arrays it counts and the
# name it indexes. Re-declaring the full shape here would mean every field the
# editor gains needs a matching change on the server before it can be saved, and
# the two definitions would drift the first time that step was skipped.
DOC_ARRAY_KEYS = (
    "views",
    "flows",
    "screens",
    "edges",
    "modules",
    "moduleEdges",
    "sections",
)

# A ceiling on one document, in bytes of serialised JSON. A real 200-screen
# diagram with modules and notes measures in the low hundreds of kilobytes, so
# this is roughly twenty times the largest honest document — loose enough never
# to be hit by accident, tight enough that a client cannot push the server into
# swap by sending one enormous field. Checked here rather than as a body-size
# middleware so the error names the document instead of being an opaque 413.
MAX_DOC_BYTES = 8 * 1024 * 1024


def validate_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Structural sanity check on an incoming document.

    Not a schema check. It rejects the shapes that would break the server's own
    reasoning — a non-object document, or a `screens` value that is not a list
    for the code that counts it — and passes everything else through untouched.
    """
    if not isinstance(doc, dict):
        raise ValueError("document must be an object")
    for key in DOC_ARRAY_KEYS:
        value = doc.get(key)
        if value is not None and not isinstance(value, list):
            raise ValueError(f"`{key}` must be a list")
    size = len(json.dumps(doc, default=str).encode())
    if size > MAX_DOC_BYTES:
        raise ValueError(
            f"document is {size // 1024}KB, over the {MAX_DOC_BYTES // 1024 // 1024}MB limit"
        )
    return doc


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=4000)
    doc: dict[str, Any] = Field(default_factory=dict)
    # Bounded, or a value past int64 overflows the BIGINT column and answers 500
    # instead of 422.
    schema_version: int = Field(1, ge=1, le=2_147_483_647)
    # Addresses to share it with immediately. Saves the two-step of create-then-
    # invite, which is how every one of these projects actually starts.
    #
    # Capped to match `MemberBulkInvite`. Uncapped, one unthrottled request sent
    # a thousand invitation emails to arbitrary third parties.
    member_emails: list[EmailStr] = Field(default_factory=list, max_length=50)

    @field_validator("doc")
    @classmethod
    def _check_doc(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_doc(value)


class ProjectUpdate(BaseModel):
    """Metadata only. The document has its own endpoint because it needs the
    optimistic-concurrency check that renaming does not."""

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)
    is_archived: bool | None = None


class ProjectDocSave(BaseModel):
    doc: dict[str, Any]
    # The version the client started editing from. The server rejects the save
    # if the stored document has moved on, so a stale tab cannot silently
    # overwrite work done in another one. Omit it to force the write.
    base_version: int | None = None
    schema_version: int | None = Field(None, ge=1, le=2_147_483_647)
    # Take a named snapshot as part of this save.
    label: str | None = Field(None, max_length=200)

    @field_validator("doc")
    @classmethod
    def _check_doc(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_doc(value)


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    role: ProjectRole
    status: MemberStatus
    user: UserSummary | None = None
    invited_at: datetime | None = None
    joined_at: datetime | None = None
    last_opened_at: datetime | None = None


class MemberInvite(BaseModel):
    """Share a project with one address.

    The address does not have to belong to an account yet — that is the point.
    The row is written as INVITED and claimed when someone registers with it.
    """

    email: EmailStr
    role: ProjectRole = ProjectRole.EDITOR


class MemberBulkInvite(BaseModel):
    emails: list[EmailStr] = Field(min_length=1, max_length=50)
    role: ProjectRole = ProjectRole.EDITOR


class MemberUpdate(BaseModel):
    role: ProjectRole


class ProjectSummary(BaseModel):
    """List row. Carries no document — a projects page must not download every
    diagram in the account to render its titles."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    schema_version: int
    doc_version: int
    screen_count: int
    module_count: int
    is_archived: bool
    owner: UserSummary | None = None
    member_count: int = 0
    my_role: ProjectRole | None = None
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime | None = None


class ProjectDetail(ProjectSummary):
    doc: dict[str, Any]
    members: list[MemberRead] = []
    # Present so the owner's share dialog knows whether the link is live
    # without a second request. Only ever populated for someone who already has
    # access to the project — see `serializers.to_detail`.
    public_token: str | None = None


class PublicLinkRead(BaseModel):
    """The state of one project's public link, for its owner."""

    enabled: bool
    token: str | None = None
    enabled_at: datetime | None = None


class PublicProjectRead(BaseModel):
    """What an anonymous reader gets.

    Deliberately not `ProjectDetail`. A public link shares a *diagram*, not the
    team behind it: no members, no owner, no activity, no comments, no counts
    that would let someone infer how the account is used. `id` is the server
    id and is safe to expose — it is useless without membership — but it lets
    a member who opens the public link be moved onto their own live copy.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    schema_version: int
    doc_version: int
    doc: dict[str, Any]
    updated_at: datetime


class VersionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    doc_version: int
    is_auto: bool
    created_at: datetime
    created_by: uuid.UUID | None = None


class VersionDetail(VersionSummary):
    doc: dict[str, Any]


class VersionCreate(BaseModel):
    label: str = Field("", max_length=200)


class ActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: ActivityType
    summary: str
    actor_id: uuid.UUID | None = None
    actor_email: str = ""
    meta: dict[str, Any] | None = None
    created_at: datetime


# What a comment can be pinned to. A closed vocabulary on the way *out* as well
# as in: typed as a bare `str` on the read model, a client cannot switch on it
# without a fallback branch for a value the API will never send.
CommentTarget = Literal["canvas", "screen", "module", "edge"]


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    target_kind: CommentTarget = "canvas"
    target_key: str = Field("", max_length=120)


class CommentUpdate(BaseModel):
    body: str | None = Field(None, min_length=1, max_length=4000)
    resolved: bool | None = None


class CommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    body: str
    target_kind: CommentTarget
    target_key: str
    author_id: uuid.UUID | None = None
    author_email: str = ""
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PresenceMember(BaseModel):
    """Someone with the project open right now."""

    user_id: uuid.UUID
    email: EmailStr
    name: str
    accent_color: str
    role: ProjectRole
    connections: int = 1
