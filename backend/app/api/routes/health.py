"""Liveness and readiness probes.

Three endpoints because orchestrators ask three different questions:

* ``/health/live`` — is the process running? Never touches a dependency, so a
  database blip cannot get every container restarted.
* ``/health/ready`` — can this instance serve traffic? Checks the database and
  the queue broker, and returns 503 when one is down so the load balancer
  takes the instance out of rotation instead of serving errors.
* ``/health`` — the shallow summary, for humans and simple uptime monitors.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Response, status

from app import __version__
from app.config import settings
from app.database import check_database, pool_status
from app.schemas.health import HealthOut, ReadinessOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["meta"])

# A probe must answer fast enough to be useful to the orchestrator.
BROKER_TIMEOUT_SECONDS = 1.0


def _check_broker() -> tuple[bool, str | None]:
    """PING the Celery broker. Reported, but not fatal for readiness."""
    if not settings.celery_broker_url.startswith(("redis://", "rediss://", "unix://")):
        return True, None
    try:
        import redis

        client = redis.from_url(
            settings.celery_broker_url,
            socket_connect_timeout=BROKER_TIMEOUT_SECONDS,
            socket_timeout=BROKER_TIMEOUT_SECONDS,
        )
        client.ping()
        client.close()
        return True, None
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
        logger.warning("Broker health check failed: %s", exc)
        return False, type(exc).__name__


@router.get(
    "/health",
    response_model=HealthOut,
    summary="Shallow health summary",
    description="Returns 200 whenever the process is serving. Does not touch dependencies.",
)
def health() -> HealthOut:
    return HealthOut(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
    )


@router.get(
    "/health/live",
    response_model=HealthOut,
    summary="Liveness probe",
    description=(
        "Returns 200 while the process is running. Deliberately free of dependency "
        "checks so a database outage does not trigger a restart loop."
    ),
)
def liveness() -> HealthOut:
    return HealthOut(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
    )


@router.get(
    "/health/ready",
    response_model=ReadinessOut,
    summary="Readiness probe",
    description=(
        "Checks the database (required) and the Celery broker (reported only). "
        "Returns 503 when the database is unreachable, so the instance is pulled "
        "out of rotation rather than serving failures."
    ),
    responses={503: {"model": ReadinessOut, "description": "A required dependency is down"}},
)
def readiness(response: Response) -> ReadinessOut:
    started = time.perf_counter()
    db_ok, db_error = check_database()
    broker_ok, broker_error = _check_broker()

    checks: dict[str, Any] = {
        "database": {"ok": db_ok, "required": True, "error": db_error, "pool": pool_status()},
        "broker": {"ok": broker_ok, "required": False, "error": broker_error},
    }
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessOut(
        status="ok" if db_ok else "unavailable",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        checks=checks,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
