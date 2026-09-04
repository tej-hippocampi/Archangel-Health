"""Earnings + billable session router (PRD-P §6).

Every doctor-facing endpoint here scopes from the SESSION. No doctor-facing route
takes a ``user_id`` in its path, query or body — a physician can read their own
ledger and nobody else's, and that is a property of the routes rather than of a
check somebody remembered to write.

``user_id`` appears on exactly two routes, both under ``/admin/`` and both gated
on ``require_admin``: the ledger view and the disbursement record. That split is
the design — naming another physician is an administrative act, so it happens only
where an administrator is proven to be doing it.

Policy lives in ``asclepius.payments``; persistence in the PRD-P sentinel block of
``asclepius.store``. This router never touches ``routers/asclepius.py``, and
imports nothing from ``review.py`` or ``routing.py``.
"""

# DISBURSEMENT SEAM. This records that we consider these rows settled; it does
# not move money. The rail will be Stripe Connect Express: physicians onboard
# themselves, Stripe holds bank details and tax ids and files the 1099-NECs.
# Nothing in this file should ever store a bank account number or a tax id — if
# a change wants to, that is the signal it belongs behind Connect instead.

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException,
                     Query, Request, Response)
from pydantic import BaseModel, EmailStr, Field, field_validator

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius import payments as asc_payments
from asclepius import referrals as asc_referrals
from asclepius.store import get_store
from ratelimit import rate_limiter

log = logging.getLogger("asclepius.payments.router")

router = APIRouter(tags=["asclepius-payments"])


def _store():
    return get_store()


def _require_money_movement(
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.EARNINGS)),
) -> Dict[str, Any]:
    """Holds the earnings SURFACE, and may also transact against it.

    An applicant under review can now open the Earnings page. Seeing how they
    will be paid before deciding whether to do the work is reasonable, and the
    page reads zero, which is honest.

    Opening a BILLABLE SESSION is a different act, and the surface was carrying
    both permissions at once. A session accrues paid minutes, so an account
    nobody has verified could have started one by hand against endpoints that
    only ever asked "do you hold EARNINGS". The client never called them, which
    is why it was invisible: the rail did not show the control, and an
    entitlement that relies on the UI not offering it is not an entitlement.

    Admins are exempt for the same reason they are exempt everywhere else in
    this router: they operate the thing.
    """
    if user.get("role") == "admin":
        return user
    if asc_caps.access_level(user) == asc_caps.PROVISIONAL:
        raise HTTPException(
            status_code=403,
            detail="This opens when your credentials are approved.",
            headers={asc_auth.AUTH_GATE_HEADER: "pending"},
        )
    return user


# ─── Earnings ─────────────────────────────────────────────────────────────────
@router.get("/api/asclepius/earnings")
async def my_earnings(user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.EARNINGS))):
    """Summary + recent ledger for the signed-in physician. Own rows only —
    the user id comes from the token and from nowhere else."""
    return asc_payments.earnings_summary(_store(), user_id=user["id"])


# ─── Referrals (PRD-REF §3) ───────────────────────────────────────────────────
# Both routes are SESSION-SCOPED and take no id of any kind: a physician reads
# their own funnel and nobody else's, and that is a property of the route shape
# rather than of a check somebody remembered to write. They live on the payments
# router because the earnings surface is where the referral card renders and
# because the bounty is a ledger row; the POLICY lives in ``asclepius.referrals``
# and the advisor router calls the same functions.
def _require_referrer(
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.REFERRAL)),
) -> Dict[str, Any]:
    """Any physician with a live account, including one still under review.

    Not a tier literal — see ``referrals.can_refer``. Gated on the REFERRAL
    surface rather than EARNINGS because referring is not a money surface: the
    ledger is, and a physician can hold a link long before they hold a balance.
    This is still the second gate, at the boundary that sends mail to a third
    party.
    """
    if not asc_referrals.can_refer(user):
        raise HTTPException(
            status_code=403,
            detail="Referrals are for approved physicians. If you believe this is "
                   "wrong, contact your workspace admin.")
    return user


@router.get("/api/asclepius/referrals")
async def my_referrals(user: Dict[str, Any] = Depends(_require_referrer)):
    """This physician's own funnel. No id parameter exists to tamper with."""
    store = _store()
    try:
        asc_payments.reconcile_referral_bounties(store, referrer_id=user["id"])
    except Exception:
        # The funnel that already exists is still the truth. Never a 500 on the
        # surface whose entire job is to prove the referral did not vanish.
        log.exception("asclepius.payments: referral reconcile failed for %s", user["id"])
    referrer = store.get_user_by_id(user["id"]) or user
    payload = asc_referrals.funnel(
        store, referrer=referrer,
        bounty_cents=asc_payments.referral_bounty_cents(),
        referee_bonus_cents=asc_payments.referee_bonus_cents(),
        cap_cents=asc_payments.referral_cap_cents())
    # Both funnels in ONE response. The tab renders two columns from a single
    # fetch, so a health-system row cannot appear a beat after the physician
    # rows and read as a glitch. Degrades to an empty list rather than a 500:
    # this surface exists to prove a referral did not vanish, and it must not be
    # the thing that vanishes.
    try:
        payload["health_systems"] = asc_referrals.hs_funnel(store, referrer=referrer)
    except Exception:
        log.exception("asclepius.payments: hs funnel failed for %s", user["id"])
        payload["health_systems"] = []
    return payload


class ReferralBody(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    note: Optional[str] = None


def _throttle_referral(user: Dict[str, Any]) -> None:
    """Per-USER and fleet-wide limits (defect 1). Raises 429.

    Keyed on ``user["id"]``, never on the IP. A hospital NATs — the eleventh
    referral out of one building would get a 429 while the actual threat, a
    stolen token rotated across a proxy pool, went unthrottled. The IP limit on
    the route below stays as a cheap outer wall; this is the one that means
    anything.
    """
    from ratelimit import check, is_enabled

    if not is_enabled():
        return
    for key, (limit, window) in asc_referrals.throttle_keys(user["id"]):
        allowed, retry_after = check(key, limit, window)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many invitations sent recently. Try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )


@router.post(
    "/api/asclepius/referrals",
    dependencies=[Depends(rate_limiter("asclepius_referral_ip", 60, 600))],
)
async def create_referral(
    body: ReferralBody,
    user: Dict[str, Any] = Depends(_require_referrer),
):
    """Refer a colleague.

    **The response is byte-identical whether or not the address already has an
    account** (defect 2). The advisor path narrowed its oracle to physician
    accounts, which closed the worst version of it, but it still answered "does
    this doctor have an account here?" one address at a time to anyone who could
    call it — and generalising the endpoint to every physician multiplies who
    that is. The referral is recorded either way and the FUNNEL reports the
    outcome, which loses the referrer nothing: they see the row on their own
    page, where the answer is about their own referral rather than about a
    stranger's account.

    **The residual, stated rather than hidden:** a new address sends an email and
    an existing member does not, so the two paths differ in RESPONSE TIME by a
    SendGrid round trip. That is a real side channel and it is left in place
    knowingly — closing it means either firing the send blind (losing the
    delivery signal and every send error with it) or padding the fast path, both
    of which cost more than they buy against a probe that is already bounded to
    20 attempts a day per account and 3 per address across the whole fleet.
    """
    _throttle_referral(user)
    store = _store()
    referrer = store.get_user_by_id(user["id"]) or user
    try:
        result = asc_referrals.create_referral(
            store, referrer=referrer, email=str(body.email),
            name=body.name, note=body.note)
    except asc_referrals.ReferralRefused as refused:
        store.log_event(
            entity_type="user", entity_id=referrer["id"],
            event_type="referral_refused", actor=referrer.get("email"),
            payload={"reason": refused.code},
        )
        raise HTTPException(status_code=refused.status, detail=refused.detail)

    sent = False
    if result["outcome"] == asc_referrals.OUTCOME_INVITED:
        sent = await asc_referrals.send_invite(
            referrer=referrer, email=str(body.email).lower().strip(),
            name=body.name, code=result["referral_code"])
    # An existing member already has an account, so their first task may already
    # be approved — settle immediately rather than making the referrer wait for a
    # sweep that has nothing left to trigger it.
    if result.get("invitee_user_id"):
        try:
            asc_payments.accrue_referral_bounty(
                store, referred_user_id=result["invitee_user_id"])
        except Exception:
            log.exception("asclepius.payments: immediate bounty settle failed")

    store.log_event(
        entity_type="user", entity_id=referrer["id"], event_type="referral_invited",
        actor=referrer.get("email"),
        payload={"referral_id": (result.get("referral") or {}).get("referral_id"),
                 # The outcome is recorded for US, and deliberately not returned.
                 "outcome": result["outcome"], "email_sent": sent},
    )

    # ONE response, always — the same KEYS and the same VALUES. No `already`, no
    # `email_sent`, and deliberately not the funnel either: returning the funnel
    # here would put the new row's status ("Invited" vs "Signed up") in the same
    # response and move the oracle rather than close it. The client refetches
    # ``GET /referrals``, where the answer is about the referrer's own funnel a
    # request later and behind the same per-user throttle.
    return {
        "ok": True,
        "message": "Invitation recorded. You'll see them in your referrals below.",
    }


