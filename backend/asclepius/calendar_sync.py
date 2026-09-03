"""Calendar sync for the intro call: the honest half of "put it in the product".

THE ASK AND THE LINE DRAWN THROUGH IT. The founders asked for the routine that
schedules intro calls to live inside the product rather than beside it. A full
Google Calendar integration is not that request's cheapest true answer: it needs
an OAuth client, per-founder consent, refresh-token storage, a watch channel or
a poller, and a rule for deciding which of a founder's fifty weekly events is an
intro call. All of that before it can tell you anything the founder does not
already know by looking at their own calendar.

What this does instead is take the ONE signal the scheduler already produces and
that nothing in the product could see: the booking itself. The founders book
through Calendly (``FOUNDER_INTRO_CALENDLY``), Calendly posts an event when
somebody books, cancels, or is marked a no-show, and those three map exactly
onto three of the four states an intro meeting can be in. So a booking becomes a
row without anyone typing it, and a cancellation and a no-show land as
themselves.

WHAT IT DELIBERATELY CANNOT DO IS MARK A MEETING HELD. No calendar knows whether
a conversation happened; it knows an event existed. The whole safety property of
this feature is that the outcome which SENDS is asserted by a person, and an
integration that inferred attendance from a calendar entry would quietly hand
that assertion to a machine that cannot make it. A no-show, by contrast, is
recorded here because Calendly's no-show is itself a human marking one.

OFF BY DEFAULT, twice over: the flag must be on AND a signing key must be
present. An unsigned webhook endpoint that writes rows is a way for anyone on
the internet to fill an admin screen with fake physicians.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("asclepius.calendar_sync")

#: How far out of date a signed payload may be. Calendly's own guidance, and it
#: is what stops a captured request being replayed a week later.
_TOLERANCE_SECONDS = int(os.getenv("CALENDLY_WEBHOOK_TOLERANCE_SECONDS", "180") or 180)

#: The three Calendly events that mean something to this funnel.
INVITEE_CREATED = "invitee.created"
INVITEE_CANCELED = "invitee.canceled"
INVITEE_NO_SHOW = "invitee_no_show.created"
HANDLED_EVENTS = (INVITEE_CREATED, INVITEE_CANCELED, INVITEE_NO_SHOW)


class SignatureError(ValueError):
    """The payload did not come from Calendly, or came too long ago."""


def signing_key() -> str:
    return (os.getenv("CALENDLY_WEBHOOK_SIGNING_KEY") or "").strip()


def enabled() -> bool:
    """Both switches. A flag with no key would accept unsigned writes; a key
    with no flag is somebody who set the secret before they meant to turn it on."""
    raw = (os.getenv("ASCLEPIUS_CALENDAR_SYNC", "0") or "0").strip()
    return raw not in ("", "0", "false", "False") and bool(signing_key())


def _parse_signature_header(header: str) -> Tuple[str, str]:
    """``t=<unix>,v1=<hex>`` to its two parts. Raises on anything else."""
    parts: Dict[str, str] = {}
    for chunk in (header or "").split(","):
        key, _, value = chunk.strip().partition("=")
        if key and value:
            parts[key.strip()] = value.strip()
    timestamp, signature = parts.get("t", ""), parts.get("v1", "")
    if not timestamp or not signature:
        raise SignatureError("the signature header is not in Calendly's format")
    return timestamp, signature


def verify_signature(header: str, body: bytes, *, key: str = "",
                     now: Optional[float] = None) -> None:
    """Raise unless ``body`` was signed by the configured key, recently.

    Compared with ``compare_digest`` rather than ``==``: a byte-by-byte
    comparison of a signature leaks where it stopped matching, and a webhook
    endpoint is exactly the kind of thing somebody has the patience to measure.
    """
    secret = (key or signing_key()).strip()
    if not secret:
        raise SignatureError("no signing key is configured")
    timestamp, provided = _parse_signature_header(header)
    try:
        age = (now if now is not None else time.time()) - float(timestamp)
    except ValueError as exc:
        raise SignatureError("the signature timestamp is not a number") from exc
    if abs(age) > _TOLERANCE_SECONDS:
        raise SignatureError("the signature is outside the replay window")
    expected = hmac.new(secret.encode("utf-8"),
                        f"{timestamp}.".encode("utf-8") + body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("the signature does not match")


def _payload_of(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _booking_ref(payload: Dict[str, Any]) -> str:
    """What identifies this booking across the three events.

    The INVITEE uri, not the event uri: a group event has one event and several
    invitees, and keying on the event would let one person's cancellation close
    another person's meeting.
    """
    return str(payload.get("uri") or "").strip()


def _scheduled_start(payload: Dict[str, Any]) -> str:
    scheduled = payload.get("scheduled_event")
    if isinstance(scheduled, dict):
        return str(scheduled.get("start_time") or "").strip()
    return ""


def apply_event(store: Any, event: Dict[str, Any]) -> Dict[str, Any]:
    """Fold one Calendly webhook into the intro-meeting funnel.

    Returns what it did, so the endpoint can say so and a test can assert it.
    Unknown event types are ignored rather than refused: Calendly adds new ones,
    and a subscription that starts 400ing on an event we do not care about is a
    subscription Calendly eventually disables.

    NEVER MARKS HELD. See the module docstring.
    """
    from asclepius import intro_meeting as intro  # noqa: PLC0415

    kind = str(event.get("event") or "").strip()
    if kind not in HANDLED_EVENTS:
        return {"action": "ignored", "event": kind}
    payload = _payload_of(event)
    ref = _booking_ref(payload)
    if not ref:
        return {"action": "ignored", "event": kind, "reason": "no invitee uri"}

    existing = store.intro_meeting_by_booking_ref(ref)

    if kind == INVITEE_CREATED:
        if existing:
            return {"action": "already_known", "meeting_id": existing["meeting_id"]}
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            return {"action": "ignored", "event": kind, "reason": "no invitee email"}
        meeting = store.create_intro_meeting(
            email=email, full_name=str(payload.get("name") or "").strip(),
            scheduled_at=_scheduled_start(payload), booking_ref=ref,
            note="Booked through the scheduling link.", created_by="calendly")
        return {"action": "scheduled", "meeting_id": meeting["meeting_id"]}

    if not existing:
        # A cancellation or a no-show for a booking we never saw created. Worth
        # a log line and nothing else: inventing a row here would put a meeting
        # on the founders' screen that opens in an outcome nobody can act on.
        log.info("calendar_sync: %s for an unknown booking %s", kind, ref)
        return {"action": "unknown_booking", "event": kind}

    outcome = intro.CANCELLED if kind == INVITEE_CANCELED else intro.NO_SHOW
    moved = store.record_intro_meeting_outcome(
        existing["meeting_id"], outcome=outcome,
        allowed_from=intro.allowed_from(outcome), actor="calendly")
    return {"action": outcome if moved else "no_change",
            "meeting_id": existing["meeting_id"]}
