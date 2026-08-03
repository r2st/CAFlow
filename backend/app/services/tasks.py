"""Turning compliance deadlines into assignable work, and reading team load.

A compliance item is a statutory obligation; a task is the human job of
discharging it. One task per compliance item, created once the deadline comes
inside the planning horizon and assigned to whoever owns the filing (falling
back to whoever owns the client).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import clock
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
    today = today or clock.today()
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

    # A filing or a client can still name someone who has since been switched
    # off; a task inheriting that name is one no one can sign in to do. It is
    # not on any active member's queue and it is not in the unassigned pile
    # either, so the deadline sits on a name nobody is watching.
    assignable = set(
        db.scalars(
            select(Practitioner.id).where(
                Practitioner.firm_id == firm_id, Practitioner.is_active.is_(True)
            )
        ).all()
    )

    created: list[Task] = []
    for item in items:
        if item.id in already_tracked:
            continue
        if item.client is None or not item.client.is_active:
            continue

        # Filing owner first, then client owner — skipping anyone switched off
        # rather than stopping at them. A member leaving should cost their
        # filings the fallback the firm already set, not send the work to
        # unassigned while an active client owner is sitting right behind it.
        assignee_id = next(
            (
                candidate
                for candidate in (
                    item.assigned_practitioner_id,
                    item.client.assigned_practitioner_id,
                )
                if candidate in assignable
            ),
            None,
        )
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


def withdraw_tasks_for_items(db: Session, item_ids: Collection[uuid.UUID]) -> int:
    """Cancel the open work raised for filings that are no longer owed.

    A task is the human job of discharging one compliance item, so closing the
    item and leaving the task is asking someone to file a return the firm has
    decided nobody owes. Off-boarding a client closed a year of filings and
    left every task standing: on a practitioner's queue, counted in the
    workload view, and going overdue one by one against deadlines belonging to
    a client the firm no longer acts for. Dropping a GST registration does the
    same on a smaller scale, and does it while the client is still on the books
    — so the stale work sits among real work rather than under a name someone
    might think to look at.

    ``withdrawn_from_status`` records what the task was, which is what makes
    this reversible; see :func:`reinstate_tasks_for_items`. Only open work is
    touched: a task already done is the record that it *was* done, and one a
    manager cancelled is their decision, not this one's to overwrite.
    """
    if not item_ids:
        return 0

    tasks = db.scalars(
        select(Task).where(
            Task.compliance_item_id.in_(item_ids),
            Task.status.in_(OPEN_TASK_STATUSES),
        )
    ).all()
    for task in tasks:
        task.withdrawn_from_status = task.status
        task.status = TaskStatus.CANCELLED
    if tasks:
        db.flush()
    return len(tasks)


def reinstate_tasks_for_items(db: Session, item_ids: Collection[uuid.UUID]) -> int:
    """Put back the work that :func:`withdraw_tasks_for_items` cancelled.

    Needed for the same reason reopening the filings is: generation skips a
    compliance item that already carries a task whatever its status, so nothing
    else would ever raise work for these again. A client taken back on would
    have their whole calendar returned and not one task against it.

    Only tasks still sitting where the withdrawal left them, and only at the
    status they held — a manager who has since cancelled or completed one keeps
    that, and merely loses the marker.
    """
    if not item_ids:
        return 0

    tasks = db.scalars(
        select(Task).where(
            Task.compliance_item_id.in_(item_ids),
            Task.withdrawn_from_status.is_not(None),
        )
    ).all()
    reinstated = 0
    for task in tasks:
        if task.status == TaskStatus.CANCELLED:
            task.status = task.withdrawn_from_status
            task.completed_at = None
            reinstated += 1
        task.withdrawn_from_status = None
    if tasks:
        db.flush()
    return reinstated


def release_open_tasks(db: Session, practitioner: Practitioner) -> int:
    """Hand a departing member's unfinished work back to the firm.

    Deactivating an account leaves whatever it was holding: the task still
    names them, they can no longer sign in to do it, and no active member's
    queue shows it. A statutory deadline does not wait for the firm to notice,
    so the open work is unassigned — which is where a manager already looks
    for work needing an owner, and which is where the workload view was
    counting it anyway.

    Only the open work. Done and cancelled tasks keep their assignee: those
    are the record of who did what, and rewriting them would lose it.
    Reactivating an account does not undo this — nothing knows what they were
    meant to still be holding, and a manager has since redistributed it.
    """
    tasks = db.scalars(
        select(Task).where(
            Task.firm_id == practitioner.firm_id,
            Task.assignee_id == practitioner.id,
            Task.status.in_(OPEN_TASK_STATUSES),
        )
    ).all()
    for task in tasks:
        task.assignee_id = None
    return len(tasks)


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
    today = today or clock.today()
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
            # ``completed_at`` is a UTC instant and ``month_start`` an Indian
            # date, so the two only line up once the instant is read in IST.
            # Everything finished between midnight and 05:30 IST on the 1st
            # belonged to the month that had just ended.
            if (
                task.status == TaskStatus.DONE
                and task.completed_at is not None
                and clock.date_of(task.completed_at) >= month_start
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