class EnterpriseNoteBody(BaseModel):
    note: str


@router.post(
    "/api/asclepius/referrals/enterprise-note",
    dependencies=[Depends(rate_limiter("asclepius_enterprise_note_ip", 10, 3600))],
)
async def enterprise_note(
    body: EnterpriseNoteBody,
    user: Dict[str, Any] = Depends(_require_referrer),
):
    """A physician flags that their health system might sell data or partner
    on enterprise labeling. Free text straight to a founder inbox: at this
    deal size a human reads every word, so no form fields, no CRM.

    Reachable by a physician still under review, which matters because of who
    sends these: the person who can open an institutional door is very often
    the one who just walked through it, and the week they join is the week they
    are most willing to make the introduction.

    Bounded and throttled per user (3/day) because it is an outbound email a
    signed-in physician can trigger; the note is plain text in the builder so
    nothing in it can inject markup or headers.
    """
    note = " ".join((body.note or "").split())
    if not note:
        raise HTTPException(status_code=422, detail="Write a sentence or two first.")
    if len(note) > 2000:
        raise HTTPException(status_code=422, detail="Keep the note under 2,000 characters.")
    from ratelimit import check, is_enabled
    if is_enabled():
        allowed, retry_after = check(f"asclepius_enterprise_note:{user['id']}", 3, 86400)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="You have sent a few notes today already. We read every one.",
                headers={"Retry-After": str(retry_after)})

    from email_utils import is_email_transport_configured, send_html_email
    from onboarding_emails import build_enterprise_note_email

    store = _store()
    sender = store.get_user_by_id(user["id"]) or user
    dest = (os.getenv("ENTERPRISE_NOTE_EMAIL") or "aryaabhatia@berkeley.edu").strip()
    sent = False
    if is_email_transport_configured():
        try:
            sent = bool(await send_html_email(
                dest,
                f"Enterprise note from {(sender.get('full_name') or 'a physician').strip()}",
                build_enterprise_note_email(
                    sender_name=(sender.get("full_name") or "").strip(),
                    sender_email=(sender.get("email") or "").strip(),
                    specialty=(sender.get("specialty") or "").strip(),
                    organization=(sender.get("organization") or sender.get("org_name") or "").strip(),
                    note=note,
                )))
        except Exception:
            log.exception("asclepius.payments: enterprise note email failed")
    store.log_event(
        entity_type="user", entity_id=sender["id"], event_type="enterprise_note",
        actor=sender.get("email"), payload={"sent": sent, "chars": len(note)})
    if not sent:
        # The note did not leave the building; say so rather than swallowing it.
        raise HTTPException(
            status_code=503,
            detail="We could not send your note just now. Please try again shortly.")
    return {"ok": True,
            "message": "Sent. A founder reads every one of these personally."}


# ─── Health-system referrals (HS-REF) ─────────────────────────────────────────
class HealthSystemReferralBody(BaseModel):
    contact_name: str = Field(min_length=1, max_length=120)
    contact_email: EmailStr
    hs_name: str = Field(min_length=1, max_length=160)
    relationship: str = Field(min_length=1, max_length=400)
    contact_role: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=2000)

    @field_validator("contact_name", "hs_name", "relationship", mode="after")
    @classmethod
    def _must_carry_content(cls, v: str) -> str:
        """Collapse whitespace, then require something is left.

        ``min_length=1`` alone counts ``"   "`` as three characters, so a form
        submitted with a spacebar in the name field validated cleanly, and the
        row it wrote had a blank contact name that reached a founder's alert as
        "Not given" and greeted the recipient as "there". Validate the content,
        not the length of the string.
        """
        collapsed = " ".join((v or "").split())
        if not collapsed:
            raise ValueError("This field cannot be blank.")
        return collapsed
    #: Not a formality. We send this email in the physician's name, with their
    #: address on the reply-to, so the claim being made to the recipient is that
    #: a person they know asked us to write. A checkbox is the cheapest place to
    #: make the physician assert that is true before we say it on their behalf.
    consent: bool = False


def _hs_referral_send_enabled() -> bool:
    """Kill switch for the outbound leg.

    Set ``ASCLEPIUS_HS_REFERRAL_SEND_ENABLED=0`` and referrals are still
    recorded and still alert a founder, only the mail to the third party stops.
    Deliberately a degrade rather than a 503: if something is wrong with what we
    are sending, the lead a physician just handed us is the last thing that
    should be lost while it gets fixed.
    """
    return (os.getenv("ASCLEPIUS_HS_REFERRAL_SEND_ENABLED") or "1").strip() not in ("0", "false", "no")


async def _deliver_hs_referral(hs_referral_id: str, referrer_id: str) -> None:
    """Enrich, then send, then tell a founder. Runs AFTER the response.

    Everything here is best-effort by construction. The row is already written
    and the landing token already minted before this is scheduled, so a failure
    anywhere below costs the introduction its email, never its record, a
    founder can still pick it up out of the alert or the admin list. This
    function never raises into the background-task runner.
    """
    from asclepius import hs_enrich
    from email_utils import is_email_transport_configured, send_html_email
    from onboarding_emails import (
        build_hs_referral_alert_email, build_hs_referral_intro_email,
        hs_referral_subject,
    )

    store = _store()
    row = store.get_hs_referral(hs_referral_id)
    if not row:
        return
    referrer = store.get_user_by_id(referrer_id) or {}
    referrer_name = asc_referrals.referrer_display_name(referrer)

    enrichment: Dict[str, Any] = {"state": "skipped", "data": None, "reason": "not_run"}
    try:
        enrichment = await hs_enrich.enrich_health_system(
            contact_name=row.get("contact_name") or "",
            contact_role=row.get("contact_role") or "",
            hs_name=row.get("hs_name") or "",
        )
    except Exception:  # noqa: BLE001, enrichment must never cost the send
        log.exception("hs-referral: enrichment raised for %s", hs_referral_id)

    data = enrichment.get("data")
    try:
        store.set_hs_referral_enrichment(
            hs_referral_id, state=enrichment.get("state") or "skipped",
            payload=hs_enrich.to_json(data))
    except Exception:
        log.exception("hs-referral: could not stamp enrichment for %s", hs_referral_id)

    blocked = enrichment.get("state") == "blocked"

    # Already ours. Checked here rather than at capture so the referrer is told
    # nothing about who we work with (see ``hs_contact_is_known``), while the
    # partner is spared an introduction to a company they already have a login
    # for. Treated as a block: nothing sends, a founder picks it up.
    known = False
    if not blocked:
        try:
            known = store.hs_contact_is_known(row.get("contact_email") or "")
        except Exception:
            log.exception("hs-referral: known-contact check failed for %s", hs_referral_id)
    blocked = blocked or known

    personalized = (not blocked) and hs_enrich.may_personalize(data)
    fact = (data or {}).get("one_public_fact") if personalized else ""

    outcome = ("nothing (already a partner)" if known
               else "nothing (blocked)" if blocked
               else "personalized introduction" if personalized
               else "clean introduction")
    sent = False
    if not blocked and _hs_referral_send_enabled() and is_email_transport_configured():
        try:
            html_body = build_hs_referral_intro_email(
                contact_first_name=(row.get("contact_name") or "").split(" ")[0],
                contact_role=row.get("contact_role") or "",
                hs_name=row.get("hs_name") or "",
                referrer_name=referrer_name,
                referrer_specialty=(referrer.get("specialty") or "").strip(),
                relationship=row.get("relationship") or "",
                partner_url=asc_referrals.partner_url(
                    row.get("referral_code"), row.get("landing_token")),
                enrichment_sentence=fact or "",
            )
            sent = bool(await send_html_email(
                row.get("contact_email") or "",
                # Collapsed through header_safe: this string is assembled from a
                # user-controlled display name and goes into a MIME header, where
                # a CR/LF is header injection on the SMTP fallback.
                asc_referrals.header_safe(hs_referral_subject(referrer_name)),
                html_body,
                reply_to=(referrer.get("email") or "").strip() or None,
            ))
        except Exception:
            log.exception("hs-referral: intro email failed for %s", hs_referral_id)
    elif not blocked and not _hs_referral_send_enabled():
        outcome = "nothing (sending disabled)"

    if sent:
        store.stamp_hs_referral_sent(hs_referral_id)
    if blocked:
        try:
            store.set_hs_referral_fraud_flag(
                hs_referral_id,
                ("already_a_partner" if known
                 else (enrichment.get("reason") or "blocked"))[:300])
        except Exception:
            log.exception("hs-referral: could not flag %s", hs_referral_id)

    # The founder alert goes last and is sent whatever happened above. The two
    # cases a human most needs to see are the blocked one and the failed send,
    # and both of those are exactly when the earlier steps did not complete.
    if is_email_transport_configured():
        try:
            summary = ""
            if data:
                summary = (f"confidence {data.get('confidence')}, "
                           f"role_confirmed {data.get('role_confirmed')}, "
                           f"org {data.get('org_type') or 'unknown'}")
            if known:
                summary = ("This address already belongs to a health system we work "
                           "with, so nothing was sent. " + summary).strip()
            elif blocked:
                summary = (enrichment.get("reason") or "") + (f" ({summary})" if summary else "")
            await send_html_email(
                (os.getenv("ENTERPRISE_NOTE_EMAIL") or "aryaabhatia@berkeley.edu").strip(),
                asc_referrals.header_safe(
                    f"Health-system referral: {row.get('hs_name') or 'unknown'}"),
                build_hs_referral_alert_email(
                    referrer_name=referrer_name or "Not given",
                    referrer_email=(referrer.get("email") or "").strip(),
                    contact_name=row.get("contact_name") or "",
                    contact_email=row.get("contact_email") or "",
                    contact_role=row.get("contact_role") or "",
                    hs_name=row.get("hs_name") or "",
                    relationship=row.get("relationship") or "",
                    note=row.get("note") or "",
                    enrich_state=enrichment.get("state") or "skipped",
                    enrich_summary=summary,
                    outcome=outcome + (" (delivered)" if sent else " (not delivered)"),
                ))
        except Exception:
            log.exception("hs-referral: founder alert failed for %s", hs_referral_id)

    try:
        store.log_event(
            entity_type="user", entity_id=referrer_id,
            event_type="hs_referral_delivered", actor=referrer.get("email"),
            payload={"hs_referral_id": hs_referral_id, "enrich_state": enrichment.get("state"),
                     "personalized": personalized, "blocked": blocked, "email_sent": sent})
    except Exception:
        log.exception("hs-referral: event log failed for %s", hs_referral_id)


