"""Structured logging: the request-id filter and the JSON formatter.

These paths only run in a deployment (LOG_JSON=true), which is exactly why
they need covering here — a formatter that raises would take out every log
line at the moment the logs matter most.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.config import settings
from app.core.logging import (
    TEXT_FORMAT,
    JsonFormatter,
    RequestContextFilter,
    actor_var,
    configure_logging,
    get_request_id,
    request_id_var,
)


def make_record(message: str = "Client created", **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.api.routes.clients",
        level=logging.INFO,
        pathname="clients.py",
        lineno=42,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def formatted(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


@pytest.fixture
def clean_context():
    """Reset the ambient context, so one test cannot leak into the next."""
    request_token = request_id_var.set("")
    actor_token = actor_var.set("")
    try:
        yield
    finally:
        request_id_var.reset(request_token)
        actor_var.reset(actor_token)


class TestRequestContextFilter:
    def test_it_stamps_the_ambient_request_id(self, clean_context):
        request_id_var.set("req-abc123")
        record = make_record()

        assert RequestContextFilter().filter(record) is True
        assert record.request_id == "req-abc123"

    def test_it_stamps_the_actor_once_authentication_has_resolved_one(self, clean_context):
        actor_var.set("practitioner:p-1")
        record = make_record()

        RequestContextFilter().filter(record)
        assert record.actor == "practitioner:p-1"

    def test_outside_a_request_the_fields_are_placeholders(self, clean_context):
        # The CLI and the Celery worker log through the same handlers.
        record = make_record()
        RequestContextFilter().filter(record)
        assert record.request_id == "-"
        assert record.actor == "-"

    def test_it_never_drops_a_record(self, clean_context):
        # A filter returning False would silently swallow the line.
        assert RequestContextFilter().filter(make_record()) is True

    def test_get_request_id_reads_the_same_context(self, clean_context):
        request_id_var.set("req-xyz")
        assert get_request_id() == "req-xyz"


class TestJsonFormatter:
    def test_one_line_of_json_per_record(self, clean_context):
        line = JsonFormatter().format(make_record())
        assert "\n" not in line
        assert json.loads(line)["message"] == "Client created"

    def test_it_carries_the_fields_a_collector_indexes_on(self, clean_context):
        payload = formatted(make_record(request_id="req-abc123"))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.api.routes.clients"
        assert payload["request_id"] == "req-abc123"
        assert payload["ts"]

    def test_a_record_with_no_request_id_still_formats(self, clean_context):
        # Import-time and shutdown logs run outside any request.
        assert formatted(make_record())["request_id"] == "-"

    def test_the_actor_is_omitted_when_there_is_none(self, clean_context):
        assert "actor" not in formatted(make_record(actor="-"))

    def test_the_actor_is_included_when_known(self, clean_context):
        assert formatted(make_record(actor="practitioner:p-1"))["actor"] == "practitioner:p-1"

    def test_extras_are_merged_into_the_object(self, clean_context):
        payload = formatted(make_record(status_code=201, duration_ms=12.5, path="/clients"))
        assert payload["status_code"] == 201
        assert payload["duration_ms"] == 12.5
        assert payload["path"] == "/clients"

    def test_message_arguments_are_interpolated(self, clean_context):
        record = logging.LogRecord(
            name="app", level=logging.WARNING, pathname="x.py", lineno=1,
            msg="Slow request: %s took %dms", args=("/clients", 1500), exc_info=None,
        )
        assert formatted(record)["message"] == "Slow request: /clients took 1500ms"

    def test_an_unserialisable_extra_does_not_take_the_line_down(self, clean_context):
        class Opaque:
            def __repr__(self):
                return "<Opaque object>"

        payload = formatted(make_record(client=Opaque()))
        assert payload["client"] == "<Opaque object>"

    def test_an_exception_is_carried_as_a_formatted_traceback(self, clean_context):
        try:
            raise ValueError("PAN must look like AAAAA9999A")
        except ValueError:
            import sys

            record = make_record("Unhandled error")
            record.exc_info = sys.exc_info()

        payload = formatted(record)
        assert "ValueError: PAN must look like AAAAA9999A" in payload["exception"]

    def test_standard_record_attributes_are_not_leaked_as_extras(self, clean_context):
        # pathname/lineno/thread are noise in a shipped log line.
        payload = formatted(make_record())
        assert "pathname" not in payload
        assert "lineno" not in payload
        assert "args" not in payload

    def test_non_ascii_survives_intact(self, clean_context):
        # Client names and fee strings carry ₹ and Devanagari.
        payload = formatted(make_record("Invoice raised for ₹12,500 — नमस्ते"))
        assert payload["message"] == "Invoice raised for ₹12,500 — नमस्ते"


class TestConfigureLogging:
    @pytest.fixture(autouse=True)
    def restore_logging(self):
        """Put the root logger back the way the suite found it."""
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        log_json, log_level = settings.log_json, settings.log_level
        try:
            yield
        finally:
            settings.log_json, settings.log_level = log_json, log_level
            root.handlers = handlers
            root.setLevel(level)

    def test_it_installs_exactly_one_handler_however_often_it_runs(self):
        configure_logging()
        configure_logging()
        assert len(logging.getLogger().handlers) == 1

    def test_log_json_switches_the_format(self):
        settings.log_json = True
        configure_logging()
        assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    def test_the_terminal_format_shows_the_request_id(self):
        settings.log_json = False
        configure_logging()
        formatter = logging.getLogger().handlers[0].formatter
        assert not isinstance(formatter, JsonFormatter)
        assert "%(request_id)s" in TEXT_FORMAT

    def test_the_level_follows_the_setting(self):
        settings.log_level = "WARNING"
        configure_logging()
        assert logging.getLogger().level == logging.WARNING

    def test_every_line_picks_up_the_request_id_filter(self):
        configure_logging()
        handler = logging.getLogger().handlers[0]
        assert any(isinstance(f, RequestContextFilter) for f in handler.filters)

    def test_uvicorns_own_access_log_is_silenced(self):
        # It duplicates our access line, with less detail and no request id.
        configure_logging()
        assert logging.getLogger("uvicorn.access").disabled is True

    def test_uvicorn_logs_are_routed_through_our_handler(self):
        configure_logging()
        uvicorn_logger = logging.getLogger("uvicorn.error")
        assert uvicorn_logger.handlers == []
        assert uvicorn_logger.propagate is True

    def test_sqlalchemy_is_kept_quiet_unless_db_echo_is_on(self):
        configure_logging()
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
