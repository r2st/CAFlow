"""Task management schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.base import TaskPriority, TaskStatus
from app.schemas.common import ORMModel


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=512)
    description: str | None = None
    client_id: uuid.UUID | None = None
    compliance_item_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    status: TaskStatus = TaskStatus.TODO
    priority: TaskPriority = TaskPriority.NORMAL
    due_date: date | None = None
    estimated_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 30)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=512)
    description: str | None = None
    assignee_id: uuid.UUID | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    due_date: date | None = None
    estimated_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 30)


class TaskOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID
    client_id: uuid.UUID | None
    compliance_item_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    created_by_id: uuid.UUID | None
    title: str
    description: str | None
    status: TaskStatus
    priority: TaskPriority
    due_date: date | None
    estimated_minutes: int | None
    completed_at: datetime | None
    created_at: datetime

    # Denormalised for the board / list views.
    client_name: str | None = None
    assignee_name: str | None = None
    compliance_type_name: str | None = None
    period_label: str | None = None
    days_remaining: int | None = None
    is_overdue: bool = False


class BulkTaskUpdate(BaseModel):
    task_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    status: TaskStatus | None = None
    assignee_id: uuid.UUID | None = None
    priority: TaskPriority | None = None


class BulkTaskUpdateResult(BaseModel):
    updated: int
    skipped: int


class TaskGenerateRequest(BaseModel):
    horizon_days: int = Field(default=21, ge=1, le=365)


class TaskGenerateResponse(BaseModel):
    created: int
    horizon_days: int
    tasks: list[TaskOut]


class WorkloadRowOut(BaseModel):
    practitioner_id: uuid.UUID | None
    practitioner_name: str
    role: str | None
    open_tasks: int
    overdue: int
    due_this_week: int
    in_progress: int
    blocked: int
    completed_this_month: int
    estimated_minutes: int
    by_status: dict[str, int]


class WorkloadResponse(BaseModel):
    as_of: date
    rows: list[WorkloadRowOut]
    unassigned_open: int
    total_open: int