@router.post(
    "/api/asclepius/referrals/health-system",
    dependencies=[Depends(rate_limiter("asclepius_hs_referral_ip", 20, 3600))],
)
async def create_hs_referral(
    body: HealthSystemReferralBody,
    background: BackgroundTasks,
    user: Dict[str, Any] = Depends(_require_referrer),
):
    """Introduce a named person at a health system.

    **Why this endpoint returns before the email is sent.** Enrichment is a
    model call with a web search inside it, and the physician who just pressed a
    button should not watch a spinner for however long that takes. The row and
    its landing token are written synchronously, so the introduction exists and
    is attributable the moment this returns, and delivery is scheduled after.

    **The response does not disclose whether we already know this organization.**
    Same rule as ``create_referral`` above: the message is the same either way,
    and the physician's own funnel reports the real outcome a request later.
    """
    _throttle_referral(user)

    if not body.consent:
        raise HTTPException(
            status_code=422,
            detail="Please confirm you know this person and they're OK hearing from us.")

    contact_email = str(body.contact_email).lower().strip()
    store = _store()
    referrer = store.get_user_by_id(user["id"]) or user

    # Stricter than the physician path, on purpose. That invite is templated;
    # this one carries the referrer's chosen name, their free-text description
    # of a relationship, and their Reply-To, delivered cold to somebody senior
    # at an organization we want to do business with. An account nobody has
    # vetted yet does not get to make the platform speak in that voice: an
    # OTP signup is possession of an inbox, not an identity. The under-review
    # physician keeps their referral link and the physician invite; this one
    # door waits for approval.
    #
    # Gated on the ACCESS LEVEL rather than on `verification_status ==
    # 'approved'` literally. Those are not the same test: a NULL status means
    # an account that predates the review queue, which `access_level` has
    # always read as FULL, and a literal comparison would quietly strip the
    # health-system introduction from every physician who joined before
    # verification existed.
    if (referrer.get("role") != "admin"
            and asc_caps.access_level(referrer) != asc_caps.FULL):
        raise HTTPException(
            status_code=403,
            detail="Health-system introductions unlock once your account is approved.")

    # Self-referral. Not a fraud control so much as a coherence one: an email
    # telling you that you suggested we reach out to yourself is nonsense, and
    # the physician path already refuses the same shape.
    if contact_email and contact_email == (referrer.get("email") or "").lower().strip():
        raise HTTPException(
            status_code=422, detail="That's your own address.")

    # Per-CONTACT cap, across all referrers. Without it one inbox can be mailed
    # without bound by rotating who submits it. That is the reasoning behind
    # REFERRALS_PER_INVITEE_24H on the physician path, and it matters more here
    # because this address belongs to somebody senior at a company we want to
    # do business with.
    from ratelimit import is_enabled
    if is_enabled():
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        if store.count_hs_referrals_for_contact(contact_email, since_iso=since) >= 2:
            raise HTTPException(
                status_code=429,
                detail="Someone has already introduced this contact recently. "
                       "We'll be in touch with them shortly.")

    row = store.insert_hs_referral(
        referrer_id=referrer["id"],
        referral_code=referrer.get("referral_code"),
        contact_name=body.contact_name,
        contact_email=contact_email,
        contact_role=body.contact_role,
        hs_name=body.hs_name,
        relationship=body.relationship,
        note=" ".join((body.note or "").split()) or None,
    )

    store.log_event(
        entity_type="user", entity_id=referrer["id"], event_type="hs_referral_created",
        actor=referrer.get("email"),
        payload={"hs_referral_id": row["hs_referral_id"], "hs_name": row["hs_name"]})

    background.add_task(_deliver_hs_referral, row["hs_referral_id"], referrer["id"])

    return {
        "ok": True,
        "message": "Introduction recorded. We'll reach out and you'll see it below.",
    }


@router.get(
    "/api/asclepius/hs-referral/{token}",
    dependencies=[Depends(rate_limiter("asclepius_hs_referral_lookup", 60, 3600))],
)
async def hs_referral_prefill(token: str):
    """What the /partner page needs to prefill itself. PUBLIC and unauthenticated.

    **This endpoint returns nothing the holder of the token was not already
    sent.** The token arrives in the link inside their own introduction email,
    and everything below, their name, their address, their organization, their
    role, and the first name of the physician who introduced them, was in that
    email's body. The whole point is to save a CIO from retyping four fields we
    already know; it is not a lookup service.

    Specifically NOT returned: the referrer's email address, their note, our
    enrichment, and every other row on ``hs_referrals``. Built by whitelist.

    **An unknown token is a 200 with ``found: false``, not a 404.** A 404 makes
    this a membership oracle, feed it tokens and the status code tells you
    which ones are live. The page renders an ordinary empty form either way,
    which is exactly what it should do when someone follows a stale link.

    ``status`` is advanced to ``opened`` as a side effect, which is what makes
    the referring physician's funnel row move on its own.
    """
    store = _store()
    row = store.get_hs_referral_by_token(token)
    if not row:
        return {"found": False}

    try:
        store.advance_hs_referral(row["hs_referral_id"], "opened")
    except Exception:
        # A funnel stamp is not worth failing the prefill the recipient is
        # waiting on.
        log.exception("hs-referral: could not stamp opened for %s", row.get("hs_referral_id"))

    referrer = store.get_user_by_id(row.get("referrer_id") or "") or {}
    referrer_name = asc_referrals.referrer_display_name(referrer)
    return {
        "found": True,
        "contact_name": row.get("contact_name") or "",
        "contact_email": row.get("contact_email") or "",
        "contact_role": row.get("contact_role") or "",
        "hs_name": row.get("hs_name") or "",
        # First name only. The full display name was in the email; a surname is
        # not needed to say who sent you, and this response is reachable by
        # anyone holding the link.
        "referrer_first_name": (referrer_name or "").split(" ")[0],
    }


# ─── Sessions ─────────────────────────────────────────────────────────────────
class OpenSessionBody(BaseModel):
    kind: str = Field(default=asc_payments.SESSION_KIND_REVIEW)


