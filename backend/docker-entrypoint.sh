#!/bin/sh
# Turns the shared backend image into whichever process the container is for.
#
# Anything unrecognised is executed as-is, so `docker compose run api python -m
# app.cli seed` or a shell still work without a second image.
set -eu

CELERY_APP="app.worker.celery_app:celery_app"

case "${1:-api}" in
  api)
    shift
    # One worker per container: scaling is the orchestrator's job, and a
    # single process keeps the connection-pool arithmetic honest.
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-1}" \
      --proxy-headers \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
      "$@"
    ;;
  worker)
    shift
    exec celery -A "$CELERY_APP" worker \
      --loglevel "${CELERY_LOG_LEVEL:-info}" \
      --concurrency "${CELERY_CONCURRENCY:-2}" \
      "$@"
    ;;
  beat)
    shift
    # The schedule file must live somewhere writable by the container user.
    exec celery -A "$CELERY_APP" beat \
      --loglevel "${CELERY_LOG_LEVEL:-info}" \
      --schedule "${CELERY_BEAT_SCHEDULE_FILE:-/tmp/celerybeat-schedule}" \
      "$@"
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  seed)
    # Migrate first: seeding writes into tables the migration creates.
    alembic upgrade head
    exec python -m app.cli seed
    ;;
  *)
    exec "$@"
    ;;
esac
