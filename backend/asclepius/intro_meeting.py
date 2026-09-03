"""The founders' intro call: the states it can be in, and what a held one sends.

WHAT WAS MISSING. The meeting that defined the physician funnel described four
stages -- outreach, an intro call a founder takes by hand, an onboarding link
plus a one-pager, then the application -- and the product had state for all of
them except the call. There was a cold invite (``/admin/asclepius/invite``) and
there were nudges for an application already started, but nothing recorded that
we had MET somebody, and so the highest-converting email in the whole funnel,
the one that goes out ten minutes after a good conversation, was a founder
writing it themselves or nobody writing it at all.

THE THREE-STATE RULE, and it is the only piece of policy here worth arguing
about. A meeting is 'scheduled' until a person says otherwise, and 'scheduled'
means WE DO NOT KNOW WHAT HAPPENED. It does not mean held. Sending a physician
"great speaking with you, here is your link" when they never joined the call is
a worse failure than sending nothing, and it is exactly the failure you get from
a design where the absence of a no-show flag reads as attendance. So the outcome
is recorded explicitly, no sweep ever infers it, and only 'held' sends.

TRANSITIONS. An outcome can be recorded from 'scheduled'. A no-show can be
corrected to held, because "they joined nine minutes late" is a real thing that
happens and the correction should not require a second meeting row. Held cannot
be walked back to anything, and that asymmetry is deliberate: the follow-up has
already left the building, and a state machine that pretends an email can be
unsent is lying to whoever reads it next.

IDEMPOTENCY comes from two places, and neither of them is this module
remembering anything. The status transition is a guarded UPDATE in the store, so
one caller claims it. The send is ``notify_person`` into ``admin_notify_outbox``
with a key derived from the meeting id, so even a caller that races past the
first guard produces one row and one email. That is the same mechanism the
approval and rejection mails use.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Sequence, Tuple

log = logging.getLogger("asclepius.intro_meeting")

SCHEDULED = "scheduled"
HELD = "held"
NO_SHOW = "no_show"
CANCELLED = "cancelled"

#: Every state a meeting can be in. Ordered as the funnel reads.
STATES: Tuple[str, ...] = (SCHEDULED, HELD, NO_SHOW, CANCELLED)

#: The outcomes an admin may record. 'scheduled' is not among them: it is where
#: a meeting starts, not somewhere it can be put back.
OUTCOMES: Tuple[str, ...] = (HELD, NO_SHOW, CANCELLED)

#: Which statuses each outcome may be recorded from. See the module docstring
#: for why 'held' has no way out.
_ALLOWED_FROM: Dict[str, Tuple[str, ...]] = {
    HELD: (SCHEDULED, NO_SHOW),
    NO_SHOW: (SCHEDULED,),
    CANCELLED: (SCHEDULED,),
}

#: Human labels, so a console renders a state rather than a token.
STATE_LABELS: Dict[str, str] = {
    SCHEDULED: "Scheduled",
    HELD: "Held",
    NO_SHOW: "No show",
    CANCELLED: "Cancelled",
}

#: The one outcome that sends anything.
SENDS_FOLLOWUP = HELD


def allowed_from(outcome: str) -> Sequence[str]:
    """Which statuses ``outcome`` may be recorded from. Empty for a bad outcome."""
    return _ALLOWED_FROM.get(outcome, ())


def is_outcome(value: str) -> bool:
    return (value or "") in OUTCOMES


def booking_url() -> str:
    """Where a founder sends somebody to book the call.

    FROM CONFIG, NOT FROM A LITERAL IN A TEMPLATE. The meeting asked for the
    scheduling routine to live in the product rather than beside it, and the
    smallest true version of that is that the product knows the booking link and
    every surface reads the same one. ``onboarding_emails.FOUNDER_INTRO_CALENDLY``
    is the default so an unset environment keeps the behaviour the welcome email
    has always had.
    """
    from onboarding_emails import founder_intro_booking_url  # noqa: PLC0415

    return founder_intro_booking_url()


def product_base() -> str:
    """Where the BACKEND is served, which is where the one-pager route lives.

    Same resolution order as ``notifications._portal_base`` and its siblings in
    ``asclepius_verify`` and ``asclepius_provider``, and deliberately not
    ``LANDING_URL``: the wizard is served by the landing app but this PDF is a
    backend route, and mixing the two mints a link that 404s for the physician
    while looking perfectly correct to the founder who sent it.
    """
    return (os.getenv("ASCLEPIUS_PORTAL_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")


def one_pager_url(base_url: str = "") -> str:
    """The public URL of the document the follow-up carries.

    A URL and not an attachment, because the durable outbox stores a subject and
    a body and nothing else. Making it carry blobs to attach one PDF would put
    the follow-up on a different, less tested send path than every other
    lifecycle mail, and a link that always works beats an attachment that works
    until the queue is restarted.
    """
    return f"{(base_url or product_base()).rstrip('/')}/api/onboarding/asclepius/one-pager.pdf"


def queue_followup(store: Any, *, meeting: Dict[str, Any], onboarding_url: str,
                   one_pager_href: str) -> int:
    """Put the post-call follow-up in the outbox. Returns 1 when newly queued.

    Never sends inline and never raises: ``notify_person`` swallows, and a
    founder marking a meeting held must not see a 500 because SendGrid was
    having a moment. The mail is durable once it is here.
    """
    from notifications import notify_person  # noqa: PLC0415
    from onboarding_emails import build_intro_followup_email  # noqa: PLC0415

    email = (meeting.get("email") or "").strip()
    if not email or not onboarding_url:
        log.warning("intro_meeting: %s has no address or no link; queuing nothing",
                    meeting.get("meeting_id"))
        return 0
    full_name = (meeting.get("full_name") or "").strip()
    return notify_person(
        store,
        kind="intro_followup",
        to=email,
        subject="Your Archangel Health application link",
        body_html=build_intro_followup_email(
            full_name=full_name,
            onboarding_url=onboarding_url,
            one_pager_url=one_pager_href,
        ),
        # The MEETING is the event, so a second mark-held on the same meeting
        # keys to the same row and INSERT OR IGNORE drops it. Keying on the
        # email instead would silently swallow a genuine second conversation
        # months later.
        dedupe_key=f"intro_followup:{meeting.get('meeting_id')}",
    )


def view(meeting: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The shape an admin surface renders, with the policy already applied.

    Whether a button should be offered is decided here rather than by a client
    re-deriving the transition table, which is how two surfaces end up
    disagreeing about whether a no-show can still be marked held.
    """
    if not meeting:
        return {}
    status = str(meeting.get("status") or SCHEDULED)
    return {
        "meeting_id": meeting.get("meeting_id"),
        "email": meeting.get("email"),
        "full_name": meeting.get("full_name") or "",
        "specialty": meeting.get("specialty") or "",
        "organization": meeting.get("organization") or "",
        "status": status,
        "status_label": STATE_LABELS.get(status, status),
        "scheduled_at": meeting.get("scheduled_at"),
        "booking_ref": meeting.get("booking_ref") or "",
        "outcome_at": meeting.get("outcome_at"),
        "note": meeting.get("note") or "",
        "created_at": meeting.get("created_at"),
        "followup_queued_at": meeting.get("followup_queued_at"),
        "followup_sent": bool(meeting.get("followup_queued_at")),
        "one_pager_version": meeting.get("one_pager_version") or "",
        "available_outcomes": [o for o in OUTCOMES if status in allowed_from(o)],
    }
