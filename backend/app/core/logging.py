"""Structured logging.

Every log line carries the request id of the request that produced it, so a
500 reported by a user can be traced through the middleware, the route and the
service layer with one grep. The id lives in a ``ContextVar``, which survives
across ``await`` points and threadpool hops within a request but is never
shared between concurrent requests.

Two formats: human-readable for a terminal, one JSON object per line when
``LOG_JSON=true`` for anything that ships logs to a collector.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.config import settings

# Set by RequestContextMiddleware; empty outside a request (CLI, worker).
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
# Who is making the request, once authentication has resolved it.
actor_var: ContextVar[str] = ContextVar("actor", default="")

# LogRecord attributes that are not extras — everything else on a record was
# put there by a caller passing ``extra=`` and belongs in the payload.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
    | {"asctime", "message", "taskName"}
)


class RequestContextFilter(logging.Filter):
    """Attach the ambient request id and actor to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.actor = actor_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any ``extra=`` fields merged in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        actor = getattr(record, "actor", "-")
        if actor and actor != "-":
            payload["actor"] = actor
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key in payload or key in ("request_id", "actor"):
                continue
            payload[key] = value if _is_jsonable(value) else repr(value)

        return json.dumps(payload, default=str, ensure_ascii=False)


def _is_jsonable(value: Any) -> bool:
    return isinstance(value, str | int | float | bool | list | dict | type(None))


TEXT_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"


def configure_logging() -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestContextFilter())
    handler.setFormatter(
        JsonFormatter() if settings.log_json else logging.Formatter(TEXT_FORMAT)
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.effective_log_level)

    # Uvicorn installs its own coloured handlers; drop them so every line goes
    # through ours and picks up the request id.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # uvicorn.access duplicates our own access log, with less detail.
    logging.getLogger("uvicorn.access").disabled = True
    # SQLAlchemy's engine logger is deafening at DEBUG; opt in via DB_ECHO.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def get_request_id() -> str:
    return request_id_var.get()
