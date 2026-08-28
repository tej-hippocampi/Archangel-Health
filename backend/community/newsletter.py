"""The morning email.

The channels fill up whether or not anyone is looking. This is the part that
makes someone look: one email at 7am local, per doctor, carrying what landed
in their rooms overnight and whether there is work waiting for them.

Three rules it is built around.

**Never write to say nothing.** No fresh posts and no new tasks means no
email. A daily message that is empty four days a week trains people to filter
it, and then the one that mattered goes to the same place.

**Their rooms, not all rooms.** A nephrologist in Riyadh gets the core
channels, #nephrology and #saudi-arabia. Sending everybody everything is how a
newsletter becomes noise with their name at the top.

**One email a day, total.** When the morning routine is on it owns the daily
send, and the older news-digest email stands down: two automated emails on the
same morning from the same product is one too many.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from community import countries as ccountries
from community import links
from community import morning as cmorning
from community.store import get_community_store

log = logging.getLogger("community.newsletter")

#: Bot posts worth putting in an email. A welcome or a task announcement is
#: already its own notification; these are the ones nobody was told about.
_CONTENT_KINDS = (
    cmorning.KIND_EVENTS, cmorning.KIND_NEWS, cmorning.KIND_OPPORTUNITIES,
    cmorning.KIND_BRIEF, cmorning.KIND_DISCUSSION,
    "digest_news", "digest_papers",
)


def enabled() -> bool:
    return cmorning.enabled()


def _portal_url() -> str:
    base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")
    return f"{base}/community"


def _since_iso(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _member_channels(member: Dict[str, Any], channels: List[Dict[str, Any]]) -> List[str]:
    """The rooms this doctor is actually in."""
    specialty = (member.get("specialty") or "").strip().lower()
    country = (member.get("country") or "").strip().upper()
    out = []
    for ch in channels:
        grp = ch.get("grp") or "core"
        if grp == "core":
            out.append(ch["slug"])
        elif grp == "specialty" and (ch.get("specialty") or "").strip().lower() == specialty:
            out.append(ch["slug"])
        elif grp == "country" and (ch.get("country") or "").strip().upper() == country:
            out.append(ch["slug"])
    return out


def _task_line(astore: Any, member: Dict[str, Any]) -> str:
    """What is waiting for them, or plainly that nothing is.

    "No new tasks today" is worth saying: the alternative is a doctor opening
    the portal to check, finding nothing, and doing that a few times before
    they stop opening it.
    """
    specialty = (member.get("specialty") or "").strip().lower()
    try:
        tasks = astore.list_tasks(status="open") or []
    except Exception:  # noqa: BLE001
        return ""
    since = _since_iso()
    fresh = [
        t for t in tasks
        if str(t.get("created_at") or "") >= since
        and (not specialty or (t.get("specialty") or "").strip().lower() == specialty)
    ]
    if not fresh:
        return "No new tasks today. Your queue is clear."
    label = specialty or "new"
    noun = "task" if len(fresh) == 1 else "tasks"
    return f"{len(fresh)} new {label} {noun} are waiting for you."


def _collect_sections(cstore: Any, slugs: List[str]) -> List[Dict[str, Any]]:
    """Fresh bot posts from these channels, newest first."""
    since = _since_iso()
    sections: List[Dict[str, Any]] = []
    for slug in slugs:
        channel = cstore.get_channel_by_slug(slug)
        if not channel:
            continue
        try:
            messages, _more = cstore.list_messages(channel["id"], limit=20)
        except Exception:  # noqa: BLE001
            continue
        for m in messages:
            if m.get("author_user_id") != "u-system":
                continue
            if (m.get("kind") or "") not in _CONTENT_KINDS:
                continue
            if str(m.get("created_at") or "") < since:
                continue
            if m.get("deleted_at"):
                continue
            cards = m.get("cards") or []
            if isinstance(cards, str):
                import json as _json
                try:
                    cards = _json.loads(cards)
                except ValueError:
                    cards = []
            sections.append({
                "channel": slug,
                "body": m.get("body") or "",
                "cards": cards or [],
            })
    return sections


async def send_for_member(
    cstore: Any, astore: Any, member: Dict[str, Any],
    channels: List[Dict[str, Any]], *, dry_run: bool = False,
) -> str:
    """One doctor's email. Returns what happened, for the run summary."""
    email = (member.get("email") or "").strip()
    if not email:
        return "no_email"

    prefs = cstore.email_prefs(member["user_id"])
    if (prefs.get("news_frequency") or "daily") == "off":
        return "unsubscribed"

    slugs = _member_channels(member, channels)
    sections = _collect_sections(cstore, slugs)
    task_line = _task_line(astore, member)

    # Nothing happened and nothing is waiting: do not write.
    if not sections and task_line.startswith("No new tasks"):
        return "quiet"

    if dry_run:
        return "would_send"

    from email_utils import send_html_email
    from onboarding_emails import build_community_morning_email

    unsubscribe = links.unsubscribe_url(prefs.get("unsubscribe_token") or "")
    html = build_community_morning_email(
        first_name=(member.get("display_name") or "").split(" ")[-1] or None,
        sections=sections,
        task_line=task_line,
        community_url=_portal_url(),
        unsubscribe_url=unsubscribe,
    )
    try:
        ok = await send_html_email(email, "Your morning in Archangel", html)
    except Exception:  # noqa: BLE001
        log.warning("[newsletter] send failed for one member", exc_info=True)
        return "failed"
    return "sent" if ok else "failed"


async def run_newsletter(*, force: bool = False) -> Dict[str, Any]:
    """Every country cohort whose local 7am has passed today."""
    cstore = get_community_store()
    try:
        from asclepius.store import get_store as _get_astore
        from community.router import member_map

        astore = _get_astore()
        members = member_map(include_email=True)
    except Exception:  # noqa: BLE001
        log.warning("[newsletter] could not resolve members", exc_info=True)
        return {"cohorts": [], "sent": 0}

    channels = cstore.list_channels()

    # Group by country so each cohort fires on its own morning.
    cohorts: Dict[str, List[Dict[str, Any]]] = {}
    for member in members.values():
        if member.get("is_staff"):
            continue
        code = (member.get("country") or "").strip().upper() or "_home"
        cohorts.setdefault(code, []).append(member)

    ran, sent, skipped = [], 0, []
    for code, cohort in sorted(cohorts.items()):
        tz = ccountries.timezone_for(code) if code != "_home" else cmorning.home_timezone()
        key = f"morning:newsletter:{code}"
        if not force and not cmorning.is_due(cstore.last_successful_run_at(key), tz):
            skipped.append(key)
            continue
        run_id = cstore.start_digest_run(key)
        delivered = 0
        try:
            for member in cohort:
                outcome = await send_for_member(cstore, astore, member, channels)
                if outcome == "sent":
                    delivered += 1
            cstore.finish_digest_run(run_id, ok=True, items_posted=delivered)
            ran.append(key)
            sent += delivered
        except Exception as exc:  # noqa: BLE001
            log.warning("[newsletter] cohort %s failed", code, exc_info=True)
            try:
                cstore.finish_digest_run(run_id, ok=False, items_posted=delivered, error=str(exc)[:200])
            except Exception:  # noqa: BLE001
                pass
    log.info("[newsletter] cohorts=%d sent=%d skipped=%d", len(ran), sent, len(skipped))
    return {"cohorts": ran, "sent": sent, "skipped": skipped}
