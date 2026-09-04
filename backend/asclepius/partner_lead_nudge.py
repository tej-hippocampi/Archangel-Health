"""The one reminder a health-system lead gets, and the reason it is only one.

``/partner`` used to end at a Calendly link on its own success screen. A visitor
who did not click it in that second was never heard from again: nothing had been
sent, so there was nothing to reply to and nothing to follow up. The booking
moved into an email, which is what makes this module possible at all, and the
schedule it implements is deliberately the shortest one that works:

  * on submit, the thanks letter, sent by ``routers/leads.py`` and stamped on
    the row as ``thanks_sent_at``;
  * ``PARTNER_LEAD_REMINDER_HOURS`` later, if nobody has booked, ONE reminder;
  * nothing, ever again.

There is no drip and no third letter. At this deal size a CIO who has read two
messages and booked nothing is telling us something, and the cost of getting
that wrong is not a lower conversion rate, it is a hospital's compliance office
deciding we are a vendor that mails people.

Idempotency is structural, exactly as ``onboarding_nudge`` does it and for the
same three reasons: the sweep CLAIMS a row with a conditional
``UPDATE ... WHERE reminder_sent_at IS NULL`` before it sends, so a restart
mid-sweep cannot double-send, two workers racing one row cannot both send, and a
send that fails is simply not retried. A partner receiving the same reminder
twice is worse than one who receives it zero times and still has the first
letter, with the same link in it, sitting in their inbox.

This reads the TEAM store, not the asclepius store: a lead is a landing-form
submission and has no account, no health-system row and no portal. That is the
only structural difference from the onboarding sweep, which is why this rides
the same poll loop rather than owning a timer of its own.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger("asclepius.partner_lead_nudge")

#: How long a health system sits unbooked before we ask once more. Three days:
#: long enough that the first letter has been through a weekend, short enough
#: that the meeting they filled the form in for is still the thing they meant to
#: do.
PARTNER_LEAD_REMINDER_HOURS = float(
    os.getenv("PARTNER_LEAD_REMINDER_HOURS", "72") or 72)

#: A cap per pass, so a backlog drains over several sweeps rather than trying to
#: send hundreds of emails inside one loop iteration.
_BATCH = int(os.getenv("PARTNER_LEAD_REMINDER_BATCH", "50") or 50)


def _booking_url() -> str:
    from onboarding_emails import PARTNER_BOOKING_CALENDLY  # noqa: PLC0415

    return PARTNER_BOOKING_CALENDLY


async def _send_one(row: Dict[str, Any]) -> bool:
    """Mail one reminder. False when there is nothing sendable on the row.

    The name and the organization are read back out of the submitted message by
    the router that composed the parse, rather than restated here. A reminder
    that greeted somebody differently from the letter it is reminding them of
    would read as a second, unrelated approach.
    """
    from email_utils import send_html_email  # noqa: PLC0415
    from onboarding_emails import build_hs_interest_reminder_email  # noqa: PLC0415
    from routers.leads import partner_lead_contact  # noqa: PLC0415

    email = (row.get("email") or "").strip()
    if not email:
        return False
    full_name, organization = partner_lead_contact(row.get("message") or "")
    subject = "A time to talk" + (f" about {organization}" if organization else "")
    return bool(await send_html_email(
        email, subject,
        build_hs_interest_reminder_email(
            full_name=full_name, organization=organization,
            booking_url=_booking_url())))


async def sweep(ts: Optional[Any] = None) -> Dict[str, int]:
    """Send whichever reminders are due. Never raises.

    A scheduler that can throw is a scheduler that stops, so every per-row
    failure is logged and the sweep carries on: one malformed address must not
    cost every other health system its reminder.
    """
    import asyncio  # noqa: PLC0415

    from email_utils import is_email_transport_configured  # noqa: PLC0415
    from routers.leads import HEALTH_SYSTEM_SOURCE  # noqa: PLC0415
    from team_store import get_team_store  # noqa: PLC0415

    sent = {"reminder": 0}
    if not is_email_transport_configured():
        # Nothing to do, and, critically, nothing CLAIMED. A deployment with no
        # mail transport must not silently burn every lead's one reminder.
        return sent
    ts = ts or get_team_store()

    try:
        rows = await asyncio.to_thread(
            ts.list_leads_awaiting_partner_reminder,
            source=HEALTH_SYSTEM_SOURCE,
            older_than_hours=PARTNER_LEAD_REMINDER_HOURS,
            limit=_BATCH,
        )
    except Exception:
        log.exception("[partner-lead] could not list reminder candidates")
        return sent

    for row in rows:
        try:
            # Claim FIRST. See the module docstring: a claimed-but-unsent
            # reminder costs one email, an unclaimed-but-sent one costs a
            # partner a duplicate, and there is no way to take that back.
            if not await asyncio.to_thread(ts.claim_lead_reminder, row["id"]):
                continue
            if await _send_one(row):
                sent["reminder"] += 1
        except Exception:
            log.exception("[partner-lead] reminder failed for %s", row.get("id"))

    if sent["reminder"]:
        log.info("[partner-lead] sent %s reminder(s)", sent["reminder"])
    return sent
