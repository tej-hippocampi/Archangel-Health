"""Medical Advisor router (Advisor PRD) — the third physician tier's surface.

Three things live here, and they are deliberately one file rather than seven
features:

  * **Appointment** (§2.3) — ``POST /api/asclepius/admin/advisors``. An advisor
    is APPOINTED by an admin against a signed agreement, never scored into the
    role by ``propose_tier``.
  * **Referrals** (§3) — an advisor invites physicians and watches their own
    funnel. Scoped from the SESSION, never a query parameter.
  * **Advisory sign-off** (§4) — four of the seven requested capabilities
    (preview task batches · review outbound bundles · review inbound hospital
    metadata · comment on product specs) are the SAME object: a qualified
    physician examines an artifact and records a verdict with comments. One
    table, one endpoint, an ``artifact_type`` discriminator. Four features would
    mean four UIs, four permission checks, and four places the next person
    forgets to update.

Access is gated on CAPABILITY, never on a tier literal — see
``asclepius/capabilities.py`` for why that distinction is the whole risk of
this build.

Own router module by design (00_START_HERE §3.1): ``routers/asclepius.py`` is
never edited; main.py gains one import and one mount line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from starlette.concurrency import run_in_threadpool

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius import export as asc_export
from asclepius.cases import public_case
from asclepius.store import get_store
from ratelimit import rate_limiter

log = logging.getLogger("asclepius.advisor")

router = APIRouter(tags=["asclepius-advisor"])

# ─── Sign-off vocabulary (§4.1) ──────────────────────────────────────────────
ARTIFACT_TYPES = ("task_batch", "export_bundle", "inbound_upload", "product_spec")
VERDICTS = ("approved", "approved_with_comments", "changes_requested")

# The relationship recorded alongside every advisory verdict (§0.2). An advisor
# holding equity who attests that a batch is good enough to ship is a
# related-party attestation — different in kind from the same person labeling a
# case. Written by the SERVER from the advisor's own row; a client-supplied
# value is ignored, because a disclosure the subject can author is not one.
RELATIONSHIP_ADVISOR_EQUITY = "advisor_equity"
RELATIONSHIP_ADMIN = "internal_admin"

# Which capability each artifact type requires. A dict rather than a chain of
# ifs so adding a fifth artifact is one line and cannot be half-added.
_CAP_FOR_ARTIFACT = {
    "task_batch": asc_caps.SIGNOFF_TASKS,
    "export_bundle": asc_caps.SIGNOFF_EXPORT,
    "inbound_upload": asc_caps.SIGNOFF_INTAKE,
    "product_spec": asc_caps.SIGNOFF_SPEC,
}

# How many records of an about-to-ship bundle an advisor sees. The point is to
# let a physician judge the shape and quality of what leaves the building, not
# to re-serve the whole export through a second door.
_EXPORT_SAMPLE_N = 20

# ONE cap for task-batch membership, used by the queue count, the artifact view
# and the sign-off's recorded subject (audit M3). These were previously three
# different numbers — the queue counted with limit=500 while the view showed
# limit=50 — so a 60-task batch was reported as 60 in one place and 50 in
# another, both silently. Two different truncations of the same set is a way to
# make a reviewer confident about something they did not see.
_BATCH_MEMBER_CAP = 500


def _store():
    return get_store()


# ─── Capability gates ─────────────────────────────────────────────────────────
def _require(capability: str) -> Callable[..., Dict[str, Any]]:
    """Dependency factory: admits anyone whose tier grants ``capability`` (and
    admins, as everywhere else). One gate shape for every advisor endpoint, so a
    new endpoint cannot accidentally ship with no check or the wrong one."""

    def _dep(user: Dict[str, Any] = Depends(asc_auth.get_current_user)) -> Dict[str, Any]:
        if not asc_caps.can(user, capability):
            raise HTTPException(
                status_code=403,
                detail=f"This action requires the '{capability}' capability "
                       f"(medical advisor tier).")
        return user

    return _dep


require_refer = _require(asc_caps.REFER)


# ─── Referral throttles (audit H4) ───────────────────────────────────────────
# Keyed on the ADVISOR, not the client IP. The router's threat model is "an
# advisor is trusted; an advisor's stolen token is not" — and the IP is the one
# attribute a token thief controls freely, so an IP-keyed limit throttles
# nothing that matters. A global cap sits behind it as a volumetric backstop,
# mirroring the public self-serve endpoint this path parallels.
_REFERRALS_PER_ADVISOR = (10, 3600)     # 10 per hour, per advisor account
_REFERRALS_GLOBAL = (60, 3600)          # 60 per hour, fleet-wide
_REFERRALS_PER_INVITEE_24H = 3          # same address, same cap as self-serve


def _throttle_referral(advisor: Dict[str, Any]) -> None:
    """Per-advisor and fleet-wide limits. Raises 429."""
    from ratelimit import check, is_enabled

    if not is_enabled():
        return
    for key, (limit, window) in (
        (f"asclepius_advisor_referral:{advisor.get('id')}", _REFERRALS_PER_ADVISOR),
        ("asclepius_advisor_referral:__global__", _REFERRALS_GLOBAL),
    ):
        allowed, retry_after = check(key, limit, window)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many invitations sent recently. Try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )


def _relationship_for(user: Dict[str, Any]) -> str:
    """The relationship string stamped on a sign-off. An admin signing off is
    internal, not an equity-holding advisor — recording them identically would
    make the disclosure meaningless."""
    if (user or {}).get("tier") == asc_caps.ADVISOR:
        return RELATIONSHIP_ADVISOR_EQUITY
    return RELATIONSHIP_ADMIN


# ═══ Appointment (§2.3) — admin only ═════════════════════════════════════════
class AppointAdvisorBody(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    specialty: Optional[str] = None
    agreement_ref: str
    # Appointing someone this company previously REJECTED is a decision that
    # deserves a sentence (audit H5). Without these, a rejected account is
    # appointed straight through, its status flips to approved, and the note
    # explaining why it was rejected — "NPI does not match the name on the
    # license" — is silently replaced by the appointment note.
    override_rejection: bool = False
    override_reason: Optional[str] = None


@router.post("/api/asclepius/admin/advisors")
async def appoint_advisor(
    body: AppointAdvisorBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Appoint a medical advisor — provisioning the account if they are new.

    Advisors are appointed, never proposed (§2.3): ``propose_tier`` maps a score
    to labeler or reviewer and must never reach this tier. An advisory
    relationship is negotiated and carries equity; it is not the output of an
    NPI check and a years-in-practice weight.

    ``agreement_ref`` is REQUIRED. An advisor holding equity with no signed
    agreement on file is a liability, and a required field is the cheapest
    possible enforcement. The verification queue's approve path enforces the
    same rule, because it is the second door into the same tier.
    """
    agreement_ref = (body.agreement_ref or "").strip()
    if not agreement_ref:
        raise HTTPException(
            status_code=400,
            detail="agreement_ref is required: an advisor holds equity, and an "
                   "advisor with no signed agreement on file is a liability.")
    store = _store()
    email = str(body.email).lower().strip()
    user = store.get_user_by_email(email)
    provisioned = False
    if user is None:
        # A brand-new advisor gets an account with a generated standing key. It
        # is NOT mailed from here: the appointment is a conversation that has
        # already happened offline, and inventing a second credential-delivery
        # path is how two of them drift apart.
        user = store.create_user(
            email=email,
            password=secrets.token_urlsafe(18),
            role="evaluator",
            specialty=(body.specialty or "").strip().lower() or None,
        )
        provisioned = True
    if user.get("role") not in ("evaluator", "admin"):
        raise HTTPException(
            status_code=409,
            detail="That account is not a physician account and cannot hold the "
                   "advisor tier.")
    # A previously REJECTED account is not appointed by accident (audit H5). The
    # rejection note is the most important sentence in the row — someone decided
    # this physician could not be verified, and often said why — so overriding
    # it is an explicit act that carries its own reason, and the original note
    # is appended to rather than replaced.
    override_reason = (body.override_reason or "").strip()
    if user.get("verification_status") == "rejected":
        if not body.override_rejection or not override_reason:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This physician was previously REJECTED in verification"
                    + (f": “{user.get('verification_notes')}”"
                       if user.get("verification_notes") else "")
                    + ". Appointing them anyway requires override_rejection=true "
                      "and override_reason explaining why that decision is being "
                      "overturned."),
            )
    note = f"Appointed medical advisor · agreement {agreement_ref}"
    if override_reason:
        note += f" · overrides prior rejection: {override_reason}"
    if body.name:
        store.set_advisor_display_name(user["id"], body.name)

    # ONE write. An appointed advisor is verified BY the appointment — the admin
    # has a signed agreement in hand, which outranks anything an NPPES lookup
    # could add — so the tier, the agreement, the compensation model, the
    # referral code, the Slack label AND the verification stamp all land in a
    # single UPDATE. There is no ordering of two writes that is safe here
    # (audit H1): whichever goes second, a crash in between leaves an account
    # in a state the PRD calls a liability.
    prior_status = user.get("verification_status")
    updated = store.appoint_advisor(
        user["id"], agreement_ref=agreement_ref, appointed_by=admin["email"],
        approve=True, note=note,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="No such user")
    store.log_event(
        entity_type="user", entity_id=user["id"], event_type="advisor_appointed",
        actor=admin["email"],
        payload={"agreement_ref": agreement_ref, "provisioned": provisioned,
                 "compensation_model": updated.get("compensation_model"),
                 # An overturned rejection is the event a future auditor will
                 # look for; it goes in the log, not only in the notes column.
                 "prior_verification_status": prior_status,
                 "overrode_rejection": prior_status == "rejected",
                 "override_reason": override_reason or None},
    )
    return {"ok": True, "advisor": _advisor_public(updated), "provisioned": provisioned}


