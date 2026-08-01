"""Task management: assignment, progress tracking and the team workload view."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentPractitioner, DbSession, Manager
from app.models.base import TaskPriority, TaskStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.firm import Practitioner
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
from app.services import audit
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
    if assignee_id is None:
        return
    assignee = db.get(Practitioner, assignee_id)
    if assignee is None or assignee.firm_id != firm_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assignee does not belong to this firm",
        )


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
    today = date.today()
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
    today = date.today()
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
        changes={
            "task_ids": [str(t.id) for t in tasks],
            "status": payload.status.value if payload.status else None,
            "assignee_id": str(payload.assignee_id) if payload.assignee_id else None,
        },
    )
    db.commit()
    return BulkTaskUpdateResult(
        updated=len(tasks), skipped=len(payload.task_ids) - len(tasks)
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
    if payload.compliance_item_id is not None:
        item = db.get(ComplianceItem, payload.compliance_item_id)
        if item is None or item.firm_id != practitioner.firm_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Compliance item not found"
            )

    task = Task(
        firm_id=practitioner.firm_id,
        created_by_id=practitioner.id,
        **payload.model_dump(exclude={"status"}),
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
    return serialise(task, _client_names(db, [task]), date.today())


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
    today = date.today()
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
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(or_(Task.title.ilike(pattern), Task.description.ilike(pattern)))

    total = db.scalar(select(func.count(Task.id)).where(*filters)) or 0
    rows = list(
        db.scalars(
            _load(db)
            .where(*filters)
            # Nulls last on due date: dated work outranks undated work.
            .order_by(Task.due_date.is_(None), Task.due_date, Task.created_at)
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
    return serialise(task, _client_names(db, [task]), date.today())


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

    before = {key: getattr(task, key) for key in updates}
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
        changes=audit.diff(before, updates),
    )
    db.commit()
    db.refresh(task)
    return serialise(task, _client_names(db, [task]), date.today())


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
