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

# A task that has not finished in ten minutes is wedged, not busy: the longest
# real job here walks one firm's compliance window, and the dispatcher works
# through a bounded batch of sends that each carry their own SMTP timeout.
# Without a limit, a hung socket holds one of the worker's two slots until the
# process is restarted.
TASK_SOFT_TIME_LIMIT_SECONDS = 10 * 60
# The grace between the two: SoftTimeLimitExceeded is raised inside the task,
# which unwinds through the dispatcher's rollback and leaves the database
# consistent. The hard limit kills the process and skips all of that, so it is
# a backstop for a task that cannot even unwind.
TASK_TIME_LIMIT_SECONDS = 11 * 60

# How long the broker waits before handing an un-acked task to another worker.
# It must sit *above* the hard time limit. Redis defaults to an hour, which
# with ``task_acks_late`` means a worker killed mid-task leaves its work
# invisible for that long; but drop it below the hard limit and the broker
# redelivers a task that is still running, which with acks_late is two workers
# executing the same job at once.
BROKER_VISIBILITY_TIMEOUT_SECONDS = 15 * 60

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
    enable_utc=True,
    # An acknowledgement means the work is done, not that it was received. A
    # worker that dies mid-task therefore has that task redelivered — which is
    # only safe because every task here either writes nothing until it commits
    # or, in the dispatcher's case, commits each externally-visible step as it
    # happens.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=TASK_TIME_LIMIT_SECONDS,
    broker_transport_options={"visibility_timeout": BROKER_VISIBILITY_TIMEOUT_SECONDS},
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
    # Chase clients for documents their upcoming filings still need.
    "queue-document-reminders": {
        "task": "caflow.queue_document_reminders",
        "schedule": crontab(hour=7, minute=15),
    },
    # Chase unpaid invoices.
    "queue-payment-reminders": {
        "task": "caflow.queue_payment_reminders",
        "schedule": crontab(hour=7, minute=30),
    },
    # Turn approaching deadlines into assignable work.
    "generate-tasks-nightly": {
        "task": "caflow.generate_tasks",
        "schedule": crontab(hour=2, minute=0),
    },
    # Move sent invoices to overdue once their due date passes.
    "refresh-invoice-statuses": {
        "task": "caflow.refresh_invoice_statuses",
        "schedule": crontab(hour=2, minute=30),
    },
    # Deliver anything due to go out.
    "dispatch-due-reminders": {
        "task": "caflow.dispatch_due_reminders",
        "schedule": crontab(minute="*/15"),
    },
}
