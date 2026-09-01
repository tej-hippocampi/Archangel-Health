"""The application nudge schedule (Onboarding v2 §3).

Two emails, each sent at most once per application, ever:

  * **at 24 hours** — §4.2, "your application is waiting". One nudge. Not a
    drip, not a sequence, no countdown, no guilt: the reason a physician stopped
    halfway is almost always a pager, and the correct response to that is one
    reminder that their answers are still there.
  * **at day 6** — the link dies at day 7 (``_SELF_SERVE_EXPIRES_DAYS``), so it
    expires with a warning rather than silently.

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


def _first_name(row: Dict[str, Any]) -> str:
    return (row.get("director_first_name") or "").strip()


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


async def sweep(ts: Optional[Any] = None) -> Dict[str, int]:
    """Send whichever nudges are due. Returns ``{nudge: n, expiry: n}``.

    Never raises: a scheduler that can throw is a scheduler that stops. Every
    per-row failure is logged and the sweep carries on with the next row, because
    one physician's malformed address must not cost everyone else their reminder.
    """
    import asyncio  # noqa: PLC0415

    from email_utils import is_email_transport_configured  # noqa: PLC0415
    from team_store import get_team_store  # noqa: PLC0415

    sent = {"nudge": 0, "expiry": 0}
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
    if sent["nudge"] or sent["expiry"]:
        log.info("[nudge] sent %d nudges, %d expiry warnings", sent["nudge"], sent["expiry"])
    return sent
