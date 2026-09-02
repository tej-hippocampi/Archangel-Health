"""The application nudge schedule (Onboarding v2 §3).

Five emails, each sent at most once per application, ever.

Before the application is submitted, against the health-system invite row:

  * **at 24 hours** — §4.2, "your application is waiting". One nudge. Not a
    drip, not a sequence, no countdown, no guilt: the reason a physician stopped
    halfway is almost always a pager, and the correct response to that is one
    reminder that their answers are still there.
  * **at day 6** — the link dies at day 7 (``_SELF_SERVE_EXPIRES_DAYS``), so it
    expires with a warning rather than silently.

After it is submitted, against the applicant's own account:

  * **credentials**: an applicant who left us nothing to verify them against
    is waiting on a decision that cannot be made. Once, ever.
  * **practice case**: the one piece of clinical judgment we see before
    deciding. Once, ever.
  * **profile**: one question about one missing profile field, for an approved
    physician, at most once every thirty days and once per field ever.

Those three read the ASCLEPIUS store rather than the team store, because they
are about accounts rather than invites. That is the only structural difference:
the claim-then-send order, the batch cap and the transport check are shared.

Idempotency is structural rather than remembered. Each send has its own STAMP
column on the invite row, and the sweep claims a row with a conditional UPDATE
before it sends. So:

  * a restart mid-sweep cannot double-send — the claim already committed;
  * two workers racing the same row cannot both send — sqlite picks one;
  * a send that fails is not retried — which is the right trade here, because a
    physician receiving the same nudge twice is a worse outcome than one who
    receives it zero times and still has the link in their inbox from §4.1.

This rides the verification agent's existing loop rather than owning a timer of
its own: that loop already polls on an interval, already runs its sqlite work off
the event loop, and already survives a failing iteration. A second scheduler
would be a second thing to get wrong on deploy.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger("asclepius.onboarding_nudge")

#: How stale an unfinished application must be before the one nudge goes.
NUDGE_AFTER_HOURS = float(os.getenv("ASCLEPIUS_NUDGE_AFTER_HOURS", "24") or 24)

#: When the expiry warning goes. Six days against a seven-day link: one full day
#: of warning, which is the point.
EXPIRY_WARN_AFTER_HOURS = float(os.getenv("ASCLEPIUS_EXPIRY_WARN_AFTER_HOURS", "144") or 144)

#: A cap per pass, so a backlog drains over several sweeps instead of trying to
#: send hundreds of emails inside one loop iteration.
_BATCH = int(os.getenv("ASCLEPIUS_NUDGE_BATCH", "50") or 50)

#: How often the sweep is allowed to run, regardless of how fast the agent loop
#: polls. The agent's interval is tuned for verification jobs (30s by default);
#: re-running these two queries twice a minute forever is pointless work.
SWEEP_INTERVAL_SECONDS = float(os.getenv("ASCLEPIUS_NUDGE_SWEEP_SECONDS", "900") or 900)

#: How long an application sits before we chase what is missing from it. Same
#: 24 hours as the pre-submit nudge, and the same reason: a pager, not apathy.
APPLICANT_NUDGE_AFTER_HOURS = int(
    os.getenv("ASCLEPIUS_APPLICANT_NUDGE_AFTER_HOURS", "24") or 24)

#: The floor between two profile questions to the same physician. Enforced in
#: the store (``stamp_profile_nudge``), named here so the schedule is readable
#: in one place.
PROFILE_NUDGE_MIN_DAYS = int(os.getenv("ASCLEPIUS_PROFILE_NUDGE_MIN_DAYS", "30") or 30)


def _first_name(row: Dict[str, Any]) -> str:
    return (row.get("director_first_name") or "").strip()


def _portal_base() -> str:
    return (os.getenv("ASCLEPIUS_PORTAL_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")


async def _send_one(kind: str, row: Dict[str, Any]) -> bool:
    from email_utils import send_html_email  # noqa: PLC0415
    from onboarding_emails import (  # noqa: PLC0415
        build_application_expiring_email, build_application_nudge_email,
    )

    email = (row.get("director_email") or "").strip()
    url = (row.get("last_generated_invite_url") or "").strip()
    if not email or not url:
        return False
    if kind == "nudge":
        subject = "Your application is waiting — 2 minutes to finish"
        html_body = build_application_nudge_email(
            first_name=_first_name(row), onboarding_url=url)
    else:
        subject = "Your Archangel Health link expires tomorrow"
        html_body = build_application_expiring_email(
            first_name=_first_name(row), onboarding_url=url)
    return bool(await send_html_email(email, subject, html_body))


def _still_owes(kind: str, user: Dict[str, Any]) -> bool:
    """Is the thing this nudge is about still outstanding?

    The due-list query cannot answer this: credential evidence lives across a
    column, an NPI and a JSON blob, and the practice gate lives inside the
    tutorial blob. Both predicates are BORROWED from the admin queue rather
    than re-stated here, because "has this applicant done their half" is one
    question, and a nudge that disagrees with the queue about the answer is a
    physician chased for something an admin can already see they did.
    """
    from asclepius import capabilities as caps  # noqa: PLC0415
    from routers.asclepius_verify import _has_credential_evidence  # noqa: PLC0415

    if kind == "credentials":
        return not _has_credential_evidence(user)
    return caps.practice_gate_state(user) == caps.GATE_LOCKED


async def _send_applicant_one(kind: str, user: Dict[str, Any]) -> bool:
    from email_utils import send_html_email  # noqa: PLC0415
    from onboarding_emails import (  # noqa: PLC0415
        build_credentials_nudge_email, build_practice_case_nudge_email,
    )

    email = (user.get("email") or "").strip()
    if not email:
        return False
    name = (user.get("full_name") or "").strip()
    url = _portal_base() + "/asclepius"
    if kind == "credentials":
        subject = "One thing missing from your application"
        html_body = build_credentials_nudge_email(first_name=name, portal_url=url)
    else:
        subject = "Your practice case is waiting"
        html_body = build_practice_case_nudge_email(first_name=name, portal_url=url)
    return bool(await send_html_email(email, subject, html_body))


def _blob(user: Dict[str, Any], column: str) -> Dict[str, Any]:
    raw = user.get(column)
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _first_profile_gap(user: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """The single field this physician would be asked about, or None.

    Read from the same completeness rule the profile page renders, so the email
    can never ask for something the meter says is already there.

    Fields already asked about are skipped rather than left to be refused by the
    claim. Handing the claim a field it will always reject would make the whole
    sweep stall on the first gap a physician chose not to fill: they would be
    asked once about their languages, decline, and never hear about anything
    else again. None means there is nothing left to ask, which is also what a
    complete profile looks like from here.
    """
    from routers.asclepius import _profile_completeness  # noqa: PLC0415

    asked = set((_blob(user, "profile_nudge_json").get("fields") or {}).keys())
    missing = (_profile_completeness(user, _blob(user, "credentials_json")) or {}
               ).get("missing") or []
    for gap in missing:
        if gap.get("field") not in asked:
            return gap
    return None


async def _send_profile_one(gap: Dict[str, str], user: Dict[str, Any]) -> bool:
    from email_utils import send_html_email  # noqa: PLC0415
    from onboarding_emails import build_profile_nudge_email  # noqa: PLC0415

    email = (user.get("email") or "").strip()
    if not email:
        return False
    return bool(await send_html_email(
        email, "One quick question about your profile",
        build_profile_nudge_email(
            first_name=(user.get("full_name") or "").strip(),
            field_label=gap.get("label") or gap.get("field") or "",
            profile_url=_portal_base() + "/asclepius#profile",
        )))


async def _sweep_applicants(store: Any, sent: Dict[str, int]) -> None:
    import asyncio  # noqa: PLC0415

    for kind in ("credentials", "practice"):
        try:
            rows = await asyncio.to_thread(
                store.list_applicants_needing_nudge,
                kind, APPLICANT_NUDGE_AFTER_HOURS, _BATCH,
            )
        except Exception:
            log.exception("[nudge] could not list %s candidates", kind)
            continue
        for user in rows:
            try:
                if not _still_owes(kind, user):
                    continue
                # Claim FIRST, exactly as above.
                if not await asyncio.to_thread(
                        store.stamp_applicant_nudge, user["id"], kind):
                    continue
                if await _send_applicant_one(kind, user):
                    sent[kind] += 1
            except Exception:
                log.exception("[nudge] %s send failed for %s", kind, user.get("id"))


async def _sweep_profiles(store: Any, sent: Dict[str, int]) -> None:
    import asyncio  # noqa: PLC0415

    try:
        rows = await asyncio.to_thread(store.list_profiles_needing_nudge, _BATCH)
    except Exception:
        log.exception("[nudge] could not list profile candidates")
        return
    for user in rows:
        try:
            gap = _first_profile_gap(user)
            if not gap:
                continue
            # The store's claim carries BOTH rules: this field has never been
            # asked about, and this physician has not heard from us inside the
            # spacing window. One conditional write, so two racing sweeps
            # cannot both decide the same question is fair game.
            if not await asyncio.to_thread(
                    store.stamp_profile_nudge, user["id"], gap["field"],
                    min_days_between=PROFILE_NUDGE_MIN_DAYS):
                continue
            if await _send_profile_one(gap, user):
                sent["profile"] += 1
        except Exception:
            log.exception("[nudge] profile send failed for %s", user.get("id"))


async def sweep(ts: Optional[Any] = None, store: Optional[Any] = None) -> Dict[str, int]:
    """Send whichever nudges are due, one count per kind.

    Never raises: a scheduler that can throw is a scheduler that stops. Every
    per-row failure is logged and the sweep carries on with the next row, because
    one physician's malformed address must not cost everyone else their reminder.
    """
    import asyncio  # noqa: PLC0415

    from email_utils import is_email_transport_configured  # noqa: PLC0415
    from team_store import get_team_store  # noqa: PLC0415

    sent = {"nudge": 0, "expiry": 0, "credentials": 0, "practice": 0, "profile": 0}
    if not is_email_transport_configured():
        # Nothing to do, and — critically — nothing STAMPED. A deployment with no
        # mail transport must not silently burn every physician's one nudge.
        return sent
    ts = ts or get_team_store()

    for kind, hours in (("nudge", NUDGE_AFTER_HOURS),
                        ("expiry", EXPIRY_WARN_AFTER_HOURS)):
        try:
            rows = await asyncio.to_thread(
                ts.list_unfinished_asclepius_invites,
                kind=kind, older_than_hours=hours, limit=_BATCH,
            )
        except Exception:
            log.exception("[nudge] could not list %s candidates", kind)
            continue
        for row in rows:
            try:
                # Claim FIRST. See the module docstring: a stamped-but-unsent
                # nudge costs one email; an unstamped-but-sent one costs the
                # physician a duplicate, and there is no way to take it back.
                if not await asyncio.to_thread(ts.stamp_onboarding_nudge, row["id"], kind):
                    continue
                if await _send_one(kind, row):
                    sent[kind] += 1
            except Exception:
                log.exception("[nudge] %s send failed for %s", kind, row.get("id"))

    # The post-submit kinds read accounts, not invites, so they need the second
    # handle. Resolved here rather than at import: this module rides the
    # verification agent's loop and must not pull the asclepius store into
    # existence just by being imported.
    if store is None:
        try:
            from asclepius.store import get_store  # noqa: PLC0415
            store = get_store()
        except Exception:
            log.exception("[nudge] no asclepius store; post-submit nudges skipped")
            store = None
    if store is not None:
        await _sweep_applicants(store, sent)
        await _sweep_profiles(store, sent)

    if any(sent.values()):
        log.info("[nudge] sent %s", ", ".join(f"{k}={v}" for k, v in sent.items() if v))
    return sent
