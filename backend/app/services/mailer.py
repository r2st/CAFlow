"""SMTP transport for outbound client mail.

Named ``mailer`` rather than ``email`` so nothing shadows the standard library
package this module is built on.

With no ``smtp_host`` configured the sender falls back to logging the message.
Development and CI then exercise the whole dispatch path — recipient
resolution, body rendering, status transitions — without needing a mail server,
and without silently pretending a real send happened: the return value says
which transport ran.
"""

from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Delivery failed. ``permanent`` decides whether retrying is worthwhile."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True)
class SendResult:
    recipient: str
    transport: str  # "smtp" or "log"


def is_configured() -> bool:
    return bool(settings.smtp_host)


def _looks_like_an_address(value: str) -> bool:
    """Cheap sanity check — the SMTP server is the real authority.

    Internal whitespace is refused outright rather than folded the way a
    subject is: an address with a line break in the middle of it is not an
    address a firm mistyped, it is someone writing a second header.
    """
    if any(char.isspace() for char in value):
        return False
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")


# The line-breaking whitespace a header cannot carry. Tab is in here too: it is
# legal inside a folded header but arrives from the same paste, and a subject
# line is not the place to preserve indentation.
_HEADER_BREAKS = re.compile(r"[\r\n\t\v\f\x1c-\x1f  ]+")


def header_safe(value: str) -> str:
    """``value`` as something that can be one header line.

    A header *is* one line, so a line break in a subject or a display name has
    no meaning to express — and the standard library refuses to encode one
    rather than silently truncating, which is right of it. Inbound sanitising
    keeps newlines on purpose (a note or an address is genuinely multiline), so
    they reach here whenever a practitioner pastes a subject out of their mail
    client and whenever a firm's own name was entered across two lines.

    Folded to a space rather than refused. The alternative is to fail the
    message permanently, which costs the client the reminder and the firm a
    row nobody looks at, over a line break the sender did not know was there.
    """
    return _HEADER_BREAKS.sub(" ", value).strip()


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
    from_name: str | None = None,
) -> EmailMessage:
    """A plain-text message. ``reply_to`` is the firm, so replies reach the CA.

    Every header value goes through :func:`header_safe` first; the body does
    not, because it is content rather than a header and a reminder that reads
    as one paragraph instead of five would be the worse bug.
    """
    message = EmailMessage()
    display_name = header_safe(from_name or settings.email_from_name)
    local, _, domain = settings.email_from_address.partition("@")
    message["From"] = str(Address(display_name, local, domain))
    message["To"] = to
    message["Subject"] = header_safe(subject)
    if reply_to:
        message["Reply-To"] = header_safe(reply_to)
    message.set_content(body)
    return message


def send(
    *,
    to: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
    from_name: str | None = None,
) -> SendResult:
    """Send one message, raising :class:`DeliveryError` if it cannot go out."""
    recipient = (to or "").strip()
    if not recipient:
        raise DeliveryError("No recipient address", permanent=True)
    if not _looks_like_an_address(recipient):
        raise DeliveryError(f"{recipient!r} is not a usable email address", permanent=True)

    # A message that cannot be assembled is a message that certainly did not
    # go out, which is a delivery failure and a permanent one — nothing about
    # a later attempt would assemble it either.
    #
    # It has to be *reported* as one. The dispatcher treats an unexpected
    # exception as "we do not know whether that went out", rolls back and
    # re-raises, which stops the run — and the offending reminder is still
    # SCHEDULED with the oldest time, so it is the first row the next run
    # claims, and the one after that. A single subject with a line break in it
    # therefore stopped every reminder for every firm on the deployment, for
    # good, and left nothing behind saying why. ``header_safe`` above is what
    # keeps the ordinary version of that from arising at all; this is the
    # backstop for the rest.
    try:
        message = build_message(
            to=recipient,
            subject=subject,
            body=body,
            reply_to=reply_to,
            from_name=from_name,
        )
    except ValueError as exc:
        raise DeliveryError(f"This message could not be assembled: {exc}", permanent=True) from exc

    if not is_configured():
        logger.info(
            "SMTP is not configured — logging mail instead of sending.\n"
            "To: %s\nSubject: %s\n%s",
            recipient,
            subject,
            body,
        )
        return SendResult(recipient=recipient, transport="log")

    try:
        with _connect() as smtp:
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except smtplib.SMTPRecipientsRefused as exc:
        # The address itself is wrong; retrying will refuse again.
        raise DeliveryError(f"Recipient refused: {recipient}", permanent=True) from exc
    except smtplib.SMTPAuthenticationError as exc:
        # Bad credentials are an operator problem, not a per-message one, but
        # retrying every reminder against them just burns attempts.
        raise DeliveryError("SMTP authentication failed", permanent=True) from exc
    except (smtplib.SMTPException, OSError) as exc:
        # Connection refused, greylisting, timeouts — worth another try.
        raise DeliveryError(f"SMTP delivery failed: {exc}") from exc

    return SendResult(recipient=recipient, transport="smtp")


def _connect() -> smtplib.SMTP:
    if settings.smtp_use_ssl:
        return smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
        )
    smtp = smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
    )
    if settings.smtp_use_tls:
        smtp.starttls()
    return smtp
