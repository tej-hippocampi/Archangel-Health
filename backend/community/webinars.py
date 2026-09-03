"""The recurring weekend webinar.

The Sep 1 meeting put the conference and the podcast after the seed and this
one before it, because it is the cheap version of the same idea: a standing
time each weekend when the community does something together, with the first
one being "vibe-code a tool for your practice". A physician who has come to one
of those has met people, which is the thing a channel list cannot do on its
own.

This is not a subsystem. Events, RSVP, the reminder email and the .ics download
already exist and already work; the only thing missing was that somebody had to
remember to create the event every week. So this creates the next few
occurrences and then gets out of the way: everything a member does with a
webinar afterwards is the ordinary event path, unchanged.

**It stays silent until a person supplies a join link.** An event with a time
and nowhere to go is worse than no event, and a placeholder URL is the kind of
thing that ends up in production. ``COMMUNITY_WEBINAR_URL`` is the switch: set
it and the series appears, leave it and nothing is created, logged loudly
either way at startup.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from community import phi_gate
from community.store import get_community_store
from community.system_posts import SYSTEM_USER_ID, post_system_message

log = logging.getLogger("community.webinars")

WEBINAR_CHANNEL = "events"

#: How far ahead the series is kept filled. Three weeks is enough that the
#: #events rail always shows a next one and someone planning their weekend can
#: find it, and short enough that changing the time or the topic does not mean
#: cancelling a quarter of events nobody had RSVP'd to yet.
DEFAULT_WEEKS_AHEAD = 3


def join_url() -> str:
    return (os.getenv("COMMUNITY_WEBINAR_URL") or "").strip()


def enabled() -> bool:
    """A join link IS the gate. One switch, and it is the one that also has to
    be right for the event to be worth attending."""
    return bool(join_url())


def title() -> str:
    return (os.getenv("COMMUNITY_WEBINAR_TITLE")
            or "Vibe-code a tool for your practice").strip()


def description() -> str:
    return (os.getenv("COMMUNITY_WEBINAR_DESC") or
            "An hour, live, building something small and real for your own "
            "clinic with an AI coding tool. Bring the annoying part of your "
            "week; leave with a first version of the thing that fixes it. No "
            "prior programming needed.").strip()


def host() -> str:
    return (os.getenv("COMMUNITY_WEBINAR_HOST") or "").strip()


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def weekday() -> int:
    """Python weekday, 0 = Monday. Defaults to Saturday.

    A weekend slot is not a detail: the people this is for are on shift during
    the week, and an event they can never attend is worse than none.
    """
    return _int_env("COMMUNITY_WEBINAR_DOW", 5, 0, 6)


def hour_local() -> int:
    return _int_env("COMMUNITY_WEBINAR_HOUR_LOCAL", 11, 0, 23)


def duration_min() -> int:
    return _int_env("COMMUNITY_WEBINAR_DURATION_MIN", 60, 15, 480)


def weeks_ahead() -> int:
    return _int_env("COMMUNITY_WEBINAR_WEEKS_AHEAD", DEFAULT_WEEKS_AHEAD, 1, 12)


def timezone_name() -> str:
    from community import morning as cmorning  # noqa: PLC0415 - shares the house clock

    return cmorning.home_timezone()


def upcoming_starts(*, now: Optional[datetime] = None) -> List[str]:
    """The next few occurrence times, as ISO-Z UTC strings.

    Computed in the house timezone and converted, so the series stays at 11am
    local across a daylight-saving change instead of drifting an hour twice a
    year. Pure, so the schedule is testable with a frozen clock.
    """
    try:
        tz = ZoneInfo(timezone_name())
    except Exception:  # noqa: BLE001 - a bad zone must not stop the world
        tz = ZoneInfo("America/New_York")
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(tz)

    ahead = (weekday() - local.weekday()) % 7
    first = (local + timedelta(days=ahead)).replace(
        hour=hour_local(), minute=0, second=0, microsecond=0)
    # Today's slot has already been and gone: start from next week rather than
    # creating an event in the past that shows up as "upcoming" nowhere.
    if first <= local:
        first = first + timedelta(days=7)
    out = []
    for i in range(weeks_ahead()):
        at = first + timedelta(days=7 * i)
        out.append(at.astimezone(timezone.utc).replace(microsecond=0)
                   .isoformat().replace("+00:00", "Z"))
    return out


def _body() -> str:
    """The in-stream message. Deliberately carries no calendar date: a full
    date trips the PHI exact-date rule, and the event card renders the time
    from the structured row anyway."""
    parts = [f"📅 **{title()}**"]
    if host():
        parts.append("Host: " + host())
    parts.append(description())
    parts.append("_See the event card in #events for the time and to RSVP. "
                 "It repeats every week._")
    return "\n".join(parts)


async def ensure_upcoming(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Create any missing occurrence in the window. Never raises.

    Idempotent on (channel, title, starts_at): the caller runs daily, and an
    event duplicated once a day for a week is a channel nobody trusts. Matching
    on the start time rather than a stored series id keeps this a plain event
    with no schema of its own, which is the whole point of doing it this way.
    """
    if not enabled():
        return {"ok": True, "created": 0, "reason": "no_join_url"}

    # PHI gate on the free text before anything is stored, same rule as the
    # HTTP create-event route: title, description and host are operator
    # supplied env strings, and a row written by the bot must clear the same
    # bar as one written by an admin.
    blob = " \n ".join(x for x in [title(), description(), host()] if x)
    if phi_gate.scan_text(blob):
        log.error("[webinars] event text tripped the PHI gate, nothing created")
        return {"ok": False, "created": 0, "reason": "phi_detected"}

    cstore = get_community_store()
    channel = cstore.get_channel_by_slug(WEBINAR_CHANNEL)
    if not channel or not channel.get("is_active", 1):
        log.warning("[webinars] #%s is missing or inactive", WEBINAR_CHANNEL)
        return {"ok": False, "created": 0, "reason": "no_channel"}

    try:
        existing = {
            (e.get("starts_at") or "") for e in cstore.list_events(channel["id"], scope="upcoming")
            if (e.get("title") or "") == title() and not e.get("cancelled_at")
        }
    except Exception:  # noqa: BLE001
        log.warning("[webinars] could not read existing events", exc_info=True)
        return {"ok": False, "created": 0, "reason": "read_failed"}

    created = 0
    for starts in upcoming_starts(now=now):
        if starts in existing:
            continue
        try:
            ends = (datetime.fromisoformat(starts.replace("Z", "+00:00"))
                    + timedelta(minutes=duration_min())).replace(microsecond=0) \
                .isoformat().replace("+00:00", "Z")
            event = cstore.create_event(
                channel_id=channel["id"], title=title(),
                description=description(), starts_at=starts, ends_at=ends,
                timezone=timezone_name(), location=join_url(),
                host=host() or None, created_by=SYSTEM_USER_ID,
            )
            # One announcement for the series, not one a week. The first
            # occurrence gets an in-stream post; the ones after it are card and
            # RSVP only, because a channel that announces the same recurring
            # thing every Monday is a channel people mute.
            if not existing and created == 0:
                posted = await post_system_message(
                    channel_slug=channel["slug"], body=_body(), kind="event")
                if posted:
                    cstore.link_event_message(event["id"], posted["id"])
            created += 1
        except Exception:  # noqa: BLE001 - one bad occurrence must not stop the rest
            log.warning("[webinars] could not create the occurrence at %s", starts,
                        exc_info=True)
    if created:
        log.info("[webinars] created %d occurrence(s) of %r", created, title())
    return {"ok": True, "created": created}
