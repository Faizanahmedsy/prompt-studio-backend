import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# A ceiling on one artifact, in bytes. The largest honest knowledge base from a
# real prototype is a few hundred kilobytes; this is loose enough never to be
# hit by an agent writing markdown and tight enough that a runaway loop cannot
# push the row into the megabytes-per-request range.
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024

# One run is a whole product's worth of questions. The biggest trial produced
# 178; the cap is two orders of magnitude above that, and exists so a malformed
# client cannot ask for a single transaction with a million INSERTs in it.
MAX_RUN_ITEMS = 5000


class ItemIn(BaseModel):
    """One question as the generator writes it."""

    key: str = Field(min_length=1, max_length=40)
    family: str = Field(min_length=1, max_length=12)
    kind: str = Field("", max_length=40)
    severity: str | None = Field(None, max_length=20)
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    modules: list[str] = Field(default_factory=list)
    options: list[dict[str, Any]] = Field(default_factory=list)
    proposed: str | None = None
    proposed_key: str | None = Field(None, max_length=40)
    needs_user: bool = False
    depends_on: list[str] = Field(default_factory=list)
    position: int = 0


class RunCreate(BaseModel):
    label: str = Field("", max_length=200)
    source: str = Field("", max_length=300)
    items: list[ItemIn] = Field(default_factory=list, max_length=MAX_RUN_ITEMS)


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    source: str
    created_at: datetime
    updated_at: datetime


class ModuleProgress(BaseModel):
    total: int = 0
    answered: int = 0


class RunProgress(BaseModel):
    total: int = 0
    answered: int = 0
    needs_user: int = 0
    needs_user_answered: int = 0
    modules: dict[str, ModuleProgress] = Field(default_factory=dict)


class RunDetail(RunSummary):
    progress: RunProgress
    # The gate: every item has an answer. Nothing else closes a run — there is
    # no status column to forget to set.
    done: bool


class AnswerIn(BaseModel):
    decision: str = Field(min_length=1)
    choice_key: str | None = Field(None, max_length=40)
    note: str = ""


class AnswerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    decision: str
    choice_key: str | None = None
    note: str = ""
    created_at: datetime


class ItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    family: str
    kind: str
    severity: str | None = None
    title: str
    body: str
    modules: list[Any]
    options: list[Any]
    proposed: str | None = None
    proposed_key: str | None = None
    needs_user: bool
    depends_on: list[Any]
    position: int
    # The current decision — the newest of however many were written.
    answer: AnswerRead | None = None


class AnswerLine(BaseModel):
    """The whole run's decisions, keyed the way the generator names them."""

    key: str
    family: str
    decision: str
    choice_key: str | None = None
    note: str = ""


class BulkAccept(BaseModel):
    item_ids: list[uuid.UUID] = Field(max_length=MAX_RUN_ITEMS)


class BulkResult(BaseModel):
    accepted: int
    # The ids that had nothing to accept: no `proposed` value on the item.
    skipped: list[uuid.UUID] = Field(default_factory=list)


class ArtifactUpsert(BaseModel):
    kind: str = Field("md", max_length=20)
    body: str


class ArtifactSummary(BaseModel):
    """The listing shape — deliberately without bodies, so listing a project's
    files does not download every one of them."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str
    sha: str
    updated_at: datetime


class ArtifactRead(ArtifactSummary):
    body: str
