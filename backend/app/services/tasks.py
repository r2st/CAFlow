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
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.core import clock
from app.models.base import ComplianceStatus, TaskPriority, TaskStatus
from app.models.compliance import ComplianceItem
from app.models.firm import Practitioner
from app.models.task import Task
from app.services import firms

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

    Idempotent against a *second run*, that is, not against a concurrent one.
    Read which filings already carry a task, decide that these do not, insert
    one each — the same read-decide-write the invoice numbering and the plan
    limits both had to be ordered for, and this one had nothing ordering it.

    Compliance generation survives the same race because the database refuses
    the duplicate: ``uq_compliance_item_period`` covers (client, type, period).
    A task has no such constraint, and cannot — a practitioner may legitimately
    raise more than one against a filing by hand — so the ordering has to be
    taken here.

    Two runs inside the gap both read the same empty set and both insert.
    *Generate from filings* on the tasks screen is one, reachable by any
    manager at any moment, including twice from one double-clicked button; the
    02:00 beat sweeping every firm in a single transaction is the other, and it
    is inside that transaction for as long as the whole sweep takes. What comes
    out is the same filing twice on somebody's queue, counted twice in the
    workload view, counting down twice to one deadline — and one of the pair
    surviving every withdrawal and reinstatement that assumes there is one.
    """
    today = today or clock.today()
    horizon = today + timedelta(days=horizon_days)

    # Held before the work is read, not merely before the rows are inserted: by
    # the time of the insert the decision has already been taken, from a plain
    # SELECT that nothing ordered.
    firms.lock_firm(db, firm_id)

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


def retarget_tasks_for_item(
    db: Session,
    item: ComplianceItem,
    *,
    was_due: date,
    today: date | None = None,
) -> int:
    """Move the work raised for a filing whose deadline has just moved.

    A task carries its own copy of the statutory deadline — it is what the
    board sorts on, what the workload view counts as overdue, and what
    ``overdue_only`` filters by — and that copy was taken once, when the task
    was raised. Nothing moved it afterwards, while the filing's own due date is
    a field a practitioner is expected to edit: CBIC and CBDT extend deadlines
    routinely, and the seeded calendar carries the ordinary dates precisely so
    a firm can correct them.

    So the two drifted apart, in both directions and neither of them harmless:

    * an extension left the task counting down to the old date, going *overdue*
      in red on a day the deadline no longer falls, sorted to the top of
      somebody's board and counted against them in the workload view — the
      firm's own record of its work disagreeing with its record of the
      obligation;
    * a deadline corrected *earlier* is the direction that costs a client. The
      task went on showing weeks of margin against a return now due next week,
      at whatever priority that comfortable date first derived, so the one
      signal the board gives that something needs doing now was the signal it
      withheld.

    Only tasks still carrying the old statutory date, and only open ones. A
    task a practitioner dated themselves — an internal target ahead of the
    deadline, or none at all — is their plan for the work rather than a copy of
    the deadline, and is left alone; a task already done or cancelled is a
    record of what happened. Priority moves on the same terms: re-derived only
    where it is still the one the old date produced, so a manager who bumped a
    task to urgent keeps that.
    """
    if was_due == item.due_date:
        return 0

    today = today or clock.today()
    tasks = db.scalars(
        select(Task).where(
            Task.compliance_item_id == item.id,
            Task.status.in_(OPEN_TASK_STATUSES),
            Task.due_date == was_due,
        )
    ).all()
    derived_before = derive_priority((was_due - today).days)
    derived_after = derive_priority((item.due_date - today).days)
    for task in tasks:
        if task.priority == derived_before:
            task.priority = derived_after
        task.due_date = item.due_date
    if tasks:
        db.flush()
    return len(tasks)


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


def _tally(count) -> int:
    """A ``SUM(CASE …)`` that answers 0 rather than NULL for an empty group."""
    return func.coalesce(func.sum(count), 0)


def _month_start_instant(month_start: date) -> datetime:
    """Midnight IST on the first of the month, as the instant to compare against.

    ``completed_at`` is a UTC instant and the month boundary is an Indian date,
    so the two only line up once one is expressed in the other's terms. Read in
    Python this was ``clock.date_of(completed_at) >= month_start``; the same
    question asked of the database is ``completed_at >= this``, because an
    instant falls on or after an Indian date exactly when it is at or after
    midnight IST on it. Everything finished between midnight and 05:30 IST on
    the 1st belongs to the month that has just begun, and UTC still calls it
    the old one.
    """
    return datetime.combine(month_start, time.min, tzinfo=clock.IST).astimezone(UTC)


def workload(
    db: Session, firm_id: uuid.UUID, *, today: date | None = None
) -> list[WorkloadRow]:
    """Per-practitioner load, including an "Unassigned" row when work is loose.

    Counted by the database rather than by loading the firm's tasks and
    tallying them here, for the reason the dashboard was: every figure on this
    screen is an aggregate, and what used to happen on each visit was that
    every task the firm has ever had was read off disk and hydrated into a
    mapped object, to be reduced to eight integers per team member.

    That set only grows, and it grows faster than the calendar it comes from.
    A task is raised for each filing inside the planning horizon and then kept
    for good — done and cancelled tasks are the record of what was done, and
    nothing deletes them — so a practice a few years in is materialising the
    whole history of its work to answer "who is drowning this week". The
    workload view is what a manager opens to redistribute a deadline, which
    means it is opened most on exactly the mornings the database is busiest.

    Grouped by (assignee, status) because both halves of the split are read off
    the status: the closed statuses contribute only ``completed_this_month``,
    and the open ones contribute everything else. Only the two date comparisons
    need a ``CASE``, and both are the ones already being made in Python.
    """
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

    # ``case`` rather than an aggregate FILTER clause, for the reason the
    # dashboard gives: FILTER wants SQLite 3.30 and this has to render the same
    # on both backends the suite and the deploy use. A NULL date fails the
    # comparison and falls to the ``else_``, which is what the Python did.
    grouped = db.execute(
        select(
            Task.assignee_id,
            Task.status,
            func.count(Task.id),
            _tally(func.coalesce(Task.estimated_minutes, 0)),
            _tally(case((Task.due_date < today, 1), else_=0)),
            _tally(
                case(
                    ((Task.due_date >= today) & (Task.due_date <= week_end), 1),
                    else_=0,
                )
            ),
            _tally(
                case(
                    (Task.completed_at >= _month_start_instant(month_start), 1),
                    else_=0,
                )
            ),
        )
        .where(Task.firm_id == firm_id)
        .group_by(Task.assignee_id, Task.status)
    ).all()

    for assignee_id, status, count, minutes, overdue, this_week, finished in grouped:
        # An assignee who has left the firm — or was switched off — is not in
        # the map, and their work belongs in the unassigned pile, which is
        # where a manager looks for work needing an owner.
        row = rows.get(assignee_id) or rows[None]
        row.by_status[status.value] = row.by_status.get(status.value, 0) + count

        if status in CLOSED_TASK_STATUSES:
            if status == TaskStatus.DONE:
                row.completed_this_month += finished
            continue

        row.open_tasks += count
        row.estimated_minutes += minutes
        if status == TaskStatus.IN_PROGRESS:
            row.in_progress += count
        if status == TaskStatus.BLOCKED:
            row.blocked += count
        row.overdue += overdue
        row.due_this_week += this_week

    ordered = [rows[p.id] for p in practitioners]
    unassigned = rows[None]
    if unassigned.open_tasks or unassigned.by_status:
        ordered.append(unassigned)
    return ordered
