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

The third rule is the one this file got wrong. "Once a day" was recorded per
COUNTRY COHORT, and a cohort ledger cannot answer the only question a mail path
has to answer: has THIS doctor already been written to this morning. Two
schedulers drive this (the in-process hourly loop and the hourly external
trigger), both passed the cohort due-check, and both mailed the whole roster;
and a cohort that failed on doctor 400 of 900 re-mailed the first 399 on the
next tick, because a released cohort window restarts at the top. Both halves are
fixed with the claim the community ledger already provides: the cohort takes a
window so only one runner owns the morning, and every doctor takes their own,
so a send is at-most-once per person per morning and a resumed run picks up
where it stopped.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from community import countries as ccountries
from community import links
from community import morning as cmorning
from community import store as cstore_mod
from community.store import get_community_store

log = logging.getLogger("community.newsletter")

#: Bot posts worth putting in an email. A welcome or a task announcement is
#: already its own notification; these are the ones nobody was told about.
#: ``poll`` is here because the weekly discussion prompt became one. The
#: collector only ever looks at bot-authored rows, so a member's poll cannot
#: reach the email through this.
_CONTENT_KINDS = (
    cmorning.KIND_EVENTS, cmorning.KIND_NEWS, cmorning.KIND_OPPORTUNITIES,
    cmorning.KIND_BRIEF, cmorning.KIND_DISCUSSION, cmorning.KIND_POLL,
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


def _window_key(tz_name: str) -> str:
    """Which morning this is, in the cohort's own timezone.

    Delegated to ``morning.window_key`` rather than reimplemented, because the
    window and the due-check have to mean the same day: a UTC date would put two
    runners either side of local midnight into different windows and let both
    mail. A throwaway Scope is the only shape that function takes, and paying
    that rather than writing a second date derivation is the point.
    """
    return cmorning.window_key(cmorning.Scope(key="", channel="", tz=tz_name))


def _member_ledger_key(user_id: str) -> str:
    """The per-recipient ledger key: one row per doctor per morning.

    Deliberately in the same table as the cohort's own claim rather than in a
    new one. ``claim_digest_run`` is the claim primitive this codebase already
    has, with the lease and the abandoned-window release already reasoned about,
    and a second claim mechanism written next to it would be a second thing to
    get right. The cost is one small ledger row per doctor per day, which is the
    cheapest possible record of "this person has been written to".
    """
    return f"morning:newsletter:member:{user_id}"


def _member_channels(member: Dict[str, Any], channels: List[Dict[str, Any]]) -> List[str]:
    """The rooms this doctor is actually in.

    The ``staff_only`` skip is first and applies to everyone, staff included.
    This is the mail path, and a staff-only room's content is not something to
    put in an outgoing email at all: the in-app channel is where it is read, and
    the failure mode of getting this wrong is a physician receiving the team's
    internal digest in their inbox.
    """
    specialty = (member.get("specialty") or "").strip().lower()
    country = (member.get("country") or "").strip().upper()
    subspecialties = set(member.get("subspecialties") or ())
    city = (member.get("city") or "").strip()
    crossed = cstore_mod.specialty_region_key(specialty, member.get("region"))
    out = []
    for ch in channels:
        if ch.get("staff_only"):
            continue
        grp = ch.get("grp") or "core"
        if grp == "core":
            out.append(ch["slug"])
        elif grp == "specialty" and (ch.get("specialty") or "").strip().lower() == specialty:
            out.append(ch["slug"])
        elif grp == "country" and (ch.get("country") or "").strip().upper() == country:
            out.append(ch["slug"])
        elif grp == "subspecialty" and (ch.get("subspecialty") or "").strip() in subspecialties:
            out.append(ch["slug"])
        elif grp == "city" and city and (ch.get("city") or "").strip() == city:
            out.append(ch["slug"])
        elif grp == "specialty_region" and crossed and cstore_mod.specialty_region_key(
                ch.get("specialty"), ch.get("region")) == crossed:
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
    window: Optional[str] = None,
) -> str:
    """One doctor's email. Returns what happened, for the run summary.

    ``window`` is this doctor's morning (their cohort's local date). Passing it
    puts the send behind a per-recipient claim, which is what makes the send
    at-most-once for this person on this morning no matter how many runners
    reach them. Omitting it sends unconditionally, which is what a single
    hand-driven call for one doctor wants and what the existing callers do.
    """
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

    # Claim this doctor's morning HERE, immediately before the send and after
    # every reason not to send has been checked. Claiming earlier would spend a
    # doctor's one send on a quiet day and silence a later tick that had
    # something to say; claiming after the send would be a record of what
    # happened rather than a reservation, which is the shape the cohort ledger
    # had and the reason two runners both mailed everyone.
    run_id = None
    if window is not None:
        run_id = cstore.claim_digest_run(_member_ledger_key(member["user_id"]),
                                         window_key=window)
        if run_id is None:
            # Somebody else is mailing this doctor, or already has. Either way
            # the answer is the same and it is not an error.
            return "already_sent"

    ok = False
    try:
        ok = await send_html_email(email, "Your morning in Archangel", html)
    except Exception:  # noqa: BLE001
        log.warning("[newsletter] send failed for one member", exc_info=True)
    finally:
        # A successful send KEEPS the window, which is the whole at-most-once
        # guarantee. A failure RELEASES it, so the next tick retries this doctor
        # rather than writing them off for the day: an unsent morning and a
        # duplicate morning are both bad, and the retry is the cheaper mistake.
        if run_id is not None:
            try:
                cstore.finish_digest_run(run_id, ok=bool(ok), items_posted=1 if ok else 0)
            except Exception:  # noqa: BLE001
                # The claim stands and the lease will release it. Worth a line:
                # a ledger that will not write means this doctor gets no mail
                # until the lease lapses, and that should not be silent.
                log.warning("[newsletter] could not close the send ledger row",
                            exc_info=True)
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
        # The cohort's own claim, exactly as ``morning.run_scope`` takes one.
        # ``is_due`` above is a READ, and two schedulers can both pass it in the
        # same hour; this is the write only one of them can win. ``start_digest_run``,
        # which this used to call, reserves nothing, so both used to proceed.
        window = _window_key(tz)
        run_id = cstore.claim_digest_run(key, window_key=None if force else window)
        if run_id is None:
            log.info("[newsletter] cohort %s already claimed for %s", code, window)
            skipped.append(key)
            continue
        delivered = 0
        try:
            for member in cohort:
                # ``window`` is passed even on a forced run. Force overrides the
                # SCHEDULE, never the delivery ledger: an operator firing this by
                # hand after a partial run wants the doctors who were missed, not
                # a second copy for everyone who already has theirs.
                outcome = await send_for_member(cstore, astore, member, channels,
                                                window=window)
                if outcome == "sent":
                    delivered += 1
            cstore.finish_digest_run(
                run_id, ok=True, items_posted=delivered,
                # A cohort whose every doctor had a quiet morning is a valid
                # run that sent nothing, and it should not read on the admin
                # tab like a newsletter that is broken.
                reason=(cmorning.REASON_POSTED if delivered
                        else cmorning.REASON_NOTHING_FOUND))
            ran.append(key)
            sent += delivered
        except Exception as exc:  # noqa: BLE001
            log.warning("[newsletter] cohort %s failed", code, exc_info=True)
            try:
                cstore.finish_digest_run(run_id, ok=False, items_posted=delivered,
                                         error=str(exc)[:200],
                                         reason=cmorning.REASON_ERROR)
            except Exception:  # noqa: BLE001
                pass
    log.info("[newsletter] cohorts=%d sent=%d skipped=%d", len(ran), sent, len(skipped))
    return {"cohorts": ran, "sent": sent, "skipped": skipped}
