"""Routing a queued reminder to a transport that can actually deliver it.

Email is the only channel with a real transport today. WhatsApp and SMS need
provider accounts (WhatsApp Business API, MSG91) that a firm configures at
deploy time, so rather than silently marking those reminders "sent", this
module falls back to email whenever the client has an address — a document
chase that reaches the client by mail beats one that reaches nobody at all.
The substitution is recorded on the reminder, so the audit trail never claims a
WhatsApp message went out when it did not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models.base import ReminderChannel
from app.models.firm import Firm
from app.models.reminder import Reminder
from app.services import mailer

logger = logging.getLogger(__name__)

# Channels with a working transport. Add to this as adapters land.
IMPLEMENTED_CHANNELS = frozenset({ReminderChannel.EMAIL})


@dataclass(frozen=True)
class DeliveryOutcome:
    delivered: bool
    transport: str
    detail: str = ""


class NoTransport(Exception):
    """No adapter can deliver this reminder, and no fallback was possible."""


def resolve_email_recipient(reminder: Reminder) -> str | None:
    """The address to mail, whatever channel the reminder was queued for."""
    if reminder.channel is ReminderChannel.EMAIL and reminder.recipient:
        return reminder.recipient
    client = reminder.client
    return client.email if client is not None else None


def deliver(reminder: Reminder, firm: Firm | None = None) -> DeliveryOutcome:
    """Send ``reminder``, raising :class:`mailer.DeliveryError` on failure.

    ``firm`` sets the reply-to and sender name so a client replying to a
    reminder reaches their CA rather than a no-reply mailbox.
    """
    recipient = resolve_email_recipient(reminder)
    if not recipient:
        raise NoTransport(
            f"No email address on file to deliver this {reminder.channel.value} reminder"
        )

    substituted = reminder.channel not in IMPLEMENTED_CHANNELS
    result = mailer.send(
        to=recipient,
        subject=reminder.subject or "A message from your Chartered Accountant",
        body=reminder.body or "",
        reply_to=firm.email if firm else None,
        from_name=firm.name if firm else None,
    )

    # Keep the record straight about what actually carried the message.
    extra = dict(reminder.extra or {})
    extra["transport"] = result.transport
    if substituted:
        extra["substituted_channel"] = reminder.channel.value
        logger.info(
            "Delivered a %s reminder over email to %s (no %s adapter configured)",
            reminder.channel.value,
            recipient,
            reminder.channel.value,
        )
    reminder.extra = extra

    detail = f"Delivered to {recipient} via {result.transport}"
    if substituted:
        detail += f" (queued as {reminder.channel.value})"
    return DeliveryOutcome(delivered=True, transport=result.transport, detail=detail)
