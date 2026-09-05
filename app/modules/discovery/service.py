"""Rules for discovery runs, their answers, and the files generated beside them.

The one rule worth stating: an answer is never updated, only appended, and
"the answer" everywhere in this module means *the newest row for that item*.
Progress, the items list and the answers export all agree on that definition
because they all go through `_latest_answers`.
"""

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.messages import ErrorMessage
from app.modules.discovery.models import (
    DiscoveryAnswer,
    DiscoveryArtifact,
    DiscoveryItem,
    DiscoveryRun,
)
from app.modules.discovery.schemas import (
    MAX_ARTIFACT_BYTES,
    AnswerIn,
    ArtifactUpsert,
    ModuleProgress,
    RunCreate,
    RunProgress,
)
from app.modules.users.models import User

# ── runs ─────────────────────────────────────────────────────────────────────


async def list_runs(db: AsyncSession, project_id: uuid.UUID) -> list[DiscoveryRun]:
    result = await db.execute(
        select(DiscoveryRun)
        .where(DiscoveryRun.project_id == project_id)
        .order_by(DiscoveryRun.created_at.desc())
    )
    return list(result.scalars())


async def create_run(
    db: AsyncSession, project_id: uuid.UUID, user: User, data: RunCreate
) -> DiscoveryRun:
    run = DiscoveryRun(
        project_id=project_id, label=data.label, source=data.source, created_by=user.id
    )
    db.add(run)
    # Flushed, not committed: the id is a Python-side column default, so it only
    # exists once SQLAlchemy has written the row — and the items need it. One
    # transaction still, so a rejected batch leaves no half-built run behind.
    await db.flush()
    db.add_all(DiscoveryItem(run_id=run.id, **item.model_dump()) for item in data.items)
    await db.commit()
    await db.refresh(run)
    return run


async def get_run(db: AsyncSession, project_id: uuid.UUID, run_id: uuid.UUID) -> DiscoveryRun:
    run = await db.get(DiscoveryRun, run_id)
    # The project check is what stops a member of project A reading project B's
    # run by quoting its id under their own project's prefix.
    if run is None or run.project_id != project_id:
        raise NotFoundError(ErrorMessage.RUN_NOT_FOUND)
    return run


async def list_items(db: AsyncSession, run_id: uuid.UUID) -> list[DiscoveryItem]:
    result = await db.execute(
        select(DiscoveryItem)
        .where(DiscoveryItem.run_id == run_id)
        .order_by(DiscoveryItem.position.asc(), DiscoveryItem.key.asc())
    )
    return list(result.scalars())


async def latest_answers(db: AsyncSession, run_id: uuid.UUID) -> dict[uuid.UUID, DiscoveryAnswer]:
    """item id -> its newest answer.

    Read in one pass and folded in Python rather than asked for with a window
    function: a run is a few hundred rows, and the same map is wanted by every
    caller in this module.
    """
    result = await db.execute(
        select(DiscoveryAnswer)
        .where(DiscoveryAnswer.run_id == run_id)
        .order_by(DiscoveryAnswer.created_at.asc())
    )
    return {answer.item_id: answer for answer in result.scalars()}


def progress(
    items: Sequence[DiscoveryItem], answers: dict[uuid.UUID, DiscoveryAnswer]
) -> RunProgress:
    modules: dict[str, ModuleProgress] = {}
    answered = 0
    needs_user = 0
    needs_user_answered = 0
    for item in items:
        is_answered = item.id in answers
        answered += is_answered
        if item.needs_user:
            needs_user += 1
            needs_user_answered += is_answered
        for module in item.modules:
            counts = modules.setdefault(str(module), ModuleProgress())
            counts.total += 1
            counts.answered += is_answered
    return RunProgress(
        total=len(items),
        answered=answered,
        needs_user=needs_user,
        needs_user_answered=needs_user_answered,
        modules=modules,
    )


def filter_items(
    items: Sequence[DiscoveryItem],
    answers: dict[uuid.UUID, DiscoveryAnswer],
    *,
    module: str | None = None,
    family: str | None = None,
    needs_user: bool | None = None,
    unanswered: bool | None = None,
) -> list[DiscoveryItem]:
    """Narrow a run's items to what the studio is showing.

    In Python, not SQL: `modules` is a JSON list, and matching one inside it
    would be a Postgres-only containment operator in models that are meant to
    stay portable. The list is one run's worth of rows, already loaded.
    """
    return [
        item
        for item in items
        if (module is None or module in item.modules)
        and (family is None or item.family == family)
        and (needs_user is None or item.needs_user == needs_user)
        and (unanswered is None or (item.id not in answers) == unanswered)
    ]


