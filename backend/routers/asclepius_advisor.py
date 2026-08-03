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
import secrets
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from starlette.concurrency import run_in_threadpool

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius.cases import public_case
from asclepius.store import get_store

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
    if body.name:
        store.set_advisor_display_name(user["id"], body.name)

    # An appointed advisor is verified BY the appointment: the admin has a
    # signed agreement in hand, which outranks anything an NPPES lookup could
    # add. Recorded through the normal decision path so verified_by/verified_at
    # are stamped exactly as they are for every other approval.
    store.record_verification_decision(
        user["id"],
        status="approved",
        decided_by=admin["email"],
        tier=asc_caps.ADVISOR,
        note=f"Appointed medical advisor · agreement {agreement_ref}",
    )
    updated = store.appoint_advisor(
        user["id"], agreement_ref=agreement_ref, appointed_by=admin["email"])
    if updated is None:
        raise HTTPException(status_code=404, detail="No such user")
    store.log_event(
        entity_type="user", entity_id=user["id"], event_type="advisor_appointed",
        actor=admin["email"],
        payload={"agreement_ref": agreement_ref, "provisioned": provisioned,
                 "compensation_model": updated.get("compensation_model")},
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


@router.get("/api/asclepius/admin/advisors")
async def list_advisors(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """The advisor roster — three people among fifty, so they get their own
    list rather than a filter an operator has to remember to apply."""
    store = _store()
    rows = [u for u in store.list_users() if u.get("tier") == asc_caps.ADVISOR]
    out = []
    for u in rows:
        block = _advisor_public(u)
        refs = store.list_referrals_by_referrer(u["id"])
        block["referrals_invited"] = len(refs)
        block["referrals_active"] = sum(1 for r in refs if r.get("status") == "approved")
        block["signoffs"] = len(store.list_advisory_signoffs(advisor_id=u["id"], limit=500))
        out.append(block)
    return {"advisors": out, "count": len(out)}


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


@router.post("/api/asclepius/advisor/referrals")
async def create_referral(
    body: ReferralBody,
    request: Request,
    advisor: Dict[str, Any] = Depends(require_refer),
):
    """Invite a physician. Reuses the existing Asclepius invite email — there is
    exactly one invite email in this product — with one added line naming the
    referrer. A named referral converts several times better than a cold invite,
    and that sentence is the entire mechanism (§3.2)."""
    store = _store()
    email = str(body.email).lower().strip()
    code = advisor.get("referral_code")
    if not code:
        # Every advisor gets a code at appointment; an advisor without one is a
        # data problem, not a reason to silently skip attribution.
        raise HTTPException(
            status_code=409,
            detail="Your referral code is missing. Ask an admin to re-run your "
                   "appointment so a code is minted.")

    existing_user = store.get_user_by_email(email)
    if store.has_referral_for_email(advisor["id"], email):
        return {"ok": True, "already": "invited",
                "message": "You already invited this physician."}
    if existing_user is not None:
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
    SendGrid was down would lose the attribution permanently."""
    from email_utils import is_email_transport_configured, send_html_email
    from onboarding_emails import build_asclepius_invite_email

    if not is_email_transport_configured():
        return False
    referrer_name = (advisor.get("full_name") or advisor.get("email") or "").strip()
    onboarding_url = _invite_url(code) or _portal_base()
    try:
        # Mint a real onboarding link when the tenant store is reachable, so the
        # invitee lands in the existing wizard rather than a bare portal page.
        ts = getattr(request.app.state, "team_store", None)
        if ts is not None:
            invite = await run_in_threadpool(
                ts.create_health_system_invite,
                invite_base_url=(os.getenv("LANDING_URL") or _portal_base()),
                expires_days=30,
                director_email=email,
            )
            onboarding_url = f"{invite['onboarding_url']}?ref={code}"
    except Exception:
        log.exception("[advisor] could not mint an onboarding link; using the bare invite URL")

    html_body = build_asclepius_invite_email(
        invitee_first_name=((body.name or "").strip().split(" ")[0] if body.name else ""),
        director_full_name=referrer_name,
        role_label="Physician contributor",
        org_name="Archangel Health",
        specialty=(advisor.get("specialty") or ""),
        onboarding_url=onboarding_url,
        invitee_email=email,
        referrer_name=referrer_name,
    )
    try:
        return bool(await send_html_email(
            email, f"{referrer_name} suggested you'd be a good fit for Asclepius",
            html_body))
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
             "latest_verdict": summary.get(b["batch_key"], {}).get("latest_verdict")}
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
             "latest_verdict": summary.get(e["export_id"], {}).get("latest_verdict")}
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
             "latest_verdict": summary.get(u["upload_id"], {}).get("latest_verdict")}
            for u in uploads
        ]
    if asc_caps.can(user, asc_caps.SIGNOFF_SPEC):
        specs = store.list_product_specs()
        summary = store.signoff_summary("product_spec", [s["spec_id"] for s in specs])
        out["product_spec"] = [
            {"spec_id": s["spec_id"], "title": s.get("title"),
             "created_at": s.get("created_at"),
             "signoffs": summary.get(s["spec_id"], {}).get("n", 0),
             "latest_verdict": summary.get(s["spec_id"], {}).get("latest_verdict")}
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
    tasks = store.list_tasks_in_batch(batch_key)
    if not tasks:
        raise HTTPException(status_code=404, detail="No open tasks in that batch")
    return {
        "artifact_type": "task_batch",
        "artifact_id": batch_key,
        "n_tasks": len(tasks),
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


def _export_bundle_view(store: Any, export_id: str) -> Dict[str, Any]:
    """The manifest and a SAMPLED slice of what is about to ship to a buyer:
    schema, field list, data dictionary, N sampled records. Synchronous file
    reads — reached through a threadpool by the caller."""
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
    field_list = sorted({k for rec in sample for k in rec.keys()})
    return {
        "artifact_type": "export_bundle",
        "artifact_id": export_id,
        "manifest": manifest,
        "record_count": export.get("record_count"),
        "field_list": field_list,
        "data_dictionary_md": dictionary,
        "sample": sample,
        "sample_n": len(sample),
        "signoff_status": export.get("signoff_status"),
        "signoffs": store.list_advisory_signoffs(
            artifact_type="export_bundle", artifact_id=export_id),
    }


def _inbound_upload_view(store: Any, upload_id: str) -> Dict[str, Any]:
    """The DE-IDENTIFIED ingest cases from a hospital and their intake findings —
    completeness, residual-identifier scan, quarantine reasons.

    The raw pre-de-identification bundle is NEVER proxied here. It sits behind
    ``GET /api/asclepius/ingestion/uploads/{id}/download``, which is
    ``require_admin``, and it stays there. That path is the easiest thing in
    this whole build to hand over by accident, because it sits next to the
    de-identified view in the same admin UI.
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
            "filename": upload.get("filename"),
            "signoff_status": upload.get("signoff_status"),
        },
        "n_cases": len(cases),
        "cases": [
            {
                "ingest_case_id": c.get("ingest_case_id"),
                "specialty": c.get("specialty"),
                "status": c.get("status"),
                # Already de-identified at ingest; run it through public_case
                # anyway so a promoted case's answer key can never ride along.
                "case": public_case(c.get("case")),
                # Intake findings: completeness, residual-identifier scan,
                # quarantine reason. This is the substance of the review.
                "report": c.get("report"),
                "override_reason": c.get("override_reason"),
            }
            for c in cases
        ],
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


@router.post("/api/asclepius/advisor/signoffs")
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

    signoff = store.insert_advisory_signoff(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        advisor_id=user["id"],
        verdict=verdict,
        comments=comments or None,
        # Server-written, always. The client's value never reaches the database.
        relationship=_relationship_for(user),
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
    """Copy the latest verdict onto the artifact's own row so admin lists show
    it without a join. Advisory only — nothing reads it to decide whether work
    may proceed."""
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
        store.set_signoff_status(target[0], target[1], artifact_id, verdict)
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
