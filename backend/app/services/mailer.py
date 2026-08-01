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
    """Cheap sanity check — the SMTP server is the real authority."""
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
    from_name: str | None = None,
) -> EmailMessage:
    """A plain-text message. ``reply_to`` is the firm, so replies reach the CA."""
    message = EmailMessage()
    display_name = from_name or settings.email_from_name
    local, _, domain = settings.email_from_address.partition("@")
    message["From"] = str(Address(display_name, local, domain))
    message["To"] = to
    message["Subject"] = subject
    if reply_to:
        message["Reply-To"] = reply_to
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

    message = build_message(
        to=recipient,
        subject=subject,
        body=body,
        reply_to=reply_to,
        from_name=from_name,
    )

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