# ── answers ──────────────────────────────────────────────────────────────────


async def _get_item(db: AsyncSession, run: DiscoveryRun, item_id: uuid.UUID) -> DiscoveryItem:
    item = await db.get(DiscoveryItem, item_id)
    if item is None or item.run_id != run.id:
        raise NotFoundError(ErrorMessage.ITEM_NOT_FOUND)
    return item


async def answer_item(
    db: AsyncSession, run: DiscoveryRun, user: User, item_id: uuid.UUID, data: AnswerIn
) -> tuple[DiscoveryItem, DiscoveryAnswer]:
    item = await _get_item(db, run, item_id)
    answer = DiscoveryAnswer(
        run_id=run.id,
        item_id=item.id,
        decision=data.decision,
        choice_key=data.choice_key,
        note=data.note,
        created_by=user.id,
    )
    db.add(answer)
    await db.commit()
    await db.refresh(answer)
    return item, answer


async def accept_proposed(
    db: AsyncSession, run: DiscoveryRun, user: User, item_ids: Sequence[uuid.UUID]
) -> tuple[int, list[uuid.UUID]]:
    """Take the generator's own answer for each listed item.

    Items with no `proposed` value are skipped and named back to the caller —
    there is nothing to accept, and inventing one would be the server answering
    a question on the user's behalf. An item that already has an answer is
    answered again: the table is append-only and the newest row wins, so this
    is a deliberate overwrite rather than a conflict.
    """
    wanted = list(dict.fromkeys(item_ids))  # de-duplicated, order kept
    result = await db.execute(
        select(DiscoveryItem).where(DiscoveryItem.run_id == run.id, DiscoveryItem.id.in_(wanted))
    )
    by_id = {item.id: item for item in result.scalars()}

    skipped: list[uuid.UUID] = []
    accepted = 0
    for item_id in wanted:
        item = by_id.get(item_id)
        if item is None or not item.proposed:
            skipped.append(item_id)
            continue
        db.add(
            DiscoveryAnswer(
                run_id=run.id,
                item_id=item.id,
                decision=item.proposed,
                choice_key=item.proposed_key,
                created_by=user.id,
            )
        )
        accepted += 1
    await db.commit()
    return accepted, skipped


# ── artifacts ────────────────────────────────────────────────────────────────


async def list_artifacts(db: AsyncSession, project_id: uuid.UUID) -> list[DiscoveryArtifact]:
    result = await db.execute(
        select(DiscoveryArtifact)
        .where(DiscoveryArtifact.project_id == project_id)
        .order_by(DiscoveryArtifact.name.asc())
    )
    return list(result.scalars())


async def _find_artifact(
    db: AsyncSession, project_id: uuid.UUID, name: str
) -> DiscoveryArtifact | None:
    result = await db.execute(
        select(DiscoveryArtifact).where(
            DiscoveryArtifact.project_id == project_id, DiscoveryArtifact.name == name
        )
    )
    return result.scalar_one_or_none()


async def read_artifact(db: AsyncSession, project_id: uuid.UUID, name: str) -> DiscoveryArtifact:
    artifact = await _find_artifact(db, project_id, name)
    if artifact is None:
        raise NotFoundError(ErrorMessage.ARTIFACT_NOT_FOUND)
    return artifact


async def save_artifact(
    db: AsyncSession, project_id: uuid.UUID, user: User, name: str, data: ArtifactUpsert
) -> DiscoveryArtifact:
    """Write one file, replacing whatever was there under that name.

    Upsert rather than insert-a-version: this is a cache of what the generator
    holds on disk, and `weaver push` runs after every change. The history that
    matters is in the answers, not in forty copies of a knowledge base.
    """
    if len(data.body.encode()) > MAX_ARTIFACT_BYTES:
        raise ValidationError(ErrorMessage.ARTIFACT_TOO_LARGE)
    sha = hashlib.sha256(data.body.encode()).hexdigest()

    artifact = await _find_artifact(db, project_id, name)
    if artifact is None:
        artifact = DiscoveryArtifact(project_id=project_id, name=name)
        db.add(artifact)
    artifact.kind = data.kind
    artifact.body = data.body
    artifact.sha = sha
    artifact.updated_by = user.id
    await db.commit()
    await db.refresh(artifact)
    return artifact
