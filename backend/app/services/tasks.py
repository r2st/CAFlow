"""Turning compliance deadlines into assignable work, and reading team load.

A compliance item is a statutory obligation; a task is the human job of
discharging it. One task per compliance item, created once the deadline comes
inside the planning horizon and assigned to whoever owns the filing (falling
back to whoever owns the client).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.base import ComplianceStatus, TaskPriority, TaskStatus
from app.models.compliance import ComplianceItem
from app.models.firm import Practitioner
from app.models.task import Task

OPEN_COMPLIANCE_STATUSES = (ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS)
OPEN_TASK_STATUSES = (
    TaskStatus.TODO,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
    TaskStatus.REVIEW,
)
CLOSED_TASK_STATUSES = (TaskStatus.DONE, TaskStatus.CANCELLED)

# How far ahead we materialise tasks. Beyond this the calendar is the plan;
# a task list stretching months out is noise, not a to-do list.
DEFAULT_HORIZON_DAYS = 21


def derive_priority(days_until_due: int) -> TaskPriority:
    """Urgency straight off the clock — overdue and same-week work floats up."""
    if days_until_due < 0:
        return TaskPriority.URGENT
    if days_until_due <= 3:
        return TaskPriority.HIGH
    if days_until_due <= 10:
        return TaskPriority.NORMAL
    return TaskPriority.LOW


def task_title(item: ComplianceItem) -> str:
    client_name = item.client.name if item.client else "Client"
    return f"{item.compliance_type.name} — {item.period_label} ({client_name})"


def create_tasks_for_due_items(
    db: Session,
    firm_id: uuid.UUID,
    *,
    today: date | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    created_by: Practitioner | None = None,
) -> list[Task]:
    """Create the missing tasks for filings falling due inside the horizon.

    Idempotent: a compliance item that already has any task — open or closed —
    is skipped, so a task a manager deliberately cancelled does not reappear
    the next night.
    """
    today = today or date.today()
    horizon = today + timedelta(days=horizon_days)

    items = list(
        db.scalars(
            select(ComplianceItem)
            .options(
                selectinload(ComplianceItem.client),
                selectinload(ComplianceItem.compliance_type),
            )
            .where(
                ComplianceItem.firm_id == firm_id,
                ComplianceItem.status.in_(OPEN_COMPLIANCE_STATUSES),
                ComplianceItem.due_date <= horizon,
            )
            .order_by(ComplianceItem.due_date)
        ).all()
    )
    if not items:
        return []

    already_tracked = set(
        db.scalars(
            select(Task.compliance_item_id).where(
                Task.compliance_item_id.in_([item.id for item in items])
            )
        ).all()
    )

    created: list[Task] = []
    for item in items:
        if item.id in already_tracked:
            continue
        if item.client is None or not item.client.is_active:
            continue

        assignee_id = item.assigned_practitioner_id or item.client.assigned_practitioner_id
        task = Task(
            firm_id=firm_id,
            client_id=item.client_id,
            compliance_item_id=item.id,
            assignee_id=assignee_id,
            created_by_id=created_by.id if created_by else None,
            title=task_title(item),
            description=(
                f"Prepare and file {item.compliance_type.name} "
                f"({item.compliance_type.form_number or item.compliance_type.code}) "
                f"for {item.period_label}. Statutory due date "
                f"{item.due_date:%d %b %Y}."
            ),
            status=TaskStatus.TODO,
            priority=derive_priority((item.due_date - today).days),
            due_date=item.due_date,
        )
        db.add(task)
        created.append(task)
        already_tracked.add(item.id)

    if created:
        db.flush()
    return created


# ---------------------------------------------------------------- workload --


@dataclass
class WorkloadRow:
    practitioner_id: uuid.UUID | None
    practitioner_name: str
    role: str | None = None
    open_tasks: int = 0
    overdue: int = 0
    due_this_week: int = 0
    in_progress: int = 0
    blocked: int = 0
    completed_this_month: int = 0
    estimated_minutes: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


def workload(
    db: Session, firm_id: uuid.UUID, *, today: date | None = None
) -> list[WorkloadRow]:
    """Per-practitioner load, including an "Unassigned" row when work is loose."""
    today = today or date.today()
    week_end = today + timedelta(days=7)
    month_start = date(today.year, today.month, 1)

    practitioners = list(
        db.scalars(
            select(Practitioner)
            .where(Practitioner.firm_id == firm_id, Practitioner.is_active.is_(True))
            .order_by(Practitioner.full_name)
        ).all()
    )
    rows: dict[uuid.UUID | None, WorkloadRow] = {
        p.id: WorkloadRow(
            practitioner_id=p.id, practitioner_name=p.full_name, role=p.role.value
        )
        for p in practitioners
    }
    rows[None] = WorkloadRow(practitioner_id=None, practitioner_name="Unassigned")

    tasks = db.scalars(select(Task).where(Task.firm_id == firm_id)).all()
    for task in tasks:
        row = rows.get(task.assignee_id)
        if row is None:  # assignee left the firm / was deactivated
            row = rows[None]

        row.by_status[task.status.value] = row.by_status.get(task.status.value, 0) + 1

        if task.status in CLOSED_TASK_STATUSES:
            if (
                task.status == TaskStatus.DONE
                and task.completed_at is not None
                and task.completed_at.date() >= month_start
            ):
                row.completed_this_month += 1
            continue

        row.open_tasks += 1
        row.estimated_minutes += task.estimated_minutes or 0
        if task.status == TaskStatus.IN_PROGRESS:
            row.in_progress += 1
        if task.status == TaskStatus.BLOCKED:
            row.blocked += 1
        if task.due_date is not None:
            if task.due_date < today:
                row.overdue += 1
            elif task.due_date <= week_end:
                row.due_this_week += 1

    ordered = [rows[p.id] for p in practitioners]
    unassigned = rows[None]
    if unassigned.open_tasks or unassigned.by_status:
        ordered.append(unassigned)
    return ordered
