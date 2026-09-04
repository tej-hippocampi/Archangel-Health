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
from typing import Any, Dict, FrozenSet, Iterable, List, Optional

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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _iso_in(seconds: int) -> Optional[str]:
    """A send_after stamp `seconds` from now, in the shape the outbox stores."""
    if seconds <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(
        microsecond=0, tzinfo=None).isoformat()


def _portal_base() -> str:
    """Same resolution order as routers/asclepius_verify.py, so a link in the
    approval mail and a link anywhere else point at the same host."""
    return (os.getenv("ASCLEPIUS_PORTAL_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")


def _alert_keys(kind: str, dedupe_key: str, addrs: List[str],
                coalesce: bool) -> List[tuple]:
    """(recipient, idempotency_key) for one logical alert.

    Split out so the amend path can recompute exactly the keys the enqueue path
    used. Two functions deriving the same key by eye is how a rollup ends up
    writing a second row instead of updating the first.
    """
    bucket = _rollup_bucket(kind) if coalesce else ""
    out = []
    for addr in addrs:
        raw = f"{kind}|{dedupe_key}|{bucket}|{addr.lower()}"
        # Hashed because the key is UNIQUE in SQLite and a caller is free to
        # pass something long or awkward; the readable part stays in `kind`.
        out.append((addr, f"{kind}|{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"))
    return out


def amend_founders(store: Any, *, kind: str, dedupe_key: str, subject: str,
                   body_html: str, recipients: Optional[Iterable[str]] = None,
                   coalesce: bool = True) -> int:
    """Rewrite an alert that has not been sent yet.

    This is what makes a rollup honest. Twelve cases in an evening collapse to
    one queued row, and without this that row would still say "a case was
    submitted" -- true of the first one and a lie about the batch. The row is
    rewritten in place while it is still pending, the same way the verification
    agent enriches a signup alert it queued a moment earlier.
    """
    try:
        addrs = list(recipients) if recipients is not None else founder_recipients(store)
        amended = 0
        for _addr, key in _alert_keys(kind, dedupe_key, addrs, coalesce):
            if store.update_pending_admin_notification(
                    key, subject=subject, body_html=body_html):
                amended += 1
        return amended
    except Exception:
        log.exception("notifications: could not amend kind=%s", kind)
        return 0


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
        send_after = _send_after_for(kind) if coalesce else None
        queued = 0
        for addr, key in _alert_keys(kind, dedupe_key, addrs, coalesce):
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


# ─── The member-facing door ──────────────────────────────────────────────────
# Two audiences, one outbox. ``admin_notify_outbox`` is a durable mailer with an
# idempotency key and a retrying drainer; it is not an admin-only table.
# ``notify_founders`` above is the internal front door and this is the member
# one. The no-PHI rule applies to both, and a physician's own name in a mail
# addressed to that physician is not PHI. What this must never carry is anything
# about ANOTHER person.

#: Kinds the drainer sends with high-importance headers. One physician-facing
#: lifecycle mail, not a category: an outbox where everything is urgent has no
#: urgency left in it.
IMPORTANT_KINDS = frozenset({"physician_approved"})

#: How long a rejection sits in the outbox before it goes. A rejection is
#: irreversible in somebody's inbox; half an hour buys back a misclick, and an
#: approval inside the window voids the pending row.
_REJECT_GRACE_SECONDS = _env_int("ASCLEPIUS_REJECT_GRACE_SECONDS", 1800)


def notify_person(store: Any, *, kind: str, to: str, subject: str, body_html: str,
                  dedupe_key: str, send_after: Optional[str] = None) -> int:
    """Queue ONE durable email to one member. Returns 1 when newly queued.

    Keys derived through ``_alert_keys`` like everything else, so the enqueue
    path and any later void cannot derive them differently by eye.
    ``coalesce=False``: a lifecycle decision is never rolled up with another one.

    Swallows everything. See the module docstring.
    """
    try:
        addr = (to or "").strip()
        if not addr:
            return 0
        for _, key in _alert_keys(kind, dedupe_key, [addr], False):
            if store.enqueue_admin_notification(
                idempotency_key=key, kind=kind, subject=subject,
                body_html=body_html, recipient_email=addr, send_after=send_after,
            ):
                return 1
        return 0
    except Exception:
        log.exception("notifications: could not queue member mail kind=%s", kind)
        return 0


def _password_is_unset(store: Any, user: Dict[str, Any]) -> bool:
    """True when this account has no way to sign in yet (Onboarding v2)."""
    try:
        from asclepius import store as _store_mod  # noqa: PLC0415
        return bool(_store_mod.password_is_unset(user))
    except Exception:
        # Unknown reads as "has a password", so the worst case is a notice that
        # points at a door rather than no notice at all.
        return False


def _person_key(kind: str, dedupe_key: str, addr: str) -> str:
    return _alert_keys(kind, dedupe_key, [addr], False)[0][1]


def on_verification_decision(store: Any, *, user: Optional[Dict[str, Any]],
                             status: str, tier: Optional[str] = None,
                             prior: Optional[str] = None) -> None:
    """Queue the physician's own copy of a verification decision.

    Called from ``store.record_verification_decision``, so the console, the
    agent's auto-approval and ``/admin/physicians/restore`` are all covered by
    one hook rather than by three call sites that each have to remember.

    Once-per-physician comes free from the UNIQUE idempotency key plus INSERT OR
    IGNORE: a retried request, a double-click, a restore re-stamp and a
    multi-worker race all produce one row. Documented consequence: an
    approve -> reject -> re-approve sequence sends no second approval mail. That
    is the right trade, because the alternative keys on a timestamp and loses
    idempotency across retries, which is the failure that actually happens.

    Never raises. Never sends: it queues.
    """
    try:
        if not user or status not in ("approved", "rejected"):
            return
        email = (user.get("email") or "").strip()
        if not email:
            return
        from asclepius import capabilities as _caps  # noqa: PLC0415
        from onboarding_emails import (  # noqa: PLC0415
            build_asclepius_approved_email, build_asclepius_rejected_email,
        )
        full_name = (user.get("full_name") or "").strip()

        if status == "approved":
            # Onboarding v2 made approval the moment credentials come into
            # existence: the wizard has no password step, so the console mints a
            # temporary password and sends a welcome carrying it, synchronously,
            # and tells the admin when that send failed. That design is right and
            # this does not replace it.
            #
            # What it covers is the two paths that still send nothing: the
            # verification agent's auto-approval and /admin/physicians/restore.
            # Under v2 those leave a physician with no mail AND no credential, so
            # they cannot sign in at all, and nothing anywhere says so.
            #
            # `mint_credentials` is the seam. When the account already has a
            # password there is nothing to issue and the plain approval notice is
            # the whole job; when it does not, the caller has to have issued one
            # before this runs, because a queued "your temporary password is ..."
            # with no password in it is worse than silence.
            if _password_is_unset(store, user):
                log.warning(
                    "notifications: %s approved with no credential; not queuing a "
                    "welcome it cannot carry", user.get("id"))
                return
            tier_word = _caps.tier_word(tier) if tier else ""
            notify_person(
                store, kind="physician_approved", to=email,
                subject="You're approved for Archangel Health",
                body_html=build_asclepius_approved_email(
                    full_name=full_name, workspace_url=_portal_base() + "/asclepius",
                    tier_word=tier_word,
                    can_review=(tier == getattr(_caps, "REVIEWER", "reviewer")),
                ),
                dedupe_key=f"approved:{user.get('id')}",
            )
            # An admin who rejects and then approves inside the grace window
            # must not deliver both.
            try:
                store.void_pending_admin_notification(
                    _person_key("physician_rejected", f"rejected:{user.get('id')}", email))
            except Exception:
                log.exception("notifications: could not void a pending rejection")
            return

        notify_person(
            store, kind="physician_rejected", to=email,
            subject="About your Archangel Health application",
            # The door back in. A rejection is now an invitation to re-sit the
            # case work, so the message needs somewhere to send them.
            body_html=build_asclepius_rejected_email(
                full_name=full_name, sign_in_url=_portal_base() + "/asclepius"),
            dedupe_key=f"rejected:{user.get('id')}",
            send_after=_iso_in(_REJECT_GRACE_SECONDS),
        )
    except Exception:
        log.exception("notifications: verification decision mail failed")


# ─── The event dispatch ──────────────────────────────────────────────────────
# What we actually want to hear about, and how it reads in an inbox.
#
# The list is deliberately short. A notification feature earns its keep by being
# worth reading; one that fires on everything gets filtered into a folder within
# a week, and then the two events that mattered are in the folder too.
#
# Each entry: event_type -> (kind, eyebrow, headline template, one-line lede).
# `{who}` and `{what}` are filled from the event's actor and payload.
_EVENT_ALERTS: Dict[str, Dict[str, str]] = {
    # Supply side: the network is working.
    "submission_completed": {
        "kind": "case_submitted", "eyebrow": "Labelling",
        "headline": "A case was submitted",
        "lede": "{who} finished a case.",
    },
    "review_submitted": {
        "kind": "review_completed", "eyebrow": "Review",
        "headline": "A review finished",
        "lede": "{who} reviewed a submitted case.",
    },
    "qa_approved": {
        "kind": "qa_decision", "eyebrow": "QA",
        "headline": "A case cleared QA",
        "lede": "{who} approved a case in QA.",
    },
    "qa_rejected": {
        "kind": "qa_decision", "eyebrow": "QA",
        "headline": "A case was rejected in QA",
        "lede": "{who} rejected a case in QA.",
    },
    "referral_invited": {
        "kind": "referral_created", "eyebrow": "Referral",
        "headline": "A physician referred someone",
        "lede": "{who} sent a referral invite.",
    },
    # Demand and partnership side: someone is waiting on a person.
    "self_signup_verified": {
        "kind": "hs_signup", "eyebrow": "Health system",
        "headline": "A health system signed itself up",
        "lede": "{what} confirmed its email and is waiting on a decision.",
    },
    "intake_submitted": {
        "kind": "hs_intake", "eyebrow": "Health system",
        "headline": "A health system told us about its data",
        "lede": "{who} filled in the questions.",
    },
    "portal_account_approved": {
        "kind": "hs_approved", "eyebrow": "Health system",
        "headline": "A health system was approved",
        "lede": "{who} opened uploading for a partner.",
    },
    # An upload is the moment a partnership becomes real, so it is worth an
    # interruption even though it is technically routine.
    "upload_received": {
        "kind": "hs_upload", "eyebrow": "Health system",
        "headline": "A health system uploaded data",
        "lede": "{what} sent us a file.",
    },
}


def notable_event_types() -> FrozenSet[str]:
    return frozenset(_EVENT_ALERTS)


def _clean(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def on_event(store: Any, *, entity_type: str, event_type: str,
             entity_id: Optional[str] = None, actor: Optional[str] = None,
             payload: Optional[Dict[str, Any]] = None) -> None:
    """Called for every logged event. Queues an alert for the few we care about.

    Cheap and total by design: the check is a dict lookup, so the ~140 event
    types nobody asked about cost one hash. Never sends mail, never raises.
    Alerts carry counts, names and identifiers only, never case content --
    SendGrid is not PHI-eligible and these bodies are the kind that invite
    detail.
    """
    spec = _EVENT_ALERTS.get(event_type)
    if not spec:
        return
    try:
        kind = spec["kind"]
        data = payload or {}
        who = _clean(actor, "Someone")
        what = _clean(data.get("organization") or data.get("org") or entity_id,
                      "A partner")
        window = rollup_window_seconds(kind)

        # The dedupe key is what decides whether a burst becomes one email.
        #
        # A rolled-up kind keys on the ACTOR and the window, deliberately
        # omitting the entity: twelve submissions by one physician are twelve
        # entity ids, and keying on those produced twelve identical emails,
        # which is exactly what the rollup existed to prevent. Everything else
        # keys on the entity, because two health systems signing up are two
        # things you need to see.
        if window > 0:
            dedupe_key = f"{event_type}:{who}"
        else:
            dedupe_key = f"{entity_type}:{entity_id or ''}:{who}"

        count = 1
        if window > 0:
            since = (datetime.now(timezone.utc) - timedelta(seconds=window)) \
                .replace(microsecond=0, tzinfo=None).isoformat()
            count = max(1, store.count_events_since(
                event_type=event_type, actor=actor, since_iso=since))

        headline = spec["headline"]
        lede = spec["lede"].format(who=who, what=what)
        if count > 1:
            headline = _plural_headline(spec, count)
            lede = f"{who} has completed {count} in the last few minutes."

        rows = [(k.replace("_", " ").title(), str(v), False)
                for k, v in sorted(data.items())
                # Free text in a payload is where case content would leak in;
                # short scalars are ids, counts and flags.
                if v is not None and len(str(v)) <= 120]
        rows.insert(0, ("Event", event_type, True))
        if entity_id and count == 1:
            rows.insert(1, (entity_type.replace("_", " ").title(), str(entity_id), True))
        if count > 1:
            rows = [("Event", event_type, True), ("How many", str(count), True)]

        from onboarding_emails import build_founder_event_alert
        body = build_founder_event_alert(
            eyebrow=spec["eyebrow"], headline=headline, lede=lede, rows=rows,
            note=("Queued automatically when this happened on the product. "
                  "Nobody is waiting on a reply to this message."))
        subject = f"[Archangel] {headline}"

        queued = notify_founders(store, kind=kind, subject=subject, body_html=body,
                                 dedupe_key=dedupe_key)
        # Already queued for this window, so rewrite it rather than adding a
        # second: the row it wrote a minute ago says "a case", and by now it is
        # several.
        if not queued and count > 1:
            amend_founders(store, kind=kind, dedupe_key=dedupe_key,
                           subject=subject, body_html=body)
    except Exception:
        log.exception("notifications: could not handle event %s", event_type)


def _plural_headline(spec: Dict[str, str], count: int) -> str:
    """Turn "A case was submitted" into "3 cases were submitted".

    Table-driven rather than clever: an English pluraliser guessing at our own
    nine headlines is more ways to be wrong than writing them down.
    """
    plural = _PLURAL_HEADLINES.get(spec["headline"])
    return plural.format(n=count) if plural else f"{spec['headline']} ({count})"


#: The rolled-up wording for each headline that can roll up.
_PLURAL_HEADLINES: Dict[str, str] = {
    "A case was submitted": "{n} cases were submitted",
    "A review finished": "{n} reviews finished",
    "A case cleared QA": "{n} cases cleared QA",
    "A case was rejected in QA": "{n} cases were rejected in QA",
    "A physician referred someone": "{n} referrals were sent",
    "A health system uploaded data": "{n} uploads arrived",
}
