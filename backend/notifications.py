"""Founder notifications: tell us when something happens on the product.

There was already an outbox doing this for exactly one event. ``signup`` alerts
went through ``admin_notify_outbox`` -- durable, idempotent on a key, with a
grace window and a retry counter, drained every 60s by a loop that already runs
-- and every other notable thing on the site either alerted nobody or sent inline
from a router, where a SendGrid blip loses it silently. This module is that
outbox with a front door wide enough for the rest.

Two properties worth keeping when editing:

**Never raise into a caller.** Every function here swallows. A notification is
the least important thing happening in any request that triggers one, and a
physician's case submission must not 500 because our mail queue had an opinion.

**Never carry PHI.** SendGrid is not PHI-eligible unless SENDGRID_BAA_SIGNED,
and these bodies are addressed to us, which makes them feel safe to fill with
detail. Alerts carry counts, names, ids and organizations. They do not carry
case content, note text, or anything a patient would recognise as theirs.

Coalescing is what ``send_after`` is for. A physician working through twelve
cases in an evening should produce one email, not twelve, so the case-submission
kinds land in a rollup window and share one idempotency key inside it; signups
and forms are immediate, because those are the ones you want to answer today.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger("asclepius.notifications")

#: Where the alerts go when nothing is configured. Both addresses already
#: existed as hardcoded `or`-defaults in three different routers; they are here
#: once instead, and any of the env vars below overrides them.
_DEFAULT_FOUNDER_EMAILS = "aryaabhatia@berkeley.edu,tejpatel@berkeley.edu"

#: Per-kind coalescing windows, in seconds. A kind absent from this map sends as
#: soon as the drainer next runs.
#:
#: The numbers are chosen by how you would want to be interrupted, not by
#: volume. A signup or a filled-in form is someone waiting on a reply, so it goes
#: now. Work events are a pulse: you want to know the network is labelling, and
#: you do not want a phone that buzzes every four minutes while it does.
_ROLLUP_SECONDS: Dict[str, int] = {
    "case_submitted": 900,      # 15 min
    "review_completed": 900,
    "qa_decision": 900,
    "referral_created": 300,
    "hs_upload": 300,
}


def _split_emails(raw: str) -> List[str]:
    return [e.strip() for e in (raw or "").split(",") if e.strip()]


def founder_recipients(store: Any = None) -> List[str]:
    """Who gets told. Explicit list, then the Asclepius list, then the bootstrap
    admin, then every active admin account, then the built-in default.

    The chain matters, and it is lifted from the one that already guarded signup
    alerts: a notification feature that silently no-ops because a single env var
    is unset is worse than not having one, because you believe it is working.
    """
    for var in ("FOUNDER_NOTIFY_EMAILS", "ASCLEPIUS_ADMIN_NOTIFY_EMAILS"):
        found = _split_emails(os.getenv(var) or "")
        if found:
            return found
    single = (os.getenv("ASCLEPIUS_ADMIN_EMAIL") or "").strip()
    if single:
        return [single]
    if store is not None:
        try:
            admins = [
                u["email"] for u in (store.list_users() or [])
                if u.get("role") == "admin" and u.get("active") and u.get("email")
            ]
            if admins:
                return admins
        except Exception:
            log.debug("notifications: admin lookup failed, falling back", exc_info=True)
    return _split_emails(_DEFAULT_FOUNDER_EMAILS)


def rollup_window_seconds(kind: str) -> int:
    return _ROLLUP_SECONDS.get(kind, 0)


def _rollup_bucket(kind: str, now: Optional[datetime] = None) -> str:
    """A stable label for the window this event falls in.

    Floor-to-window rather than first-event-wins: two events three seconds apart
    must land in the same bucket, and a bucket that started whenever the first
    one happened would need a read to find out when that was.
    """
    window = rollup_window_seconds(kind)
    now = now or datetime.now(timezone.utc)
    if window <= 0:
        return ""
    epoch = int(now.timestamp())
    return str(epoch - (epoch % window))


def _send_after_for(kind: str, now: Optional[datetime] = None) -> Optional[str]:
    window = rollup_window_seconds(kind)
    if window <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(seconds=window)).replace(microsecond=0, tzinfo=None).isoformat()


def notify_founders(
    store: Any,
    *,
    kind: str,
    subject: str,
    body_html: str,
    dedupe_key: str,
    recipients: Optional[Iterable[str]] = None,
    coalesce: bool = True,
) -> int:
    """Queue one alert per recipient. Returns how many rows were newly queued.

    ``dedupe_key`` identifies the EVENT, not the email: this function appends the
    recipient and, when the kind has a rollup window, the window. That is what
    makes a double-submit or a retried request produce one notification rather
    than two, and what makes twelve case submissions in an evening produce one
    email per window instead of twelve.

    Swallows everything. See the module docstring.
    """
    try:
        addrs = list(recipients) if recipients is not None else founder_recipients(store)
        if not addrs:
            log.warning("notifications: no recipients for kind=%s", kind)
            return 0
        bucket = _rollup_bucket(kind) if coalesce else ""
        send_after = _send_after_for(kind) if coalesce else None
        queued = 0
        for addr in addrs:
            raw = f"{kind}|{dedupe_key}|{bucket}|{addr.lower()}"
            # Hashed because the key is UNIQUE in SQLite and a caller is free to
            # pass something long or awkward; the readable part stays in `kind`.
            key = f"{kind}|{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"
            if store.enqueue_admin_notification(
                idempotency_key=key,
                kind=kind,
                subject=subject,
                body_html=body_html,
                recipient_email=addr,
                send_after=send_after,
            ):
                queued += 1
        return queued
    except Exception:
        # A failed notification must never be the reason a request fails.
        log.exception("notifications: could not queue kind=%s", kind)
        return 0
