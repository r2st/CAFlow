"""Celery application and beat schedule."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "caflow",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
)

celery_app.conf.beat_schedule = {
    # Keep the rolling compliance window topped up.
    "generate-compliance-items-nightly": {
        "task": "caflow.generate_compliance_items",
        "schedule": crontab(hour=1, minute=30),
    },
    # Queue reminders for deadlines coming up.
    "schedule-compliance-reminders": {
        "task": "caflow.schedule_compliance_reminders",
        "schedule": crontab(hour=7, minute=0),
    },
    # Deliver anything due to go out.
    "dispatch-due-reminders": {
        "task": "caflow.dispatch_due_reminders",
        "schedule": crontab(minute="*/15"),
    },
}