@router.post(
    "/api/asclepius/sessions",
    dependencies=[Depends(rate_limiter("asclepius_session_open", 60, 600))],
)
async def open_session(
    body: OpenSessionBody,
    user: Dict[str, Any] = Depends(_require_money_movement),
):
    """Open (or resume) a billable session. Idempotent — a client that opens twice
    gets the same session back, with the nonce it must beat with."""
    kind = (body.kind or "").strip().lower()
    if kind != asc_payments.SESSION_KIND_REVIEW:
        # One kind exists today. Rejecting anything else keeps a typo from
        # minting a whole parallel class of billable session nobody priced.
        raise HTTPException(status_code=422, detail="Unsupported session kind")
    store = _store()
    try:
        return asc_payments.open_session(store, user_id=user["id"], kind=kind)
    except asc_payments.PaymentsDenied as denied:
        # Recorded, not just refused. If a labeler's client is calling this it is
        # a bug in someone's code and we want to see it; if it is not a client at
        # all, that is worth knowing about long before a payout run.
        log.warning("asclepius.payments: session open denied for %s (%s)",
                    user["id"], denied.reason)
        store.log_event(
            entity_type="work_session", entity_id=None,
            event_type="session_open_denied", actor=user["id"],
            payload={"reason": denied.reason, "kind": kind,
                     "role": user.get("role")},
        )
        raise HTTPException(status_code=403, detail=denied.detail)


class HeartbeatBody(BaseModel):
    nonce: str
    seq: Optional[int] = None
    active: bool = True
    progress_key: Optional[str] = None
    # Recorded as a FRAUD SIGNAL and never used in any calculation (PRD-P §1.3).
    client_ts: Optional[str] = None


# Beating is legitimate high-frequency traffic — a 15 s beat is 4 requests a
# minute — and is rate-limited generously enough that several tabs and a retry
# storm on a shared hospital NAT stay well inside it.
#
# Resuming is the opposite: it hands out a beating credential, so it gets a
# budget that a page reload fits inside and a script does not. The audit's bot
# needed ~1.3 credentials a minute; this allows 0.4. Named rather than inlined so
# ``test_payments_nonce.py`` can assert the RELATIONSHIP between the two rather
# than two magic numbers that could drift apart unnoticed.
HEARTBEAT_RATE_LIMIT = (600, 60)
RESUME_RATE_LIMIT = (4, 600)


@router.post(
    "/api/asclepius/sessions/{session_id}/heartbeat",
    dependencies=[Depends(rate_limiter("asclepius_session_beat", *HEARTBEAT_RATE_LIMIT))],
)
async def session_heartbeat(
    session_id: str,
    body: HeartbeatBody,
    user: Dict[str, Any] = Depends(_require_money_movement),
):
    result = asc_payments.heartbeat(
        _store(), session_id=_owned(session_id, user), nonce=body.nonce,
        active=body.active, progress_key=body.progress_key, seq=body.seq,
        client_ts=body.client_ts,
    )
    if not result.get("ok"):
        code = result.get("error")
        if code == "not_found":
            raise HTTPException(status_code=404, detail="Session not found")
        if code in asc_payments._BEAT_MALFORMED:
            # The client sent something incomplete — a beat with no progress key,
            # or no sequence number on a session that already has beats. Retrying
            # it unchanged will never work, so this is a 422 and not a 409.
            raise HTTPException(
                status_code=422, detail=result.get("message") or "Heartbeat rejected")
        # A stale nonce or a replayed seq is a CONFLICT, not a bad request: the
        # beat was well-formed and the client simply lost the race or replayed.
        # The client re-opens rather than retrying the same beat forever.
        raise HTTPException(status_code=409, detail=result.get("message") or "Heartbeat rejected")
    return result


class CloseSessionBody(BaseModel):
    reason: str = Field(default=asc_payments.END_CLOSED)


@router.post("/api/asclepius/sessions/{session_id}/close")
async def close_session(
    session_id: str,
    body: CloseSessionBody,
    user: Dict[str, Any] = Depends(_require_money_movement),
):
    """Close a session and settle it. Safe to call repeatedly — and it will be,
    because the client closes on both ``visibilitychange`` and ``pagehide``."""
    result = asc_payments.close_session(
        _store(), session_id=_owned(session_id, user), reason=body.reason)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="Session not found")
    return result


@router.post(
    "/api/asclepius/sessions/{session_id}/resume",
    dependencies=[Depends(rate_limiter("asclepius_session_resume", *RESUME_RATE_LIMIT))],
)
async def resume_session(
    session_id: str,
    user: Dict[str, Any] = Depends(_require_money_movement),
):
    """Re-issue a beating credential to a client that legitimately lost one — a
    physician who reloaded the page mid-session.

    This is the ONLY route besides creating a session and beating on one that
    returns a nonce, and it is deliberately expensive: hard rate limit, ownership
    check, and a counter on the session row. ``open_session`` used to do this job
    for free and unlimited, which turned its idempotence into a nonce dispenser."""
    try:
        return asc_payments.resume_session(
            _store(), session_id=session_id, user_id=user["id"])
    except asc_payments.PaymentsDenied as denied:
        # A session that is not this user's and a session that does not exist get
        # the same 404, so the route cannot enumerate anyone else's sessions.
        if denied.reason == "ended":
            raise HTTPException(status_code=409, detail=denied.detail)
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api/asclepius/sessions/{session_id}")
async def session_state(
    session_id: str,
    user: Dict[str, Any] = Depends(_require_money_movement),
):
    """Server-authoritative state, for a client that reloaded mid-session and
    needs the truth back — including a fresh nonce to resume beating with."""
    state = asc_payments.session_state(_store(), session_id=session_id, user_id=user["id"])
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return state


def _owned(session_id: str, user: Dict[str, Any]) -> str:
    """Ownership check on every session route.

    A session id is a guessable-shaped opaque string, and without this a physician
    could beat on — and close, and be credited for — a session belonging to
    somebody else. A session that exists but belongs to another user returns the
    same 404 as one that does not exist, so the endpoint cannot be used to
    enumerate other people's sessions."""
    row = _store().get_work_session(session_id)
    if row is None or row.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")
    return session_id