def _advisor_public(u: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "user_id": u.get("id"),
        "email": u.get("email"),
        "full_name": u.get("full_name"),
        "specialty": u.get("specialty"),
        "tier": u.get("tier"),
        "tier_word": asc_caps.tier_word(u.get("tier")),
        "compensation_model": u.get("compensation_model"),
        "advisor_since": u.get("advisor_since"),
        "advisor_agreement_ref": u.get("advisor_agreement_ref"),
        "referral_code": u.get("referral_code"),
        "slack_role": u.get("slack_role"),
    }


def _holds_advisory_relationship(u: Dict[str, Any]) -> bool:
    """Anyone with an advisory RELATIONSHIP on file, current tier aside.

    Filtering the roster on ``tier == 'advisor'`` made a demoted advisor and
    their entire sign-off history vanish from the only admin surface that lists
    them (audit M7) — while their equity, their signed agreement and their
    related-party disclosures all remained live. The people you most need to see
    are the ones whose relationship outlived their tier.
    """
    return bool(u.get("tier") == asc_caps.ADVISOR
                or u.get("advisor_since")
                or u.get("advisor_agreement_ref")
                or u.get("compensation_model") == "equity_only")


@router.get("/api/asclepius/admin/advisors")
async def list_advisors(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """The advisor roster — three people among fifty, so they get their own
    list rather than a filter an operator has to remember to apply.

    Includes FORMER advisors, flagged as such: an equity relationship that
    outlives a tier change is exactly the thing an operator must not lose track
    of, and it is still generating disclosures on shipped records.
    """
    store = _store()
    rows = [u for u in store.list_users() if _holds_advisory_relationship(u)]
    # Two grouped queries for the whole page rather than two per advisor inside
    # the loop, each on a fresh SQLite connection (audit L6).
    ids = [u["id"] for u in rows]
    ref_counts = store.referral_counts_by_referrer(ids)
    signoff_counts = store.signoff_counts_by_advisor(ids)
    out = []
    for u in rows:
        block = _advisor_public(u)
        current = u.get("tier") == asc_caps.ADVISOR
        block["current"] = current
        # Named plainly rather than implied by an absent tier: "held equity,
        # no longer holds the tier" is a state an admin has to be able to read.
        block["relationship_status"] = "active" if current else "former_tier_changed"
        counts = ref_counts.get(u["id"]) or {"total": 0, "active": 0}
        block["referrals_invited"] = counts["total"]
        block["referrals_active"] = counts["active"]
        block["signoffs"] = signoff_counts.get(u["id"], 0)
        out.append(block)
    out.sort(key=lambda b: (not b["current"], b.get("email") or ""))
    return {"advisors": out, "count": len(out),
            "active": sum(1 for b in out if b["current"])}


# ═══ Referrals (§3) ══════════════════════════════════════════════════════════
class ReferralBody(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    note: Optional[str] = None


def _referral_public(r: Dict[str, Any]) -> Dict[str, Any]:
    """What a referrer is entitled to see about the person they referred.

    Built by WHITELIST, and deliberately thin (§3.3): name, when, where they are
    in the funnel, in plain words. NOT the invitee's NPI, tier score, or
    verification internals — a referrer is not entitled to the credentialing
    file of the person they referred, and building this by stripping fields
    would leak the next one somebody adds upstream.
    """
    status = r.get("status")
    return {
        "referral_id": r.get("referral_id"),
        "invitee_name": r.get("invitee_name"),
        "invitee_email": r.get("invitee_email"),
        "note": r.get("note"),
        "status": status,
        "status_word": _STATUS_WORDS.get(status or "", "Invited"),
        "invited_at": r.get("invited_at"),
        "resolved_at": r.get("resolved_at"),
    }


# Plain words, never a raw token — and "Not heard back" is its own state,
# distinct from "Declined". NULL means we have not heard back, which is not a no.
_STATUS_WORDS = {
    "invited": "Invited",
    "signed_up": "Signed up",
    "verified": "Verifying",
    "approved": "Active",
    "declined": "Declined",
}


@router.get("/api/asclepius/advisor/referrals")
async def my_referrals(advisor: Dict[str, Any] = Depends(require_refer)):
    """This advisor's own funnel. Scoped from the SESSION — there is no user_id
    parameter to tamper with, which is the IDOR rule from the portal work
    applied at the design level rather than validated after the fact."""
    store = _store()
    rows = store.list_referrals_by_referrer(advisor["id"])
    counts = {"invited": 0, "signed_up": 0, "verified": 0, "approved": 0, "declined": 0}
    for r in rows:
        key = r.get("status") or "invited"
        if key in counts:
            counts[key] += 1
    return {
        "referral_code": advisor.get("referral_code"),
        "invite_url": _invite_url(advisor.get("referral_code")),
        "referrals": [_referral_public(r) for r in rows],
        "counts": counts,
        "total": len(rows),
        "active": counts["approved"],
    }


def _header_safe(text: str, *, limit: int = 120) -> str:
    """A string safe to place in an email header. Collapses CR/LF (and the
    unicode line separators an email library may normalize into them) to a
    space, then bounds the length."""
    collapsed = re.sub(r"[\r\n  ]+", " ", text or "")
    return " ".join(collapsed.split())[:limit]


def _portal_base() -> str:
    return (os.getenv("ASCLEPIUS_PORTAL_URL") or os.getenv("LANDING_URL")
            or os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")


def _landing_base() -> str:
    return (os.getenv("LANDING_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")


def _invite_url(code: Optional[str]) -> Optional[str]:
    """The bare link an advisor can paste into a text message.

    Points at the EXISTING physician signup page (``/physicians`` on the
    landing site), not at a referral-specific route — there is no such route,
    and a shareable link that 404s is worse than no shareable link.

    It carries the code as a query parameter for provenance, but attribution
    does NOT depend on that code surviving the round trip: the referral resolves
    on the invitee's email at provisioning time (see
    ``store.claim_referral_for_signup``), so a link stripped by a messaging app
    still attributes correctly as long as they sign up with the address they
    were invited at.
    """
    if not code:
        return None
    return f"{_landing_base()}/physicians?ref={code}"


@router.post(
    "/api/asclepius/advisor/referrals",
    # An IP-keyed limit stays as a cheap outer wall against a single noisy
    # source, but the limits that matter are keyed on the advisor and on the
    # fleet — see _throttle_referral, applied inside the handler where the
    # authenticated identity is available.
    dependencies=[Depends(rate_limiter("asclepius_advisor_referral_ip", 20, 600))],
)
async def create_referral(
    body: ReferralBody,
    request: Request,
    advisor: Dict[str, Any] = Depends(require_refer),
):
    """Invite a physician. Reuses the existing Asclepius invite email — there is
    exactly one invite email in this product — with one added line naming the
    referrer. A named referral converts several times better than a cold invite,
    and that sentence is the entire mechanism (§3.2)."""
    _throttle_referral(advisor)
    store = _store()
    email = str(body.email).lower().strip()
    # Per-invitee cap, matching the public path's "3 pending per 24h": the same
    # address must not be invitable without bound by rotating advisors.
    if store.count_recent_referrals_for_email(email, hours=24) >= _REFERRALS_PER_INVITEE_24H:
        raise HTTPException(
            status_code=429,
            detail="That address has already been invited several times recently.")
    code = advisor.get("referral_code")
    if not code:
        # Every advisor gets a code at appointment; an advisor without one is a
        # data problem, not a reason to silently skip attribution.
        raise HTTPException(
            status_code=409,
            detail="Your referral code is missing. Ask an admin to re-run your "
                   "appointment so a code is minted.")

    existing_user = store.get_user_by_email(email)
    # Only a PHYSICIAN account counts as "already a member" (audit M4).
    # ``get_user_by_email`` spans every role, so an unfiltered check turned this
    # endpoint into a clean account-existence oracle for admin, buyer and
    # hospital-contact addresses — a probe any advisor could run, one address at
    # a time, against a company inbox. A non-physician address gets the ordinary
    # invited response and no account is created either way.
    is_member = bool(existing_user and existing_user.get("role") == "evaluator"
                     and not existing_user.get("is_mock"))
    if store.has_referral_for_email(advisor["id"], email):
        return {"ok": True, "already": "invited",
                "message": "You already invited this physician."}
    if is_member:
        # Not a failure — a fact. Record the referral so the advisor sees an
        # honest row instead of an error, and never create a duplicate account.
        ref = store.insert_referral(
            referrer_id=advisor["id"], referral_code=code, invitee_email=email,
            invitee_name=(body.name or "").strip() or None,
            note=(body.note or "").strip() or None, status="signed_up")
        store.advance_referral(ref["referral_id"], "signed_up")
        return {"ok": True, "already": "member",
                "message": "That physician is already a member.",
                "referral": _referral_public(store.get_referral(ref["referral_id"]))}

    ref = store.insert_referral(
        referrer_id=advisor["id"], referral_code=code, invitee_email=email,
        invitee_name=(body.name or "").strip() or None,
        note=(body.note or "").strip() or None, status="invited")

    sent = await _send_referral_invite(request, advisor, body, email, code)
    store.log_event(
        entity_type="user", entity_id=advisor["id"], event_type="referral_invited",
        actor=advisor["email"],
        payload={"referral_id": ref["referral_id"], "email_sent": sent},
    )
    return {
        "ok": True,
        "referral": _referral_public(store.get_referral(ref["referral_id"])),
        "email_sent": sent,
        # Surfaced rather than swallowed: if email is not configured, the
        # advisor gets a link to send themselves instead of a silent no-op.
        "invite_url": _invite_url(code),
    }


async def _send_referral_invite(request: Request, advisor: Dict[str, Any],
                                body: ReferralBody, email: str, code: str) -> bool:
    """Best-effort delivery of the invite. Never fails the request: the referral
    row and the shareable link are the deliverable, and losing the row because
    SendGrid was down would lose the attribution permanently.

    The link points at the PUBLIC physician-contributor entry point, and this
    function deliberately mints nothing (audit H4). It previously called
    ``team_store.create_health_system_invite`` directly, which inserts a
    pending tenant row carrying a live 30-day token — and completing that wizard
    provisions an account with ``role="admin"``. The public endpoint that mints
    the same artifact layers five guards on it (per-IP limit, a global
    volumetric cap, a per-email pending cap, a 7-day expiry, a honeypot) and a
    lead-provenance row; reaching past it inherited none of them. A stolen
    advisor token rotated across a proxy pool would have minted unlimited
    admin-provisioning invites from a verified sending domain.

    So the invitee lands on ``/physicians``, enters their address, and gets
    their onboarding link from the guarded path like everybody else.
    Attribution does not suffer: the referral resolves on the invitee's email at
    provisioning time, not on a token surviving the round trip.
    """
    from email_utils import is_email_transport_configured, send_html_email
    from onboarding_emails import build_asclepius_invite_email

    if not is_email_transport_configured():
        return False
    # NEVER fall back to the advisor's email address here. This string goes into
    # the subject line and body of a message to a THIRD PARTY, so an advisor
    # appointed by email with no name on file would have had their address
    # disclosed to every physician they invited — and "toby@gmail.com suggested
    # you'd be a good fit" is not the sentence that makes a named referral work.
    # No name means no named referral: the invite falls back to the ordinary
    # copy rather than inventing or leaking one.
    # Collapse any newline before this reaches a Subject header (audit L8).
    # SendGrid's JSON transport is immune, but the SMTP fallback assigns the
    # string straight into a MIME header, where a CR/LF is header injection.
    # Only an admin can set a display name today, which is what keeps it low —
    # but "the only person who can exploit this is trusted" is a fact about
    # today's permissions, not a property of the code.
    referrer_name = _header_safe((advisor.get("full_name") or "").strip())
    onboarding_url = _invite_url(code) or _portal_base()

    html_body = build_asclepius_invite_email(
        invitee_first_name=((body.name or "").strip().split(" ")[0] if body.name else ""),
        # The email's own copy says "<X> has invited you to join"; with no name
        # on file, "a colleague" is the honest filler and reveals nothing.
        director_full_name=referrer_name or "a colleague",
        role_label="Physician contributor",
        org_name="Archangel Health",
        specialty=(advisor.get("specialty") or ""),
        onboarding_url=onboarding_url,
        invitee_email=email,
        referrer_name=referrer_name,
    )
    subject = (f"{referrer_name} suggested you'd be a good fit for Asclepius"
               if referrer_name
               else "You're invited to contribute to Asclepius")
    try:
        return bool(await send_html_email(email, subject, html_body))
    except Exception:
        log.exception("[advisor] referral invite email failed (referral row stands)")
        return False


# ═══ Advisory sign-off (§4) ══════════════════════════════════════════════════
@router.get("/api/asclepius/advisor/queue")
async def advisory_queue(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """Everything awaiting this advisor's eye, by artifact type. Only the types
    their capabilities actually cover are counted — an advisor should not be
    shown a queue they cannot open."""
    store = _store()
    if not any(asc_caps.can(user, c) for c in _CAP_FOR_ARTIFACT.values()):
        raise HTTPException(status_code=403, detail="Advisor tier required")
    out: Dict[str, Any] = {}
    if asc_caps.can(user, asc_caps.SIGNOFF_TASKS):
        batches = store.list_open_task_batches()
        summary = store.signoff_summary("task_batch", [b["batch_key"] for b in batches])
        out["task_batch"] = [
            {**b, "signoffs": summary.get(b["batch_key"], {}).get("n", 0),
             "latest_verdict": summary.get(b["batch_key"], {}).get("verdict")}
            for b in batches
        ]
    if asc_caps.can(user, asc_caps.SIGNOFF_EXPORT):
        exports = store.list_exports(limit=50)
        summary = store.signoff_summary("export_bundle", [e["export_id"] for e in exports])
        out["export_bundle"] = [
            {"export_id": e["export_id"], "created_at": e.get("created_at"),
             "record_count": e.get("record_count"),
             "profile": (e.get("manifest") or {}).get("profile"),
             "signoff_status": e.get("signoff_status"),
             "signoffs": summary.get(e["export_id"], {}).get("n", 0),
             "latest_verdict": summary.get(e["export_id"], {}).get("verdict")}
            for e in exports
        ]
    if asc_caps.can(user, asc_caps.SIGNOFF_INTAKE):
        uploads = store.list_ingest_uploads(limit=50)
        summary = store.signoff_summary("inbound_upload", [u["upload_id"] for u in uploads])
        out["inbound_upload"] = [
            {"upload_id": u["upload_id"], "created_at": u.get("created_at"),
             "status": u.get("status"),
             "partner_id": u.get("partner_id"),
             "health_system_id": u.get("health_system_id"),
             "signoff_status": u.get("signoff_status"),
             "signoffs": summary.get(u["upload_id"], {}).get("n", 0),
             "latest_verdict": summary.get(u["upload_id"], {}).get("verdict")}
            for u in uploads
        ]
    if asc_caps.can(user, asc_caps.SIGNOFF_SPEC):
        specs = store.list_product_specs()
        summary = store.signoff_summary("product_spec", [s["spec_id"] for s in specs])
        out["product_spec"] = [
            {"spec_id": s["spec_id"], "title": s.get("title"),
             "created_at": s.get("created_at"),
             "signoffs": summary.get(s["spec_id"], {}).get("n", 0),
             "latest_verdict": summary.get(s["spec_id"], {}).get("verdict")}
            for s in specs
        ]
    return {"queue": out, "counts": {k: len(v) for k, v in out.items()}}


@router.get("/api/asclepius/advisor/artifacts/{artifact_type}/{artifact_id:path}")
async def advisory_artifact(
    artifact_type: str,
    artifact_id: str,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """The artifact an advisor examines before recording a verdict.

    THE SECURITY BOUNDARY (§4.3). An advisor MAY see de-identified clinical
    content — they are a credentialed physician under the same agreement as
    every other labeler, and reviewing the intake pipeline is the job. An
    advisor MAY NOT see the raw pre-de-identification hospital upload (that path
    is ``require_admin`` and stays that way), and MAY NOT see sealed ground
    truth for a case they might later be routed to label — which is why the
    task-batch view runs every case through ``public_case``.
    """
    capability = _CAP_FOR_ARTIFACT.get(artifact_type)
    if capability is None:
        raise HTTPException(
            status_code=400,
            detail=f"artifact_type must be one of {', '.join(ARTIFACT_TYPES)}")
    if not asc_caps.can(user, capability):
        raise HTTPException(status_code=403, detail=f"'{capability}' capability required")
    # The ``:path`` converter is here because a derived batch key can carry
    # separators; it also means the raw segment can contain ``..``. Every
    # handler below resolves through a DB lookup rather than the filesystem, so
    # this is belt to those braces — but the export view DOES open files under a
    # directory read from the row, and an id that looks like a traversal has no
    # legitimate reason to reach it.
    if ".." in artifact_id or artifact_id.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid artifact id")
    store = _store()
    if artifact_type == "task_batch":
        return _task_batch_view(store, artifact_id)
    if artifact_type == "export_bundle":
        return await run_in_threadpool(_export_bundle_view, store, artifact_id)
    if artifact_type == "inbound_upload":
        return _inbound_upload_view(store, artifact_id)
    return _product_spec_view(store, artifact_id)


def _task_batch_view(store: Any, batch_key: str) -> Dict[str, Any]:
    """Generated cases before they reach the labeling queue: prompt, candidate
    answers, rubric. The case is served through ``public_case`` and the answer
    key is never included — an advisor who previews a batch may later be routed
    to label one of these cases, and a previewed answer key would contaminate
    their own submission and every κ that submission touches."""
    # The SAME cap the queue counts with and the sign-off records — see
    # _BATCH_MEMBER_CAP. An advisor must not approve "the batch" having been
    # shown a differently-truncated slice of it than the count they were given.
    tasks = store.list_tasks_in_batch(batch_key, limit=_BATCH_MEMBER_CAP)
    if not tasks:
        raise HTTPException(status_code=404, detail="No open tasks in that batch")
    return {
        "artifact_type": "task_batch",
        "artifact_id": batch_key,
        "n_tasks": len(tasks),
        "truncated": len(tasks) >= _BATCH_MEMBER_CAP,
        "tasks": [
            {
                "task_id": t.get("task_id"),
                "specialty": t.get("specialty"),
                "difficulty": t.get("difficulty"),
                "modality": t.get("modality"),
                "prompt": t.get("prompt"),
                "case": public_case(t.get("case")),
                "candidate_answers": [
                    {"id": c.get("id"), "text": c.get("text")}
                    for c in (t.get("candidate_answers") or []) if isinstance(c, dict)
                ],
                "grounding_mode": t.get("grounding_mode"),
            }
            for t in tasks
        ],
        "signoffs": store.list_advisory_signoffs(
            artifact_type="task_batch", artifact_id=batch_key),
    }


# Keys stripped from every sampled export record before an advisor sees it
# (audit M1). An advisor holds REVIEW and SIGNOFF_EXPORT at the same time, and
# `blinded_review_view` correctly serves `submission_id` with zero identity — so
# an advisor could note the id from a blinded review, page through export
# samples until it appeared, and read off the labeler's stable pseudonym,
# specialty and experience band. In a fifty-physician pool with a handful of
# nephrologists that names them, and the referral funnel already gives the
# advisor real names and emails for everyone they referred.
#
# Two things have to go, not one: the LINKAGE key (submission/task id, which
# ties a blinded review to a row) and the stable PSEUDONYM (any hashed id, which
# ties rows to each other across bundles). Removing either alone leaves the
# attack half-working.
_SAMPLE_IDENTITY_KEYS = frozenset({
    "submission_id", "task_id", "record_id", "evaluator_id",
    "annotator_id_hashed", "annotator_years_experience",
    "annotator_years_experience_band",
})
# Annexes carrying their own hashed ids — supervision.labeler_id_hashed is the
# same pseudonym by another name.
_SAMPLE_DROP_BLOCKS = frozenset({"supervision", "review"})


def _sampled_record_view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """One sampled export record as an advisor may see it.

    Built by REMOVAL of a named set rather than a positive whitelist, on
    purpose: the sample's whole job is to show an advisor what is actually about
    to ship, field for field, so a whitelist here would quietly hide the thing
    they are being asked to judge. The credential stays — it is the premium
    claim the advisor is checking — but nothing that links one row to another
    row or to a review does.
    """
    out = {k: v for k, v in rec.items()
           if k not in _SAMPLE_IDENTITY_KEYS and k not in _SAMPLE_DROP_BLOCKS}
    return out


def _public_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """The manifest minus operational internals (audit L1).

    ``dir_path`` is a server filesystem path and ``created_by`` is an admin's
    email address; neither is part of judging a bundle, and every other advisor
    payload in this module is a deliberate whitelist.

    Delegated to ``export._shippable_manifest`` so this list and the one applied
    to the ``batch.json`` a buyer actually downloads cannot drift. They were two
    copies of the same three keys, which is precisely how one of them ends up
    missing the fourth.
    """
    return asc_export._shippable_manifest(manifest or {})


def _export_bundle_view(store: Any, export_id: str) -> Dict[str, Any]:
    """The manifest and a SAMPLED slice of what is about to ship to a buyer:
    schema, field list, data dictionary, N sampled records. Synchronous file
    reads — reached through a threadpool by the caller.

    The sample is de-linked first — see ``_SAMPLE_IDENTITY_KEYS``.
    """
    export = store.get_export(export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="No such export")
    manifest = export.get("manifest") or {}
    dir_path = export.get("dir_path") or manifest.get("dir_path") or ""
    sample: List[Dict[str, Any]] = []
    dictionary = None
    if dir_path and os.path.isdir(dir_path):
        jsonl = os.path.join(dir_path, "records.jsonl")
        if os.path.exists(jsonl):
            with open(jsonl, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i >= _EXPORT_SAMPLE_N:
                        break
                    try:
                        sample.append(json.loads(line))
                    except ValueError:
                        continue
        dict_path = os.path.join(dir_path, "data_dictionary.md")
        if os.path.exists(dict_path):
            with open(dict_path, encoding="utf-8") as f:
                dictionary = f.read()
    # The FULL field list is computed before de-linking, because "what fields
    # does this bundle ship" is exactly the question the advisor is reviewing —
    # they must be able to see that lineage and annotator fields go to the
    # buyer, even though the sample they read is de-linked.
    field_list = sorted({k for rec in sample for k in rec.keys()})
    sample = [_sampled_record_view(r) for r in sample]
    return {
        "artifact_type": "export_bundle",
        "artifact_id": export_id,
        "manifest": _public_manifest(manifest),
        "record_count": export.get("record_count"),
        "field_list": field_list,
        "data_dictionary_md": dictionary,
        "sample": sample,
        "sample_n": len(sample),
        # Said plainly, so an advisor does not mistake the de-linked sample for
        # what ships and report a field as missing.
        "sample_delinked": sorted(_SAMPLE_IDENTITY_KEYS | _SAMPLE_DROP_BLOCKS),
        "sample_note": ("Lineage and annotator-identity fields are withheld from "
                        "this preview so an export sample cannot be used to "
                        "de-blind a reviewed submission. They are present in the "
                        "shipped bundle — see the field list above."),
        "signoff_status": export.get("signoff_status"),
        "signoffs": store.list_advisory_signoffs(
            artifact_type="export_bundle", artifact_id=export_id),
    }


# Case statuses whose BODY has provably been through ``cf.deidentify()``.
#
# This is a WHITELIST, and it is the most important line in this file. In
# ``ingestion.py`` the de-identification call sits on the SUCCESS path only:
# a case that quarantines never reaches it, and its stored body is the merged
# hospital fragment. Worse, a case quarantines *precisely because* the
# residual-identifier scanner flagged it or the timeline normalizer found raw
# dates — so the un-de-identified bodies are exactly the ones most likely to
# carry names, MRNs, dates of birth and SSNs.
#
# 'ingested' and 'needs_review' are both set after ``deidentify()`` succeeds;
# 'promoted' was 'ingested' first. 'quarantined' and 'rejected' are NOT, and a
# blocklist would silently admit the next status somebody adds.
#
# ``public_case()`` is not a second line of defence here: it strips
# ``ground_truth``/``hard_hook``/``reasoning_divergence``. It is an answer-key
# stripper, not a de-identifier.
_DEIDENTIFIED_CASE_STATUSES = frozenset({"ingested", "needs_review", "promoted"})


def _safe_quarantine_reason(reason: Optional[str]) -> Optional[str]:
    """A category, never the exception text.

    ``report['quarantine_reason']`` is ``str(exc)`` and the exceptions quote the
    tokens that caused the failure — which, for a de-id flag or an unresolved
    timeline, ARE the identifiers. The advisor needs to know a case failed and
    what kind of failure it was; they do not need the offending string, and the
    masked findings below already carry the shape of it.
    """
    text = (reason or "").strip().lower()
    if not text:
        return None
    for needle, label in (
        ("de-id verification flagged", "de-identification scan flagged residual identifiers"),
        ("unresolved date-like", "timeline could not be normalized (ambiguous date tokens)"),
        ("sealed answer key", "referring institution's adjudication could not be stored"),
        ("completeness", "declared modality missing or unverified"),
        ("answer leak", "answer-key leakage detected in the case body"),
    ):
        if needle in text:
            return label
    return "case failed intake validation"


def _intake_case_view(case: Dict[str, Any]) -> Dict[str, Any]:
    """One ingest case as an advisor may see it — built by WHITELIST.

    The body rides only for statuses in ``_DEIDENTIFIED_CASE_STATUSES``. For
    everything else the advisor still sees THAT the case failed and what kind of
    failure it was, from the masked findings — which is the substance of an
    intake review — but never the body. This mirrors the rule the admin
    quarantine endpoint already applies (``routers/asclepius.py``:
    ``c.pop("case", None)``); withholding the body from an advisor while showing
    it to nobody else was never the intent.
    """
    status = case.get("status")
    report = dict(case.get("report") or {})
    # verify_deid findings are masked at source (``snippet_masked`` replaces
    # every alphanumeric), so they are safe to serve verbatim.
    if "quarantine_reason" in report:
        report["quarantine_reason"] = _safe_quarantine_reason(report.get("quarantine_reason"))
    # str(exc) again, same problem.
    report.pop("unresolved_after_scrub", None)
    out: Dict[str, Any] = {
        "ingest_case_id": case.get("ingest_case_id"),
        "specialty": case.get("specialty"),
        "status": status,
        "report": report,
    }
    body = None
    withheld_reason = None
    if status not in _DEIDENTIFIED_CASE_STATUSES:
        withheld_reason = "status"
    else:
        # De-identified at ingest; public_case additionally guarantees a
        # promoted case's answer key cannot ride along.
        body = public_case(case.get("case"))
        # SECOND LINE, independent of the status whitelist. The whitelist is
        # only as true as an invariant held by convention across four call sites
        # in two files: "nothing sets 'ingested' without also writing a
        # de-identified body". That happens to hold today. C1 was precisely a
        # false assumption about de-identification, so the bytes about to be
        # served are re-scanned with the same verifier the ingest pipeline uses,
        # and a flagged body is withheld regardless of what its status says.
        if body and _residual_identifier_kinds(body):
            body, withheld_reason = None, "residual_identifiers"
    out["case"] = body
    out["body_withheld"] = withheld_reason is not None
    if withheld_reason:
        out["body_withheld_reason"] = withheld_reason
    else:
        out["override_reason"] = case.get("override_reason")
    return out


def _residual_identifier_kinds(body: Dict[str, Any]) -> List[str]:
    """Masked finding kinds from re-scanning a body we are about to serve.

    Never raises: a verifier that is unavailable must not take down the intake
    review — but it also must not silently vouch for the body, so a failure is
    treated as "flagged" by the caller returning a non-empty list.
    """
    try:
        from asclepius import deid_verify

        result = deid_verify.verify_deid(body or {})
        return sorted({f.get("kind") for f in (result.get("findings") or [])
                       if f.get("kind")})
    except Exception:
        log.exception("[advisor] de-id re-scan failed; withholding the body")
        return ["scan_unavailable"]


def _inbound_upload_view(store: Any, upload_id: str) -> Dict[str, Any]:
    """The DE-IDENTIFIED ingest cases from a hospital and their intake findings —
    completeness, residual-identifier scan, and the KIND of any quarantine.

    The raw pre-de-identification bundle is NEVER proxied here. It sits behind
    ``GET /api/asclepius/ingestion/uploads/{id}/download``, which is
    ``require_admin``, and it stays there.

    Neither is the quarantined case BODY, which is the same PHI reached through
    a different door — the database rather than the filesystem. An advisor is an
    outside contractor holding equity, not an employee. See
    ``_DEIDENTIFIED_CASE_STATUSES``.
    """
    upload = store.get_ingest_upload(upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="No such upload")
    cases = store.list_ingest_cases(upload_id=upload_id, limit=200)
    return {
        "artifact_type": "inbound_upload",
        "artifact_id": upload_id,
        "upload": {
            "upload_id": upload.get("upload_id"),
            "status": upload.get("status"),
            "created_at": upload.get("created_at"),
            "partner_id": upload.get("partner_id"),
            "health_system_id": upload.get("health_system_id"),
            # NOT ``filename``. It is chosen by the sending institution and is
            # entirely uncontrolled free text — "SMITH_JOHN_2024.zip" and
            # "MRN88213347.zip" are both realistic, and there is no way to
            # sanitize an arbitrary string that might be a patient name. Same
            # leak class as the quarantined body, through a smaller door.
            # ``partner_id`` and ``health_system_id`` are identifiers WE mint,
            # which is what makes them safe to show.
            "signoff_status": upload.get("signoff_status"),
        },
        "n_cases": len(cases),
        "n_bodies_withheld": sum(
            1 for c in cases if c.get("status") not in _DEIDENTIFIED_CASE_STATUSES),
        "cases": [_intake_case_view(c) for c in cases],
        "signoffs": store.list_advisory_signoffs(
            artifact_type="inbound_upload", artifact_id=upload_id),
    }


def _product_spec_view(store: Any, spec_id: str) -> Dict[str, Any]:
    spec = store.get_product_spec(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="No such product spec")
    return {
        "artifact_type": "product_spec",
        "artifact_id": spec_id,
        "title": spec.get("title"),
        "body_md": spec.get("body_md"),
        "created_at": spec.get("created_at"),
        "signoffs": store.list_advisory_signoffs(
            artifact_type="product_spec", artifact_id=spec_id),
    }


class SignoffBody(BaseModel):
    artifact_type: str
    artifact_id: str
    verdict: str
    comments: Optional[str] = None
    # Accepted by the model so a client that sends it gets a clean 200 rather
    # than a validation error — and then IGNORED. See _relationship_for: the
    # server writes this field from the advisor's own row, always.
    relationship: Optional[str] = None


@router.post(
    "/api/asclepius/advisor/signoffs",
    dependencies=[Depends(rate_limiter("asclepius_advisor_signoff", 60, 600))],
)
async def record_signoff(
    body: SignoffBody,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Record an advisory verdict on an artifact.

    Sign-off is RECORDED, never blocking. An export always builds and always
    ships; this is the advisor's comment on it, surfaced in admin next to the
    thing it describes. One advisor with a day job must not sit on the revenue
    path — so there is no gate here to turn on, by design.
    """
    artifact_type = (body.artifact_type or "").strip()
    capability = _CAP_FOR_ARTIFACT.get(artifact_type)
    if capability is None:
        raise HTTPException(
            status_code=400,
            detail=f"artifact_type must be one of {', '.join(ARTIFACT_TYPES)}")
    if not asc_caps.can(user, capability):
        raise HTTPException(status_code=403, detail=f"'{capability}' capability required")
    verdict = (body.verdict or "").strip()
    if verdict not in VERDICTS:
        raise HTTPException(
            status_code=400, detail=f"verdict must be one of {', '.join(VERDICTS)}")
    comments = (body.comments or "").strip()
    if verdict == "changes_requested" and not comments:
        # Same rule as PRD A's "reject requires a reason", for the same reason:
        # an unexplained rejection is unusable to everyone downstream.
        raise HTTPException(
            status_code=400,
            detail="changes_requested requires comments explaining what to change.")
    if verdict == "approved_with_comments" and not comments:
        raise HTTPException(
            status_code=400,
            detail="approved_with_comments requires comments — otherwise it is "
                   "a plain approval wearing the wrong label.")

    store = _store()
    artifact_id = (body.artifact_id or "").strip()
    if not _artifact_exists(store, artifact_type, artifact_id):
        raise HTTPException(status_code=404, detail="No such artifact")

    # Idempotency (audit L2). Two advisors signing the same artifact is the
    # point and both rows must persist — but the SAME advisor re-submitting an
    # identical verdict is a double-click or a retried request, and five
    # identical rows make the history unreadable and the counts wrong. A CHANGED
    # verdict from the same advisor is a real event and still records: people
    # revise their opinion, and the trail should show that they did.
    for prior in store.list_advisory_signoffs(
            artifact_type=artifact_type, artifact_id=artifact_id,
            advisor_id=user["id"], limit=20):
        if prior.get("verdict") == verdict and (prior.get("comments") or "") == (
                comments or ""):
            return {"ok": True, "signoff": prior, "blocking": False,
                    "duplicate": True}

    # Pin WHAT was signed off (audit M3). A task_batch id is derived —
    # ``specialty:YYYY-MM-DD`` over tasks still ``status='open'`` — so its
    # membership keeps changing after the attestation: tasks generated later the
    # same day join the batch and inherit an approval nobody gave them, and a
    # task labeled out of the queue leaves it. Resolving the ids at write time
    # is what makes the subject of the attestation reconstructible later.
    subject_ids: Optional[List[str]] = None
    if artifact_type == "task_batch":
        subject_ids = [t["task_id"] for t in
                       store.list_tasks_in_batch(artifact_id, limit=_BATCH_MEMBER_CAP)]

    signoff = store.insert_advisory_signoff(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        advisor_id=user["id"],
        verdict=verdict,
        comments=comments or None,
        # Server-written, always. The client's value never reaches the database.
        relationship=_relationship_for(user),
        subject_ids=subject_ids,
    )
    _mirror_signoff_status(store, artifact_type, artifact_id, verdict)
    store.log_event(
        entity_type="advisory_signoff", entity_id=signoff["signoff_id"],
        event_type="advisory_signoff_recorded", actor=user["id"],
        payload={"artifact_type": artifact_type, "artifact_id": artifact_id,
                 "verdict": verdict, "relationship": signoff["relationship"]},
    )
    return {"ok": True, "signoff": signoff, "blocking": False}


def _artifact_exists(store: Any, artifact_type: str, artifact_id: str) -> bool:
    if not artifact_id:
        return False
    if artifact_type == "task_batch":
        return bool(store.list_tasks_in_batch(artifact_id, limit=1))
    if artifact_type == "export_bundle":
        return store.get_export(artifact_id) is not None
    if artifact_type == "inbound_upload":
        return store.get_ingest_upload(artifact_id) is not None
    return store.get_product_spec(artifact_id) is not None


def _mirror_signoff_status(store: Any, artifact_type: str, artifact_id: str,
                           verdict: str) -> None:
    """Copy the WORST outstanding verdict onto the artifact's own row so admin
    lists show it without a join. Advisory only — nothing reads it to decide
    whether work may proceed.

    Worst, not latest (audit M2). Last-write-wins meant a second advisor
    approving after a first requested changes silently flipped the field an
    operator reads before shipping from 'changes_requested' to 'approved'. Both
    rows always survived in ``advisory_signoffs``, so the evidence was never
    lost — but the summary said the opposite of the evidence, which is worse
    than saying nothing.
    """
    target = {
        "export_bundle": ("exports", "export_id"),
        "inbound_upload": ("ingest_uploads", "upload_id"),
    }.get(artifact_type)
    if target is None:
        # A task_batch is a derived key spanning many rows; stamping each task
        # would be a write amplification with no reader. The signoff table is
        # the record.
        return
    try:
        summary = store.signoff_summary(artifact_type, [artifact_id]).get(artifact_id) or {}
        store.set_signoff_status(target[0], target[1], artifact_id,
                                 summary.get("verdict") or verdict)
    except Exception:
        log.exception("[advisor] could not mirror signoff_status (the signoff row stands)")


@router.get("/api/asclepius/advisor/signoffs")
async def my_signoffs(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """This advisor's own sign-off history, newest first."""
    if not any(asc_caps.can(user, c) for c in _CAP_FOR_ARTIFACT.values()):
        raise HTTPException(status_code=403, detail="Advisor tier required")
    return {"signoffs": _store().list_advisory_signoffs(advisor_id=user["id"])}


# ═══ Product specs — admin puts a document up for comment ════════════════════
class ProductSpecBody(BaseModel):
    title: str
    body_md: str


@router.post("/api/asclepius/admin/product-specs")
async def create_product_spec(
    body: ProductSpecBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    title = (body.title or "").strip()
    body_md = (body.body_md or "").strip()
    if not title or not body_md:
        raise HTTPException(status_code=400, detail="title and body_md are required")
    spec = _store().insert_product_spec(
        title=title, body_md=body_md, created_by=admin["email"])
    return {"ok": True, "spec": spec}


@router.get("/api/asclepius/admin/signoffs")
async def all_signoffs(
    artifact_type: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Every advisory verdict, for the admin surface. The relationship rides on
    each row — that is the disclosure, and it is only useful if it is visible
    where the decision is read."""
    store = _store()
    rows = store.list_advisory_signoffs(artifact_type=artifact_type, limit=500)
    names = {u["id"]: (u.get("full_name") or u.get("email")) for u in store.list_users()}
    return {
        "signoffs": [{**r, "advisor_name": names.get(r["advisor_id"])} for r in rows],
        "count": len(rows),
    }
