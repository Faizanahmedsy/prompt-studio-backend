import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.envelope import set_response_message
from app.core.messages import ResponseMessage
from app.modules.auth.dependencies import CurrentUser, DbSession
from app.modules.discovery import service
from app.modules.discovery.models import DiscoveryAnswer, DiscoveryItem, DiscoveryRun
from app.modules.discovery.schemas import (
    AnswerIn,
    AnswerLine,
    AnswerRead,
    ArtifactRead,
    ArtifactSummary,
    ArtifactUpsert,
    BulkAccept,
    BulkResult,
    ItemRead,
    RunCreate,
    RunDetail,
    RunSummary,
)
from app.modules.projects.access import ProjectEditor, ProjectViewer

router = APIRouter(prefix="/projects/{project_id}/discovery", tags=["Discovery"])

# `:path` because an artifact is addressed by its path under `weave/` —
# `discovery/kb.md` — and a plain path parameter stops at the first slash.
ArtifactName = Annotated[str, Path(description="Path under weave/, e.g. discovery/kb.md")]


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(access: ProjectViewer, db: DbSession) -> list[RunSummary]:
    """Every discovery run on this project, newest first."""
    runs = await service.list_runs(db, access.project.id)
    return [RunSummary.model_validate(run) for run in runs]


@router.post("/runs", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
async def create_run(
    data: RunCreate,
    access: ProjectEditor,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> RunDetail:
    """Push a whole question tree in one call. Written by the generator."""
    run = await service.create_run(db, access.project.id, current_user, data)
    set_response_message(request, ResponseMessage.DISCOVERY_RUN_CREATED)
    return await _detail(db, run)


@router.get("/runs/{run_id}", response_model=RunDetail)
async def read_run(run_id: uuid.UUID, access: ProjectViewer, db: DbSession) -> RunDetail:
    """The run and how far through it the project is."""
    run = await service.get_run(db, access.project.id, run_id)
    return await _detail(db, run)


@router.get("/runs/{run_id}/items", response_model=list[ItemRead])
async def list_items(
    run_id: uuid.UUID,
    access: ProjectViewer,
    db: DbSession,
    module: str | None = Query(None, description="Only items touching this module"),
    family: str | None = Query(None, description="RULE, QUESTION, ISSUE or M"),
    needs_user: bool | None = Query(None),
    unanswered: bool | None = Query(None),
) -> list[ItemRead]:
    """The questions, each with its current answer.

    Unpaginated on purpose: the studio renders the tree, and a page boundary
    through a set of questions grouped by module is a page boundary through the
    thing the screen is for.
    """
    run = await service.get_run(db, access.project.id, run_id)
    items = await service.list_items(db, run.id)
    answers = await service.latest_answers(db, run.id)
    shown = service.filter_items(
        items,
        answers,
        module=module,
        family=family,
        needs_user=needs_user,
        unanswered=unanswered,
    )
    return [_item(item, answers.get(item.id)) for item in shown]


@router.post(
    "/runs/{run_id}/items/{item_id}/answer",
    response_model=ItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def answer_item(
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    data: AnswerIn,
    access: ProjectEditor,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> ItemRead:
    """Decide one question. Answering again replaces the decision."""
    run = await service.get_run(db, access.project.id, run_id)
    item, answer = await service.answer_item(db, run, current_user, item_id, data)
    set_response_message(request, ResponseMessage.DECISION_SAVED)
    return _item(item, answer)


@router.post("/runs/{run_id}/answers/bulk", response_model=BulkResult)
async def accept_proposed(
    run_id: uuid.UUID,
    data: BulkAccept,
    access: ProjectEditor,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> BulkResult:
    """Accept the generator's own answer for a list of items at once."""
    run = await service.get_run(db, access.project.id, run_id)
    accepted, skipped = await service.accept_proposed(db, run, current_user, data.item_ids)
    set_response_message(request, ResponseMessage.DEFAULTS_ACCEPTED)
    return BulkResult(accepted=accepted, skipped=skipped)


@router.get("/runs/{run_id}/answers", response_model=list[AnswerLine])
async def list_answers(run_id: uuid.UUID, access: ProjectViewer, db: DbSession) -> list[AnswerLine]:
    """Every decision, keyed the way the generator names its questions — this
    is what a generator reads back to build with."""
    run = await service.get_run(db, access.project.id, run_id)
    items = await service.list_items(db, run.id)
    answers = await service.latest_answers(db, run.id)
    return [
        AnswerLine(
            key=item.key,
            family=item.family,
            decision=answers[item.id].decision,
            choice_key=answers[item.id].choice_key,
            note=answers[item.id].note,
        )
        for item in items
        if item.id in answers
    ]


@router.get("/artifacts", response_model=list[ArtifactSummary])
async def list_artifacts(access: ProjectViewer, db: DbSession) -> list[ArtifactSummary]:
    """The generated files on this project, without their bodies."""
    artifacts = await service.list_artifacts(db, access.project.id)
    return [ArtifactSummary.model_validate(artifact) for artifact in artifacts]


@router.get("/artifacts/{name:path}", response_model=ArtifactRead)
async def read_artifact(name: ArtifactName, access: ProjectViewer, db: DbSession) -> ArtifactRead:
    artifact = await service.read_artifact(db, access.project.id, name)
    return ArtifactRead.model_validate(artifact)


@router.put("/artifacts/{name:path}", response_model=ArtifactRead)
async def save_artifact(
    name: ArtifactName,
    data: ArtifactUpsert,
    access: ProjectEditor,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
) -> ArtifactRead:
    """Write one file. One row per name per project — pushing again replaces it."""
    artifact = await service.save_artifact(db, access.project.id, current_user, name, data)
    set_response_message(request, ResponseMessage.ARTIFACT_SAVED)
    return ArtifactRead.model_validate(artifact)


def _item(item: DiscoveryItem, answer: DiscoveryAnswer | None) -> ItemRead:
    return ItemRead.model_validate(item).model_copy(
        update={"answer": AnswerRead.model_validate(answer) if answer is not None else None}
    )


async def _detail(db: AsyncSession, run: DiscoveryRun) -> RunDetail:
    items = await service.list_items(db, run.id)
    answers = await service.latest_answers(db, run.id)
    counts = service.progress(items, answers)
    return RunDetail(
        **RunSummary.model_validate(run).model_dump(),
        progress=counts,
        # Every question decided. This is the gate the studio opens on.
        done=counts.answered == counts.total,
    )