# ─── Admin ────────────────────────────────────────────────────────────────────
@router.get("/api/asclepius/admin/earnings")
async def admin_earnings(
    user_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    payout_batch_id: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The whole ledger, filterable. Reconciles first so the admin view and the
    doctor's view are never two different answers to the same question."""
    store = _store()
    try:
        asc_payments.reconcile_task_accruals(store)
    except Exception:
        log.exception("asclepius.payments: admin reconciliation failed; serving the ledger as-is")
    if status is not None and status not in asc_payments.LEDGER_STATES:
        raise HTTPException(status_code=422, detail="Unknown status filter")
    rows = store.list_earnings(user_id=user_id, status=status,
                               payout_batch_id=payout_batch_id, limit=limit)
    if user_id:
        # Scoped to ONE physician, so the per-row lookups are bounded by that
        # doctor's own ledger. Not done for the whole company: it would be a
        # query per row over a table that only grows.
        _enrich_case_context(store, rows)
    return {
        "rows": rows,
        "totals": store.earnings_by_status(),
        # Admin Launch PRD §4.2 level 1: outstanding per physician, aggregated in
        # SQL over the WHOLE ledger. Summing ``rows`` client-side would be a
        # different number the moment the ledger outgrows ``limit`` — and wrong
        # quietly, since a smaller total still looks like a total.
        "by_user": store.earnings_outstanding_by_user(),
        "rates": {
            "tl_rate_cents": asc_payments.tl_rate_cents(),
            "tr_session_cents": asc_payments.tr_session_cents(),
            "tr_min_seconds": asc_payments.tr_min_seconds(),
            "tl_auto_approve_days": asc_payments.tl_auto_approve_days(),
        },
    }


def _enrich_case_context(store: Any, rows: List[Dict[str, Any]]) -> None:
    """Add ``case_id``, ``specialty`` and ``seconds`` to each ledger row, in place.

    **A zero is never written for an unknown.** ``submissions.time_spent_sec``
    defaults to 0, so a task that predates timing, or one whose timer never
    started, reads as 0 seconds — and "0m" in a Time column is how an operator
    voids honest work. Unknown stays ``None`` and the console renders an em dash.

    Only ``task`` rows carry a case: a review session spans several and a
    referral bounty is not casework at all. Those get ``None``, not a guess.
    """
    task_cache: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        row.setdefault("case_id", None)
        row.setdefault("specialty", None)
        row.setdefault("seconds", None)
        # The internal case-quality number, and WHY it is that number. Read from
        # the stamp rather than recomputed, so the console shows the figure the
        # case was actually graded on. None means never graded, which the UI
        # renders as an em dash for the same reason a zero is never written for
        # an unknown time.
        row.setdefault("quality", None)
        row.setdefault("quality_reasons", None)
        kind = row.get("kind")
        ref = row.get("ref_id")
        if not ref:
            continue
        if kind == asc_payments.KIND_TASK:
            sub = store.get_submission(ref)
            if not sub:
                continue
            tid = sub.get("task_id")
            row["case_id"] = tid
            secs = sub.get("time_spent_sec")
            # 0 means "not recorded", not "instant".
            row["seconds"] = int(secs) if secs else None
            try:
                stamped = store.submission_quality(ref)
            except Exception:  # noqa: BLE001 - never break the money screen
                stamped = None
            if stamped:
                row["quality"] = stamped.get("score")
                row["quality_reasons"] = (stamped.get("components") or {}).get("reasons")
            if tid:
                if tid not in task_cache:
                    task_cache[tid] = store.get_task(tid) or {}
                row["specialty"] = task_cache[tid].get("specialty")
        elif kind == asc_payments.KIND_REVIEW_SESSION:
            # Reviewers are paid per SESSION, and payments.py already tracks the
            # session's credited duration — the honest number here.
            session = store.get_work_session(ref)
            if session:
                secs = session.get("credited_seconds")
                row["seconds"] = int(secs) if secs else None


@router.get("/api/asclepius/admin/referrals")
async def admin_referrals(
    limit: int = Query(500, ge=1, le=2000),
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The whole referral book, for the Money tab: who referred whom, where
    each row sits in the funnel, what the ledger has paid, and any fraud
    flag. Admin eyes only, so the invitee's raw address is shown."""
    store = _store()
    rows = []
    for r in store.list_all_referrals(limit=limit):
        rows.append({
            "referral_id": r.get("referral_id"),
            "referrer_name": (r.get("referrer_name") or "").strip() or None,
            "referrer_email": r.get("referrer_email"),
            "invitee_email": r.get("invitee_email"),
            "invitee_name": r.get("invitee_name"),
            "status": r.get("status"),
            "status_sentence": asc_referrals.status_sentence(
                r.get("status"), r.get("bounty_state")),
            "bounty_state": r.get("bounty_state") or "pending",
            "source": r.get("source"),
            "fraud_flag": r.get("fraud_flag"),
            "invited_at": r.get("invited_at"),
            "first_case_at": r.get("first_case_at"),
        })
    return {
        "rows": rows,
        "payout_structure": {
            "referrer_bounty_cents": asc_payments.referral_bounty_cents(),
            "referee_bonus_cents": asc_payments.referee_bonus_cents(),
            "cap_cents": asc_payments.referral_cap_cents(),
        },
    }


class MarkPaidBody(BaseModel):
    # The idempotency key, not a label. Replaying a batch is a no-op, which is
    # what makes a retried disbursement job safe.
    payout_batch_id: str = Field(default="")
    earning_ids: Optional[List[str]] = None
    user_id: Optional[str] = None


@router.post(
    "/api/asclepius/admin/earnings/mark-paid",
    dependencies=[Depends(rate_limiter("asclepius_mark_paid", 30, 600))],
)
async def mark_paid(
    body: MarkPaidBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Record that a batch of approved earnings has actually been disbursed.

    This is the ledger's record that money moved; it does not move money. The
    transfer is a treasury operation, and ``payout_batch_id`` is how the two are
    reconciled — ``GET /admin/earnings?payout_batch_id=...`` returns exactly the
    rows a given disbursement covered.

    ``user_id`` appears here and nowhere else on this router. That is the point of
    the split: every doctor-facing route scopes from the session and cannot name
    another physician, and the one route that must name one is admin-gated."""
    store = _store()
    _rail_preflight(store, user_id=body.user_id, earning_ids=body.earning_ids)
    try:
        result = asc_payments.mark_paid(
            store, payout_batch_id=body.payout_batch_id, actor_id=admin["id"],
            earning_ids=body.earning_ids, user_id=body.user_id)
    except asc_payments.PaymentsDenied as denied:
        raise HTTPException(status_code=422, detail=denied.detail)
    return _with_transfers(store, result, actor=admin)


# ═══ PAYMENTS RAIL §C, §D: the seam where money actually moves ═══════════════
#
# Everything below sits next to mark-paid on purpose. The header at the top of
# this file has said for two releases that the rail would be Stripe Connect
# Express and that nothing here may ever store a bank account number or a tax
# id; this is that commitment, and it is readable in one place beside the ledger
# write it hangs off.
#
# THE ORDERING IS THE DESIGN (G2). ``mark_paid`` stays the source of truth: its
# batch-id idempotency and guarded compare-and-set already make a retried
# disbursement safe, and a second write path would be a second chance to pay
# twice. The transfer is created AFTER the compare-and-set succeeds, as a
# consequence of the ledger row changing state, never as a precondition. So a
# transfer failure cannot un-settle the ledger. It becomes a visible
# reconciliation item instead, which is the honest split: the ledger records our
# decision to pay, Stripe records the execution, and the one thing that must
# never happen is the ledger saying settled for a physician we provably cannot
# pay. That case is refused BEFORE mark_paid runs, not detected after.
#
# WITH THE FLAG OFF EVERY FUNCTION HERE IS A NO-OP and the two routes above
# behave exactly as they did before this file learned the word Stripe.

#: Transfer attempt states written to ``stripe_transfers``. ``blocked`` is not a
#: Stripe outcome: it is us refusing to call Stripe for a row whose physician has
#: no connected account, recorded so the queue shows why nothing was attempted.
TRANSFER_OK = "transferred"
TRANSFER_FAILED = "failed"
TRANSFER_BLOCKED = "blocked"


def _rail():
    """The rail module, imported inside the flag (G6).

    Module scope would make ``asclepius.stripe_rail`` load on every import of
    this router, which is harmless today and exactly the kind of thing that
    stops being harmless the first time that module grows a top-level SDK
    import.
    """
    from asclepius import stripe_rail                      # noqa: PLC0415
    return stripe_rail


def _target_user_ids(
    store: Any, *, user_id: Optional[str], earning_ids: Optional[List[str]],
) -> List[str]:
    """Whose money this call is about, resolved the same way mark_paid resolves
    it: a named physician, or the owners of the named rows."""
    if user_id:
        return [user_id]
    out: List[str] = []
    for earning_id in earning_ids or []:
        row = store.get_earning_by_id(earning_id)
        if row and row.get("user_id") and row["user_id"] not in out:
            out.append(row["user_id"])
    return out


def _rail_preflight(
    store: Any, *, user_id: Optional[str] = None,
    earning_ids: Optional[List[str]] = None,
) -> None:
    """Refuse, BEFORE the ledger write, to settle rows we cannot transfer (C2).

    Two refusals, and they are different kinds of problem told apart on purpose:

    * a misconfigured rail (flag on, key or package missing) is an operator
      incident and 503s loudly. A payout run that quietly moved no money would
      look identical to one that worked.
    * a physician with no ``active`` bank link is a 409 naming them. Settling a
      row for someone Stripe will not pay creates a ledger that says we paid and
      a bank account that never saw it, and no later reconciliation makes that
      row honest again.

    Flag off: returns immediately, so this function does not exist as far as
    today's behavior is concerned.
    """
    rail = _rail()
    if not rail.enabled():
        return
    try:
        rail.sdk()
    except rail.RailUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    for uid in _target_user_ids(store, user_id=user_id, earning_ids=earning_ids):
        user = store.get_user_by_id(uid) or {}
        status = (user.get("bank_link_status") or "").strip()
        if status == rail.ACTIVE and (user.get("stripe_account_id") or "").strip():
            continue
        raise HTTPException(
            status_code=409,
            detail=(f"{user.get('email') or uid} has not finished linking a bank "
                    f"account (status: {status or 'never started'}), so these rows "
                    "cannot be paid. Nothing was marked paid."))


def _transfer_one(
    store: Any, earning: Dict[str, Any], *, actor: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """One ledger row, one transfer, one ``stripe_transfers`` row (G3, G4).

    Never raises. A failure here is a queue item, not an exception that
    unwinds a ledger write that has already committed.
    """
    rail = _rail()
    earning_id = earning["earning_id"]
    batch = earning.get("payout_batch_id")
    user = store.get_user_by_id(earning.get("user_id")) or {}
    destination = (user.get("stripe_account_id") or "").strip()
    if not destination:
        row = store.record_stripe_transfer(
            earning_id=earning_id, status=TRANSFER_BLOCKED, payout_batch_id=batch,
            failure_reason="This physician has no connected Stripe account.")
        log.error("asclepius.payments: earning %s settled with no transfer "
                  "destination", earning_id)
        return _transfer_outcome(row)

    try:
        created = rail.create_transfer(
            earning_id=earning_id,
            amount_cents=int(earning.get("amount_cents") or 0),
            destination=destination,
            payout_batch_id=batch,
            user_id=user.get("id"))
    except Exception as exc:
        reason = getattr(exc, "reason", None) or str(exc) or exc.__class__.__name__
        row = store.record_stripe_transfer(
            earning_id=earning_id, status=TRANSFER_FAILED, payout_batch_id=batch,
            failure_reason=reason)
        store.log_event(
            entity_type="earning", entity_id=earning_id,
            event_type="earning_transfer_failed",
            actor=(actor or {}).get("id"),
            payload={"payout_batch_id": batch, "reason": reason,
                     "amount_cents": int(earning.get("amount_cents") or 0)})
        log.error("asclepius.payments: transfer failed for earning %s: %s",
                  earning_id, reason)
        return _transfer_outcome(row)

    row = store.record_stripe_transfer(
        earning_id=earning_id, status=TRANSFER_OK, payout_batch_id=batch,
        transfer_id=created["transfer_id"], failure_reason=None)
    store.log_event(
        entity_type="earning", entity_id=earning_id,
        event_type="earning_transfer_created", actor=(actor or {}).get("id"),
        payload={"payout_batch_id": batch, "transfer_id": created["transfer_id"],
                 "amount_cents": created.get("amount_cents")})
    return _transfer_outcome(row)


def _transfer_outcome(row: Dict[str, Any]) -> Dict[str, Any]:
    """What the console renders per row: what happened, not what was intended."""
    return {"earning_id": row["earning_id"], "status": row["status"],
            "transfer_id": row.get("transfer_id"),
            "failure_reason": row.get("failure_reason")}


def _with_transfers(
    store: Any, result: Dict[str, Any], *, actor: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Transfer every settled row in the batch, and report row by row (C1, C3).

    Scoped to the BATCH rather than to the rows this particular call changed,
    which makes a replayed batch retry its failures instead of reporting a bare
    ``marked: 0``. That is safe precisely because the idempotency key is
    ``earning:{id}``: a row that already transferred is skipped here and would
    be a no-op at Stripe even if it were not.

    Flag off: returns ``result`` unchanged, same object, same keys.
    """
    rail = _rail()
    if not rail.enabled():
        return result
    batch = result.get("payout_batch_id")
    outcomes: List[Dict[str, Any]] = []
    for earning in store.list_earnings(payout_batch_id=batch, status="paid", limit=2000):
        existing = store.get_stripe_transfer(earning["earning_id"])
        if existing and existing.get("status") == TRANSFER_OK:
            continue
        outcomes.append(_transfer_one(store, earning, actor=actor))
    return {**result, "transfers": outcomes}


@router.post(
    "/api/asclepius/admin/earnings/{earning_id}/retry-transfer",
    dependencies=[Depends(rate_limiter("asclepius_retry_transfer", 60, 600))],
)
async def admin_retry_transfer(
    earning_id: str,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Retry the Stripe transfer for one settled row (C4).

    The gate is deliberately narrow: settled, and not already transferred. A
    row still in review has no decision to execute, and a row that transferred
    is done. Note what the gate is NOT protecting against: a double-clicked
    retry cannot double-pay whatever the gate says, because the idempotency key
    is derived from the ledger row and Stripe returns the first transfer for the
    second call. The gate exists so the console can say what it refused and why,
    not because correctness rests on it.
    """
    rail = _rail()
    if not rail.enabled():
        # Admin-facing, so this says what is wrong rather than 404ing the way the
        # webhook does. There is no signature oracle to hide behind an
        # admin-gated route, and "the rail is off" is the answer an operator
        # needs.
        raise HTTPException(
            status_code=409,
            detail="The Stripe payout rail is off, so there is no transfer to retry.")
    store = _store()
    earning = store.get_earning_by_id(earning_id)
    if earning is None:
        raise HTTPException(status_code=404, detail="No such ledger row.")
    if earning.get("status") != asc_payments.PAID:
        raise HTTPException(
            status_code=409,
            detail="That row is not settled, so nothing was ever meant to transfer "
                   "for it. Mark it paid first.")
    existing = store.get_stripe_transfer(earning_id)
    if existing and existing.get("status") == TRANSFER_OK:
        raise HTTPException(
            status_code=409,
            detail="That row has already transferred. Reversals are a Stripe "
                   "treasury operation and are not handled here.")
    _rail_preflight(store, user_id=earning.get("user_id"))
    outcome = _transfer_one(store, earning, actor=admin)
    return {"ok": outcome["status"] == TRANSFER_OK, "earning_id": earning_id,
            "transfer": outcome}


# ═══ PAYMENTS RAIL §D: webhooks ══════════════════════════════════════════════
#
# On the payments router rather than in main.py because everything it writes is
# payments state, and the router is already mounted. Nothing about the route is
# session-scoped: Stripe is the caller, the signature is the authentication, and
# a request that fails verification never reaches a handler.
def _handle_account_updated(store: Any, obj: Dict[str, Any]) -> str:
    """B3/D2: recompute ``bank_link_status`` from what Stripe now says."""
    rail = _rail()
    account_id = obj.get("id")
    if not account_id:
        return "ignored: no account id"
    user = store.get_user_by_stripe_account(str(account_id))
    if user is None:
        # Stripe delivers events for every account on the platform, including
        # ones this database has never seen. Not an error, and not a 500.
        return "ignored: unknown account"
    was = (user.get("bank_link_status") or "").strip()
    now = rail.status_for_account(obj)
    if now == was:
        return f"unchanged: {now}"
    store.set_bank_link_status(user["id"], now)
    store.log_event(
        entity_type="user", entity_id=user["id"],
        event_type="bank_link_status_changed", actor=None,
        payload={"from": was or None, "to": now, "source": "webhook"})
    log.warning("asclepius.payments: bank link for %s moved %s -> %s",
                user["id"], was or "none", now)
    return f"status: {was or 'none'} -> {now}"


def _handle_transfer_event(store: Any, event_type: str, obj: Dict[str, Any]) -> str:
    """D3: stamp the matching attempt row. A reversal is visibility only (G7).

    A reversal deliberately writes no ledger row. The void endpoint already 409s
    on a paid row because money has left, and a ledger that auto-mutated on
    reversal would contradict the attributed, human-decided shape of every other
    money action in this system.
    """
    rail = _rail()
    transfer_id = obj.get("id")
    if not transfer_id:
        return "ignored: no transfer id"
    status = rail.transfer_status_from_event(event_type, obj)
    failure = obj.get("failure_message") or None
    row = store.stamp_stripe_transfer_status(
        str(transfer_id), status=status, failure_reason=failure)
    if row is None:
        return "ignored: unknown transfer"
    return f"transfer {transfer_id}: {status}"


@router.post("/api/asclepius/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe's callback. Signature-verified, stored, then processed (D1, G5).

    The order matters and is the whole reason this is a table rather than a
    handler. The event lands in ``stripe_webhook_events`` BEFORE anything acts
    on it, and the duplicate test is ``processed_at IS NOT NULL`` rather than
    "have I seen this id": a crash between the insert and the handler must leave
    a work item that Stripe's redelivery picks up, not a row that makes the
    redelivery look like a duplicate and drop it.

    404 while dark, so the route is not observable and does not offer a
    signature oracle to anyone probing for one.
    """
    rail = _rail()
    if not rail.enabled():
        raise HTTPException(status_code=404, detail="Not Found")

    payload = await request.body()
    try:
        event = rail.construct_event(payload, request.headers.get("stripe-signature"))
    except rail.SignatureInvalid as bad:
        # 400, never 200. An unverified body is an unauthenticated write path
        # into a money surface, and accepting it quietly would be worse than
        # letting Stripe retry.
        log.warning("asclepius.payments: rejected unsigned Stripe webhook: %s", bad)
        raise HTTPException(status_code=400, detail="Signature verification failed.")
    except rail.RailUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    event_id = event.get("id")
    if not event_id:
        raise HTTPException(status_code=400, detail="Event has no id.")

    import json as _json                                   # noqa: PLC0415
    store = _store()
    store.record_stripe_webhook_event(
        event_id=str(event_id), event_type=event.get("type"),
        payload_json=_json.dumps(event.get("object") or {}, default=str))
    stored = store.get_stripe_webhook_event(str(event_id)) or {}
    if stored.get("processed_at"):
        return {"ok": True, "duplicate": True}

    event_type = (event.get("type") or "").strip()
    obj = event.get("object") or {}
    if event_type == "account.updated":
        outcome = _handle_account_updated(store, obj)
    elif event_type in rail.TRANSFER_EVENTS:
        outcome = _handle_transfer_event(store, event_type, obj)
    else:
        # D4. Stripe adds event types, and a webhook that 500s on novelty gets
        # disabled by its own retry policy. Stored, stamped, and no action.
        outcome = "ignored: unhandled type"
    store.stamp_stripe_webhook_event(str(event_id), outcome=outcome)
    return {"ok": True, "received": True}


# ═══ Admin Launch PRD §4 — one physician, one case ════════════════════════════
#
# The Money screen is two levels: every physician with an outstanding total, and
# then that physician's cases. These three routes are the level-2 actions.
#
# One rule runs through all of them: **the server owns the total.** Void and pay
# both return the recomputed figure, and the console renders that rather than
# subtracting locally. A client-side total that drifts from the ledger is the bug
# this return value exists to make impossible.

def _case_export_payload(store: Any, earning: Dict[str, Any]) -> Dict[str, Any]:
    """The admin spot-check for one paid-for case, shaped by the EXPORT pipeline.

    Deliberately not a second serializer. It runs the same profile mapping, the
    same ``_case_answer_key``, the same review/supervision blocks and the same
    ``_case_bundle`` fold that produce ``cases.jsonl`` in a buyer bundle — so a
    buyer-facing artifact and an admin spot-check cannot disagree about what a
    case contains. If they could, the spot-check would be checking something we
    never ship.

    Raises HTTPException for the cases an admin needs told apart: an earning that
    is not a case at all, a case whose records have not been packaged yet, and a
    case the export gate would reject.
    """
    from asclepius import export as asc_export           # noqa: PLC0415
    from asclepius import packaging as asc_packaging     # noqa: PLC0415
    from asclepius import profiles                       # noqa: PLC0415
    from asclepius import credentials as asc_credentials  # noqa: PLC0415

    kind = earning.get("kind")
    if kind != asc_payments.KIND_TASK:
        # A review session spans several cases and a referral bounty is not a
        # case at all. Saying so beats exporting a plausible-looking empty
        # bundle, which is how an operator concludes a case has no content.
        raise HTTPException(
            status_code=409,
            detail=f"A {kind!r} ledger row is not a single case, so there is nothing "
                   "to export for it. Only task rows carry one case.")

    submission_id = earning.get("ref_id")
    submission = store.get_submission(submission_id)
    if not submission:
        raise HTTPException(status_code=404,
                            detail="The submission behind this ledger row no longer exists.")
    task_id = submission.get("task_id")
    task = store.get_task(task_id) or {}

    prof = profiles.load_profile("default")
    emitted: List[Dict[str, Any]] = []
    mapped_objs: List[Dict[str, Any]] = []
    reviews_by_sid: Dict[Any, List[Dict[str, Any]]] = {}
    obs_by_tid: Dict[Any, Optional[Dict[str, Any]]] = {
        task_id: store.get_agreement_observation(task_id) if task_id else None}

    # Every labeler on this case, not only the one being paid: a case with two
    # labelers and a review is one artifact, and a spot-check that showed one
    # label would misreport the consensus the buyer receives.
    for sub in store.submissions_for_task(task_id):
        sid = sub["submission_id"]
        if sid not in reviews_by_sid:
            reviews_by_sid[sid] = store.reviews_for_submission(sid)
        for rec in store.records_for_submission(sid):
            payload = dict(rec.get("payload") or {})
            payload.pop("record_id", None)
            try:
                mapped = profiles.map_record(prof, payload)
            except Exception:
                mapped = None
            if mapped is None:
                continue          # a type this profile does not emit
            answer_key = asc_export._case_answer_key(store, rec)
            if answer_key:
                mapped["answer_key"] = answer_key
            mapped["review"] = asc_packaging.review_block(reviews_by_sid[sid], store)
            mapped["supervision"] = asc_packaging.supervision_block(
                labeler_id_hashed=payload.get("annotator_id_hashed"),
                observation=obs_by_tid.get(task_id))
            emitted.append(rec)
            mapped_objs.append(mapped)

    if not emitted:
        raise HTTPException(
            status_code=409,
            detail="This case has no packaged records yet, so there is nothing to "
                   "spot-check. Records are written when a submission reaches a "
                   "terminal state.")

    cases = asc_export._case_bundle(store, emitted, mapped_objs, reviews_by_sid, obs_by_tid)

    # The SAME Tier B gate the export runs. An admin spot-check that quietly
    # returned a payload the export would reject is a spot-check that cannot
    # catch the one thing it is worth catching.
    for case in cases:
        leak = asc_credentials.find_tier_b_leak(case)
        if leak is not None:
            raise HTTPException(
                status_code=409,
                detail=f"This case carries the identifying field {leak!r}, which the "
                       "export gate rejects. It cannot ship in this state: fix the "
                       "record before paying for it.")

    return {
        # Deliberately NO physician identity. The admin already knows whose
        # ledger they opened, and this file is shaped like an export bundle and
        # named like one — a stray email address in it is an identity leak one
        # forward away from a buyer, in an architecture whose core rule is that
        # buyer-facing artifacts carry credential ATTRIBUTES only. earning_id is
        # opaque and traces back internally, which is all a spot-check needs.
        "earning_id": earning["earning_id"],
        "case_id": task_id,
        "specialty": task.get("specialty"),
        "modality": asc_export._rec_modality(emitted[0]),
        "amount_cents": int(earning.get("amount_cents") or 0),
        "status": earning.get("status"),
        "exported_for": "admin spot-check: not a buyer deliverable",
        "cases": cases,
    }


@router.get("/api/asclepius/admin/earnings/{earning_id}/case-export")
async def admin_case_export(
    earning_id: str,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Download the one case behind a ledger row, shaped exactly as it ships."""
    import json as _json                                 # noqa: PLC0415
    store = _store()
    earning = store.get_earning_by_id(earning_id)
    if earning is None:
        raise HTTPException(status_code=404, detail="No such ledger row.")
    payload = _case_export_payload(store, earning)
    body = _json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    filename = f"case-{payload.get('case_id') or earning_id}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


class ReleaseHoldBody(BaseModel):
    # The algorithm proposed a reduction; a person may disagree with it. Both
    # answers are decisions and both are recorded.
    pay_full_rate: bool = False
    note: str = Field("", max_length=500)


@router.get("/api/asclepius/admin/earnings/held")
async def admin_held_earnings(
    user_id: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Rows where the payout algorithm proposed paying less than the posted rate
    and is waiting on a human.

    This queue exists because a proposal nobody can see is an automated decision
    with extra steps. If it grows, physicians are waiting on us, and that is
    visible here rather than only in their inbox.
    """
    store = _store()
    rows = store.held_earnings(user_id=user_id, limit=limit)
    _enrich_case_context(store, rows)
    return {"held": rows, "count": len(rows)}


@router.post(
    "/api/asclepius/admin/earnings/{earning_id}/release",
    dependencies=[Depends(rate_limiter("asclepius_release_earning", 60, 600))],
)
async def admin_release_earning_hold(
    earning_id: str,
    body: ReleaseHoldBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Decide a proposed pay reduction.

    THE POINT OF THIS ENDPOINT: the algorithm never applies a pay cut on its
    own. It computes a multiplier, and a row it wants to pay below the posted
    rate is held until a person acts here. An automated reduction and a proposed
    reduction a human approves are materially different objects, legally and
    ethically, and this route is the difference between them.

    ``pay_full_rate`` overrides the proposal and pays the posted rate, because
    the person deciding is allowed to disagree with the model. Either way the
    decision is attributed and timestamped: reducing a physician's pay is
    consequential and an unattributable reduction cannot be appealed.

    Releasing does not itself approve the row. It removes the hold, and the
    normal ledger rules (a verdict, or the auto-approve window) then apply, so
    this endpoint decides ONE question and does not quietly decide others.
    """
    store = _store()
    earning = store.get_earning_by_id(earning_id)
    if earning is None:
        raise HTTPException(status_code=404, detail="No such ledger row.")
    updated = store.release_earning_hold(
        earning_id, by=admin["email"], pay_full_rate=bool(body.pay_full_rate))
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="That row is not holding a proposed reduction. It may have "
                   "been decided already, or never been held.")
    store.log_event(
        entity_type="earning", entity_id=earning_id,
        event_type="earning_quality_hold_released", actor=admin["email"],
        payload={"pay_full_rate": bool(body.pay_full_rate),
                 "amount_cents": updated.get("amount_cents"),
                 "rate_cents": updated.get("rate_cents"),
                 "multiplier": updated.get("quality_multiplier"),
                 "note": (body.note or "").strip() or None},
    )
    return {"earning": updated}


class ApproveEarningBody(BaseModel):
    # Optional: an approval is the expected outcome and does not need defending
    # the way a void does. Recorded when given.
    note: str = Field("", max_length=500)


#: Every way ``payments.approve_earning`` can refuse, and what the operator is
#: told. A dict rather than a chain of ``if``s so a new refusal token cannot
#: reach the console as a 500 — the mapping is total by construction, and
#: ``test_export_approval_prd`` asserts it covers ``APPROVE_REFUSALS``.
_APPROVE_HTTP: Dict[str, Dict[str, Any]] = {
    "not_found": {"status_code": 404, "detail": "No such ledger row."},
    "already_paid": {
        "status_code": 409,
        "detail": "That case has already been paid, which is past approved. "
                  "Nothing to do."},
    "already_approved": {
        "status_code": 409,
        "detail": "That case is already approved. Nothing was written twice."},
    "voided": {
        "status_code": 409,
        "detail": "That case was voided. Re-approving a void is a reversal, not "
                  "an approval, it is not available here."},
    "quality_held": {
        "status_code": 409,
        "detail": "The payout algorithm proposed paying this case below the "
                  "posted rate and is waiting on a decision. Release the hold "
                  "first, then approve."},
    "raced": {
        "status_code": 409,
        "detail": "That row is no longer awaiting approval, it was decided by "
                  "someone else (or by the auto-approve sweep) a moment ago."},
}


@router.post(
    "/api/asclepius/admin/earnings/{earning_id}/approve",
    dependencies=[Depends(rate_limiter("asclepius_approve_earning", 60, 600))],
)
async def admin_approve_earning(
    earning_id: str,
    body: ApproveEarningBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Approve one case — the ledger AND the export gate, in one action.

    THE POINT OF THIS ENDPOINT (PRD §1.1): before it existed there was no admin
    action that moved a labeler's earning at all. `/release` decides a proposed
    pay cut and `/void` declines to pay; neither approves. So an accrued row sat
    reading "Pending review" with no button under it, waiting on a reviewer who
    might never come, and even when one did the case still could not ship —
    because approving the MONEY never touched `records.status`, which is the
    only thing export reads.

    Three writes, one meaning:

    1. the ledger row moves ``accrued`` → ``approved`` (compare-and-set, so a
       double-click cannot double-approve),
    2. the submission and its records move to ``export_ready`` — but only from a
       non-terminal state: an already-``exported`` case is never downgraded and a
       ``rejected`` one is Void's business, not this route's,
    3. an audit event, because this deliberately BYPASSES QA sampling and an
       audit that does not say so is not an audit.

    A row the payout algorithm has HELD is refused with a 409 pointing at
    `/release`. That hold is the promise that an automated pay cut never applies
    without a person deciding it, and approving through this route would apply
    the reduced amount while looking like a plain approval.
    """
    store = _store()
    result = asc_payments.approve_earning(
        store, earning_id=earning_id, actor=admin["email"], note=body.note)
    if not result["ok"]:
        raise HTTPException(**_APPROVE_HTTP[result["refusal"]])

    gate = result["gate"]
    user_id = result.get("user_id")
    return {
        "ok": True,
        "earning_id": earning_id,
        "approved": True,
        "row": result["row"],
        "user_id": user_id,
        # What happened to the EXPORT gate, in the same response. An operator who
        # approves a case is entitled to know in one round trip whether it can
        # now ship — that silence is the bug this PRD exists to fix.
        "exportable": bool(gate["moved"]) or gate["outcome"] == "already",
        "records_outcome": gate["outcome"],
        "submission_status": gate["status"],
        "totals": store.earnings_payable_for_user(user_id),
    }


class VoidEarningBody(BaseModel):
    # Required, and long enough to be a reason rather than a keystroke. Voiding a
    # doctor's pay must be attributable AND explicable — "x" is neither.
    reason: str = Field(..., min_length=3, max_length=500)


@router.post(
    "/api/asclepius/admin/earnings/{earning_id}/void",
    dependencies=[Depends(rate_limiter("asclepius_void_earning", 60, 600))],
)
async def admin_void_earning(
    earning_id: str,
    body: VoidEarningBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Decline to pay for one case.

    Four properties, all of them load-bearing:

    1. ``paid`` is a **409**. The money already left; a refund is a treasury
       operation and out of scope here. Voiding it would leave the ledger saying
       we owe nothing for work we have already disbursed.
    2. Idempotent on ``earning_id`` — a double-click cannot double-decrement,
       because the store's guarded UPDATE is the arbiter rather than a read.
    3. The recomputed total comes back, so the console renders the ledger's
       number instead of subtracting its own.
    4. An audit row, because voiding a physician's pay is consequential and must
       be attributable to the person who did it.
    """
    reason = (body.reason or "").strip()
    if len(reason) < 3:
        raise HTTPException(status_code=422,
                            detail="A void needs a reason of at least 3 characters.")
    store = _store()
    earning = store.get_earning_by_id(earning_id)
    if earning is None:
        raise HTTPException(status_code=404, detail="No such ledger row.")

    result = store.void_earning(earning_id, reason=reason, voided_by=admin["email"])
    if result["reason_code"] == "already_paid":
        raise HTTPException(
            status_code=409,
            detail="That case has already been paid. Money has left; refunds are "
                   "handled outside the ledger.")
    if result["reason_code"] == "not_found":
        raise HTTPException(status_code=404, detail="No such ledger row.")

    gate = {"moved": False, "outcome": "not_a_case", "status": None}
    if result["changed"]:
        # THE MIRROR (PRD §1.1). Approve moves the ledger and the export gate
        # together; a void that moved only the ledger would leave a case we have
        # declined to pay for still queued to ship. Non-terminal states only:
        # an already-``exported`` case is with a buyer and cannot be recalled by
        # a payment decision.
        gate = asc_payments.apply_ledger_decision_to_records(
            store, submission_id=asc_payments.submission_ref(
                earning.get("kind"), earning.get("ref_id")),
            decision="reject", reason="admin_voided", actor=admin["email"])
        store.log_event(
            entity_type="earning", entity_id=earning_id, event_type="earning_voided",
            actor=admin["email"],
            payload={"reason": reason, "kind": earning.get("kind"),
                     "ref_id": earning.get("ref_id"), "user_id": earning.get("user_id"),
                     "amount_cents": int(earning.get("amount_cents") or 0),
                     "from_status": earning.get("status"),
                     "records_outcome": gate["outcome"]},
        )
        log.warning("asclepius.payments: earning %s voided by %s (%s; records: %s)",
                    earning_id, admin["email"], reason, gate["outcome"])

    user_id = earning.get("user_id")
    return {
        "ok": True,
        "earning_id": earning_id,
        # False on a replayed void. The console needs to tell "I just voided
        # this" from "this was already void" — they are the same end state and
        # very different things to say to an operator.
        "voided": bool(result["changed"]),
        "row": result["row"],
        "user_id": user_id,
        "records_outcome": gate["outcome"],
        "submission_status": gate["status"],
        "totals": store.earnings_payable_for_user(user_id),
    }


class PayEarningsBody(BaseModel):
    user_id: str
    # ``None`` (omitted) and ``[]`` (sent, empty) are DIFFERENT requests and the
    # difference is money. Omitted means "every approved row this physician has",
    # which is ``mark_paid``'s user-scoped mode. An explicitly empty list means
    # the caller selected nothing — and must not be silently widened into paying
    # everything, which a ``default_factory=list`` plus ``or None`` does.
    earning_ids: Optional[List[str]] = None
    payout_batch_id: str = Field(default="")


@router.post(
    "/api/asclepius/admin/earnings/pay",
    dependencies=[Depends(rate_limiter("asclepius_pay_earnings", 30, 600))],
)
async def admin_pay_earnings(
    body: PayEarningsBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Send payment for one physician's approved rows.

    Thin over ``asc_payments.mark_paid`` rather than a parallel write path —
    that function owns the batch-id idempotency and the compare-and-set that
    makes a retried disbursement safe, and a second implementation of it is a
    second chance to pay twice.

    The one thing added here is the compensation guard: an advisor on
    ``equity_only`` holds equity and is not paid per case. It calls
    ``compensation.accrues_payment`` rather than re-testing the column, because
    the rule that NULL is payable lives in that function and a re-implementation
    written as ``!= 'equity_only'`` gets legacy contributors wrong.
    """
    from asclepius import compensation                    # noqa: PLC0415
    if body.earning_ids is not None and not body.earning_ids:
        # Selected nothing. Falling through would reach mark_paid with
        # earning_ids=None and a user_id, i.e. "pay this physician everything".
        raise HTTPException(
            status_code=422,
            detail="No rows were selected for payment. Omit earning_ids entirely "
                   "to pay every approved row for this physician.")
    store = _store()
    user = store.get_user_by_id(body.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such physician.")
    if not compensation.accrues_payment(user):
        raise HTTPException(
            status_code=409,
            detail="This contributor is on the equity-only model: they hold equity "
                   "and are not paid per case. Nothing was marked paid.")
    _rail_preflight(store, user_id=body.user_id, earning_ids=body.earning_ids)
    try:
        result = asc_payments.mark_paid(
            store, payout_batch_id=body.payout_batch_id, actor_id=admin["id"],
            earning_ids=body.earning_ids, user_id=body.user_id)
    except asc_payments.PaymentsDenied as denied:
        raise HTTPException(status_code=422, detail=denied.detail)
    result = _with_transfers(store, result, actor=admin)
    return {"ok": True, "user_id": body.user_id, **result,
            "totals": store.earnings_payable_for_user(body.user_id)}
