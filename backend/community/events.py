"""Events helpers (Community v2.1): .ics generation + the reminder loop.

Add-to-calendar is deliberately integration-free: the ``.ics`` is string-
templated here (served as ``text/calendar``) and the Google Calendar link is a
pure URL template built on the client. No OAuth, no calendar sync.

The reminder loop is one in-process async loop (same shape as
``notify.start_digest_loop`` / ``digest.start_content_loop``): each tick it
finds events starting within ``COMMUNITY_EVENT_REMINDER_MIN`` minutes that have
interested members not yet reminded, emails them, and stamps ``reminded_at`` so
each member is reminded at most once per event. No-ops without email transport.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import realm as _realm
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from community.store import get_community_store

log = logging.getLogger("community.events")


def reminder_window_min() -> int:
    try:
        return max(1, int(os.getenv("COMMUNITY_EVENT_REMINDER_MIN", "60")))
    except (TypeError, ValueError):
        return 60


# ─── .ics (RFC 5545 minimal VEVENT) ───────────────────────────────────────────
def _ics_dt(iso_z: str) -> str:
    """ISO-8601 UTC (``2026-09-10T17:00:00Z``) → iCalendar basic UTC
    (``20260910T170000Z``)."""
    s = (iso_z or "").strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%dT%H%M%SZ")
    except ValueError:
        # Best-effort strip of separators if already close to basic form.
        return re.sub(r"[-:]", "", s).replace(".000", "")


def _ics_escape(text: str) -> str:
    """Escape per RFC 5545 §3.3.11 (backslash, comma, semicolon, newline)."""
    return (text or "").replace("\\", "\\\\").replace("\n", "\\n") \
        .replace(",", "\\,").replace(";", "\\;")


def build_ics(event: Dict[str, Any]) -> str:
    """Minimal single-VEVENT calendar for one event row."""
    eid = event["id"]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Archangel Health//Community Events//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:community-event-{eid}@archangelhealth.ai",
        "DTSTAMP:" + datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "DTSTART:" + _ics_dt(event["starts_at"]),
    ]
    if event.get("ends_at"):
        lines.append("DTEND:" + _ics_dt(event["ends_at"]))
    lines.append("SUMMARY:" + _ics_escape(event["title"]))
    desc_bits = []
    if event.get("description"):
        desc_bits.append(event["description"])
    if event.get("host"):
        desc_bits.append("Host: " + event["host"])
    if desc_bits:
        lines.append("DESCRIPTION:" + _ics_escape("\n".join(desc_bits)))
    if event.get("location"):
        lines.append("LOCATION:" + _ics_escape(event["location"]))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    # RFC 5545 wants CRLF line endings.
    return "\r\n".join(lines) + "\r\n"


# ─── Reminder email ───────────────────────────────────────────────────────────
def _reminder_html(event: Dict[str, Any], member_name: str) -> str:
    from onboarding_emails import build_community_event_reminder_email  # noqa: PLC0415

    base = (os.getenv("PUBLIC_BASE_URL", "") or "").rstrip("/")
    name = (member_name or "").strip()
    return build_community_event_reminder_email(
        first_name=name.split()[0] if name else "there",
        title=event.get("title") or "an event",
        when_label=event.get("starts_at") or "",
        timezone_label=event.get("timezone") or "UTC",
        community_url=f"{base}/community",
        location=event.get("location") or "",
        host=event.get("host") or "",
    )


async def send_due_reminders(*, resolve_member: Any) -> int:
    """Email every interested-but-un-reminded member of each event starting
    within the window. ``resolve_member(user_id) -> {email, display_name}|None``
    is injected by the caller (owns the member map). Returns emails sent.
    Best-effort and at-most-once: a member is stamped reminded the moment we
    attempt their send, mirroring the digest loop's at-most-once contract."""
    from email_utils import is_email_transport_configured, send_html_email  # noqa: PLC0415

    if not is_email_transport_configured():
        return 0
    cstore = get_community_store()
    due = cstore.events_needing_reminder(within_minutes=reminder_window_min())
    sent = 0
    for entry in due:
        event = entry["event"]
        user_ids = entry["user_ids"]
        # Stamp first (at-most-once): a crash mid-loop must not re-blast.
        cstore.mark_reminded(event["id"], user_ids)
        for uid in user_ids:
            member = resolve_member(uid) if resolve_member else None
            if not member or not member.get("email"):
                continue
            try:
                ok = await send_html_email(
                    member["email"],
                    f"Reminder: {event.get('title') or 'an event'} is coming up",
                    _reminder_html(event, member.get("display_name") or ""),
                )
                if ok:
                    sent += 1
            except Exception:
                log.warning("[events] reminder email failed for one recipient", exc_info=True)
    return sent


# ─── In-process reminder loop ─────────────────────────────────────────────────
_loop_task: Optional[asyncio.Task] = None
_TICK_SEC = 300  # 5 min — comfortably finer than the default 60-min window


def start_reminder_loop(*, resolve_member: Any) -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return

    async def _run() -> None:
        while True:
            await asyncio.sleep(_TICK_SEC)
            for r in _realm.active_realms():  # Sandbox PRD §1.3
                try:
                    with _realm.scoped(r):
                        await send_due_reminders(resolve_member=resolve_member)
                except Exception:  # pragma: no cover — the loop must survive
                    log.warning("[events] reminder sweep failed (%s)", r, exc_info=True)

    _loop_task = asyncio.get_running_loop().create_task(_run())
    log.info("[events] reminder loop started (window %d min)", reminder_window_min())


def stop_reminder_loop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
