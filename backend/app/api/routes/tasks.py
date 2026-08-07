"""Task management: assignment, progress tracking and the team workload view."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentPractitioner, DbSession, Manager
from app.core import clock
from app.core.search import LIKE_ESCAPE, contains_pattern
from app.models.base import TaskPriority, TaskStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.task import Task
from app.schemas.common import Page
from app.schemas.task import (
    BulkTaskUpdate,
    BulkTaskUpdateResult,
    TaskCreate,
    TaskGenerateRequest,
    TaskGenerateResponse,
    TaskOut,
    TaskUpdate,
    WorkloadResponse,
    WorkloadRowOut,
)
from app.services import audit, firms
from app.services import tasks as task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])

OPEN_STATUSES = task_service.OPEN_TASK_STATUSES


def _load(db: Session, task_ids: list[uuid.UUID] | None = None, **filters):
    """Task query with the relations the serialiser needs already loaded."""
    stmt = select(Task).options(
        selectinload(Task.assignee),
        selectinload(Task.compliance_item).selectinload(ComplianceItem.compliance_type),
    )
    if task_ids is not None:
        stmt = stmt.where(Task.id.in_(task_ids))
    return stmt


def _get_task_or_404(db: Session, firm_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.firm_id != firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def _validate_assignee(db: Session, firm_id: uuid.UUID, assignee_id: uuid.UUID | None) -> None:
    try:
        firms.assert_assignable(db, firm_id, assignee_id)
    except firms.NotAssignable as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


def serialise(task: Task, client_names: dict[uuid.UUID, str], today: date) -> TaskOut:
    out = TaskOut.model_validate(task)
    out.client_name = client_names.get(task.client_id) if task.client_id else None
    out.assignee_name = task.assignee.full_name if task.assignee else None
    if task.compliance_item is not None:
        out.compliance_type_name = task.compliance_item.compliance_type.name
        out.period_label = task.compliance_item.period_label
    if task.due_date is not None:
        out.days_remaining = (task.due_date - today).days
        out.is_overdue = task.due_date < today and task.status in OPEN_STATUSES
    return out


def _client_names(db: Session, tasks: list[Task]) -> dict[uuid.UUID, str]:
    client_ids = {task.client_id for task in tasks if task.client_id}
    if not client_ids:
        return {}
    rows = db.execute(
        select(Client.id, Client.name).where(Client.id.in_(client_ids))
    ).all()
    return {row[0]: row[1] for row in rows}


def _apply_status(task: Task, new_status: TaskStatus) -> None:
    """Keep ``completed_at`` consistent with the status."""
    task.status = new_status
    if new_status == TaskStatus.DONE:
        task.completed_at = task.completed_at or datetime.now(UTC)
    else:
        task.completed_at = None


# ----------------------------------------------------------------- workload --
# Before /{task_id} so the literal path wins.


@router.get("/workload", response_model=WorkloadResponse, summary="Open work per team member")
def team_workload(practitioner: CurrentPractitioner, db: DbSession):
    """Open work per team member — who is drowning and who is free."""
    today = clock.today()
    rows = task_service.workload(db, practitioner.firm_id, today=today)
    return WorkloadResponse(
        as_of=today,
        rows=[
            WorkloadRowOut(
                practitioner_id=row.practitioner_id,
                practitioner_name=row.practitioner_name,
                role=row.role,
                open_tasks=row.open_tasks,
                overdue=row.overdue,
                due_this_week=row.due_this_week,
                in_progress=row.in_progress,
                blocked=row.blocked,
                completed_this_month=row.completed_this_month,
                estimated_minutes=row.estimated_minutes,
                by_status=row.by_status,
            )
            for row in rows
        ],
        unassigned_open=sum(r.open_tasks for r in rows if r.practitioner_id is None),
        total_open=sum(r.open_tasks for r in rows),
    )


@router.post(
    "/generate",
    response_model=TaskGenerateResponse,
    summary="Create tasks from upcoming filings",
)
def generate_tasks(
    payload: TaskGenerateRequest, practitioner: Manager, db: DbSession
):
    """Create tasks for every filing falling due inside the horizon."""
    today = clock.today()
    created = task_service.create_tasks_for_due_items(
        db,
        practitioner.firm_id,
        today=today,
        horizon_days=payload.horizon_days,
        created_by=practitioner,
    )
    audit.record(
        db,
        action="task.generate",
        entity_type="task",
        actor=practitioner,
        summary=f"Generated {len(created)} task(s) for the next {payload.horizon_days} days",
    )
    db.commit()

    names = _client_names(db, created)
    return TaskGenerateResponse(
        created=len(created),
        horizon_days=payload.horizon_days,
        tasks=[serialise(task, names, today) for task in created],
    )


@router.post("/bulk", response_model=BulkTaskUpdateResult, summary="Update many tasks at once")
def bulk_update_tasks(
    payload: BulkTaskUpdate, practitioner: CurrentPractitioner, db: DbSession
):
    """Reassign or advance many tasks at once."""
    if payload.status is None and payload.assignee_id is None and payload.priority is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one of: status, assignee_id, priority",
        )
    _validate_assignee(db, practitioner.firm_id, payload.assignee_id)

    tasks = list(
        db.scalars(
            select(Task).where(
                Task.firm_id == practitioner.firm_id, Task.id.in_(payload.task_ids)
            )
        ).all()
    )
    for task in tasks:
        if payload.assignee_id is not None:
            task.assignee_id = payload.assignee_id
        if payload.priority is not None:
            task.priority = payload.priority
        if payload.status is not None:
            _apply_status(task, payload.status)

    audit.record(
        db,
        action="task.bulk_update",
        entity_type="task",
        actor=practitioner,
        summary=f"Bulk-updated {len(tasks)} task(s)",
        # All three of the fields this endpoint can set, not two of them. The
        # priority was the one left out, and it is the one a bulk update is
        # most often *for* — a manager sweeping a deadline's worth of work up
        # to urgent on the morning of the twentieth. What the trail held was
        # "Bulk-updated 40 task(s)" beside a null status and a null assignee:
        # an entry recording that something happened to forty tasks and not
        # what, on the one path that changes forty rows at once.
        changes={
            "task_ids": [str(t.id) for t in tasks],
            "status": payload.status.value if payload.status else None,
            "assignee_id": str(payload.assignee_id) if payload.assignee_id else None,
            "priority": payload.priority.value if payload.priority else None,
        },
    )
    db.commit()
    # Distinct ids, for the reason ``compliance.bulk_update_status`` gives: a
    # selection built by clicking rows on a board that re-reads between clicks
    # repeats ids, and counting the repeats as skips reports work that was
    # done as work that was not.
    return BulkTaskUpdateResult(
        updated=len(tasks), skipped=len(set(payload.task_ids)) - len(tasks)
    )


# --------------------------------------------------------------------- CRUD --


@router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task",
)
def create_task(payload: TaskCreate, practitioner: CurrentPractitioner, db: DbSession):
    _validate_assignee(db, practitioner.firm_id, payload.assignee_id)

    if payload.client_id is not None:
        client = db.get(Client, payload.client_id)
        if client is None or client.firm_id != practitioner.firm_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Client not found"
            )
    fields = payload.model_dump(exclude={"status"})
    if payload.compliance_item_id is not None:
        item = db.get(ComplianceItem, payload.compliance_item_id)
        if item is None or item.firm_id != practitioner.firm_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Compliance item not found"
            )
        # A task names a client and, optionally, the filing it is the job of
        # discharging — and the two were checked against the firm but never
        # against each other. A compliance item is addressable by id alone, so
        # a stale id from the wrong screen put one client's return onto another
        # client's task, which is not merely untidy:
        #
        # * the board renders the client name from ``client_id`` and the period
        #   from the filing, so the row reads as one client's work while being
        #   another's — and whoever picks it up files against the wrong client;
        # * withdrawal follows the *filing*, so off-boarding the named client
        #   leaves this task standing on the queue while surrendering the other
        #   client's registration cancels it out from under them, in both cases
        #   for reasons nothing on the task explains.
        #
        # Generation always takes the client from the filing, so a task created
        # by hand that names only the filing does the same rather than landing
        # clientless on the board.
        if payload.client_id is None:
            fields["client_id"] = item.client_id
        elif item.client_id != payload.client_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That filing belongs to a different client",
            )

    task = Task(
        firm_id=practitioner.firm_id,
        created_by_id=practitioner.id,
        **fields,
    )
    _apply_status(task, payload.status)
    db.add(task)
    db.flush()

    audit.record(
        db,
        action="task.create",
        entity_type="task",
        entity_id=task.id,
        actor=practitioner,
        summary=f"Created task “{task.title}”",
    )
    db.commit()
    db.refresh(task)
    return serialise(task, _client_names(db, [task]), clock.today())


@router.get("", response_model=Page[TaskOut], summary="List tasks")
def list_tasks(
    practitioner: CurrentPractitioner,
    db: DbSession,
    task_status: TaskStatus | None = Query(default=None),
    priority: TaskPriority | None = Query(default=None),
    assignee_id: uuid.UUID | None = Query(default=None),
    client_id: uuid.UUID | None = Query(default=None),
    mine: bool = Query(default=False, description="Only tasks assigned to me"),
    open_only: bool = Query(default=False, description="Exclude done and cancelled"),
    overdue_only: bool = Query(default=False),
    due_before: date | None = Query(default=None),
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    today = clock.today()
    filters = [Task.firm_id == practitioner.firm_id]
    if task_status is not None:
        filters.append(Task.status == task_status)
    if priority is not None:
        filters.append(Task.priority == priority)
    if mine:
        filters.append(Task.assignee_id == practitioner.id)
    elif assignee_id is not None:
        filters.append(Task.assignee_id == assignee_id)
    if client_id is not None:
        filters.append(Task.client_id == client_id)
    if open_only:
        filters.append(Task.status.in_(OPEN_STATUSES))
    if overdue_only:
        filters.extend([Task.due_date < today, Task.status.in_(OPEN_STATUSES)])
    if due_before is not None:
        filters.append(Task.due_date <= due_before)
    # Escaped, because ``%`` and ``_`` in the caller's text are pattern syntax
    # rather than text — see :mod:`app.core.search`.
    if (pattern := contains_pattern(search)) is not None:
        filters.append(
            or_(
                Task.title.ilike(pattern, escape=LIKE_ESCAPE),
                Task.description.ilike(pattern, escape=LIKE_ESCAPE),
            )
        )

    total = db.scalar(select(func.count(Task.id)).where(*filters)) or 0
    rows = list(
        db.scalars(
            _load(db)
            .where(*filters)
            # Nulls last on due date: dated work outranks undated work.
            # ``id`` closes it, because the two columns ahead of it do not:
            # generation raises a firm's tasks in one sweep, so they share a
            # ``created_at``, and their due dates are statutory deadlines that
            # every GST client of the firm shares as well. A page boundary
            # falling inside such a run repeats rows and drops others.
            .order_by(
                Task.due_date.is_(None), Task.due_date, Task.created_at, Task.id
            )
            .limit(limit)
            .offset(offset)
        ).all()
    )
    names = _client_names(db, rows)
    return Page[TaskOut](
        items=[serialise(task, names, today) for task in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{task_id}", response_model=TaskOut, summary="A single task")
def get_task(task_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession):
    task = _get_task_or_404(db, practitioner.firm_id, task_id)
    return serialise(task, _client_names(db, [task]), clock.today())


@router.patch("/{task_id}", response_model=TaskOut, summary="Update a task")
def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    practitioner: CurrentPractitioner,
    db: DbSession,
):
    task = _get_task_or_404(db, practitioner.firm_id, task_id)
    updates = payload.model_dump(exclude_unset=True)
    if "assignee_id" in updates:
        _validate_assignee(db, practitioner.firm_id, updates["assignee_id"])

    # ``_apply_status`` stamps and clears ``completed_at``, which no payload
    # ever names — so without watching it, when a task was finished is a fact
    # the trail does not hold.
    watched = set(updates) | {"completed_at"}
    before = audit.snapshot(task, watched)
    for key, value in updates.items():
        if key == "status":
            _apply_status(task, value)
        else:
            setattr(task, key, value)

    audit.record(
        db,
        action="task.update",
        entity_type="task",
        entity_id=task.id,
        actor=practitioner,
        summary=f"Updated task “{task.title}” → {task.status.value}",
        changes=audit.diff(before, audit.snapshot(task, watched)),
    )
    db.commit()
    db.refresh(task)
    return serialise(task, _client_names(db, [task]), clock.today())


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a task")
def delete_task(task_id: uuid.UUID, practitioner: Manager, db: DbSession):
    task = _get_task_or_404(db, practitioner.firm_id, task_id)
    title = task.title
    db.delete(task)
    audit.record(
        db,
        action="task.delete",
        entity_type="task",
        entity_id=task_id,
        actor=practitioner,
        summary=f"Deleted task “{title}”",
    )
    db.commit()
