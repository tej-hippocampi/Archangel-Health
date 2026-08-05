"""Health system onboarding (magic link, email OTP, team invites)."""

import hashlib
import html
import logging
import os
import string
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import secrets
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi import Form
from pydantic import BaseModel, EmailStr, Field
from starlette.concurrency import run_in_threadpool

from ratelimit import client_ip, global_rate_limiter, rate_limiter

from email_utils import is_email_transport_configured, send_html_email
from onboarding_emails import (
    build_asclepius_complete_email,
    build_asclepius_invite_email,
    build_complete_email,
    build_invite_email,
    build_verification_email,
)
from tenant_utils import generate_secure_password

# Mapping of API role values → display labels used in the new email templates.
# The frontend uses the labels directly; the API persists the lowercased token.
# Pass-4 taxonomy: surgeon | rn_coordinator | np_pa. The director slot is a
# `surgeon` with `is_team_director=1` — only the director's row is auto-created
# on `/finish`; the wizard only invites RN and NP/PA seats.
_ROLE_LABELS = {
    "surgeon": "Surgeon",
    "rn_coordinator": "RN Care Coordinator",
    "np_pa": "NP / PA",
}

_INVITABLE_ROLES = {"rn_coordinator", "np_pa"}

# ─── Asclepius (data-training product) onboarding ────────────────────────────
# Clinical-role labels for the people a Director of Data Training invites. These
# describe the human, not the Asclepius RBAC role — every invited clinician is
# provisioned as an Asclepius `evaluator`; the director is an `admin`.
_ASCLEPIUS_MEMBER_ROLES = {
    "physician": "Physician (MD/DO/MBBS)",
    "np": "Nurse Practitioner (NP)",
    "pa": "Physician Assistant (PA)",
    "resident_fellow": "Resident / Fellow",
}
_ASCLEPIUS_DIRECTOR_ROLE_LABEL = "Director of Data Training"
_ASCLEPIUS_TEAM_CAP = 10  # director + up to 10 invited clinicians

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

log = logging.getLogger("onboarding")


def _asc_credentialing():
    """Lazy import — keeps the Asclepius package out of this router's import
    graph for the clinical-only paths."""
    from asclepius import credentialing
    return credentialing


# ─── Signup throttling (B-5.3) ────────────────────────────────────────────────
# A per-IP bucket is the wrong key for this endpoint. A health system egresses
# through one NAT gateway, so the 6th physician of a 10-person team invited in
# the same hour hit a 429 and could not finish signup — and "a physician who
# cannot complete signup on launch day is gone for good". Worse, client_ip()
# uses the LAST X-Forwarded-For hop, which is correct with exactly one
# appending proxy; with Cloudflare AND the platform proxy the last hop is an
# edge IP, so every signup on the planet shared one bucket per PoP.
#
# The onboarding token is the right key: one account per token by
# construction, so a legitimate flow spends one of its attempts and a replayed
# token is exactly what we want to throttle. The per-IP ceiling is kept as a
# much looser abuse guard, and a global limiter backstops both in case the XFF
# chain makes per-IP meaningless.
_SIGNUP_PER_TOKEN = (6, 3600)      # retries of one account's final step
_SIGNUP_PER_IP = (20, 3600)        # a whole team behind one NAT, comfortably
_SIGNUP_GLOBAL = (300, 3600)       # volumetric backstop


async def _signup_rate_guard(request: Request) -> None:
    """Throttle signup completion on the onboarding TOKEN first, then IP."""
    from ratelimit import check, is_enabled

    if not is_enabled():
        return
    token = ""
    try:
        body = await request.json()
        token = str((body or {}).get("token") or "").strip()
    except Exception:
        token = ""
    if token:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
        allowed, retry_after = check(f"asclepius_signup_tok:{digest}", *_SIGNUP_PER_TOKEN)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts for this invitation. Please try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )
    allowed, retry_after = check(
        f"asclepius_signup_ip:{client_ip(request)}", *_SIGNUP_PER_IP)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please slow down and try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )


def _ts(request: Request):
    return request.app.state.team_store


def _asclepius_store(request: Request):
    store = getattr(request.app.state, "asclepius_store", None)
    if store is not None:
        return store
    from asclepius.store import get_store

    return get_store()


def _landing_base() -> str:
    return (os.getenv("LANDING_URL") or "http://localhost:5173").strip().rstrip("/")


def _app_base() -> str:
    return (os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")


def _asclepius_workspace_url() -> str:
    return f"{_app_base()}/asclepius"


def _email_configured() -> bool:
    return is_email_transport_configured()


class OnboardTokenBody(BaseModel):
    token: str = Field(..., min_length=10)


class Step1Body(OnboardTokenBody):
    first_name: str
    last_name: str
    email: EmailStr


class VerifyOtpBody(OnboardTokenBody):
    code: str = Field(..., min_length=6, max_length=6)


class Step3Body(OnboardTokenBody):
    health_system_name: str
    surgery_department: str
    phone: str


class AddMemberBody(OnboardTokenBody):
    full_name: str
    email: EmailStr
    role: str  # rn_coordinator | np_pa  (surgeon is the director, auto-seeded)

class FinishBody(OnboardTokenBody):
    pass


class SelfServeBody(BaseModel):
    email: EmailStr
    # Honeypot — real users never see or fill this; a non-empty value is a bot.
    company_website: str = Field(default="", max_length=200)


# Outstanding self-serve links one inbox can hold at once (rolling 24h).
_SELF_SERVE_EMAIL_CAP = 3
_SELF_SERVE_EXPIRES_DAYS = 7


def _load_hs(request: Request, token: str) -> Dict[str, Any]:
    row = _ts(request).get_health_system_by_onboarding_token(token.strip())
    if not row:
        raise HTTPException(status_code=404, detail="Invalid or expired onboarding link.")
    return row


def _reject_if_completed(row: Dict[str, Any]) -> None:
    if row.get("onboarding_completed_at"):
        raise HTTPException(status_code=410, detail="Onboarding already completed for this link.")


def _serialize_team_member(m: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a team_members row for the onboarding wizard's local list state.

    Maps the API role token (pass-4 taxonomy) to the display label the
    redesigned wizard uses.
    """
    full = (m.get("name") or "").strip()
    first, _, last = full.partition(" ")
    role = (m.get("role") or "").strip().lower()
    role_label = _ROLE_LABELS.get(role, role.title() or "Care Team")
    return {
        "id": int(m.get("id") or 0),
        "first_name": first,
        "last_name": last,
        "email": (m.get("email") or "").strip(),
        "role": role_label,
        "is_team_director": bool(m.get("is_team_director") or 0),
        "status": "Invited",
    }


def _hydrate_session_fields(ts: Any, row: Dict[str, Any]) -> Dict[str, Any]:
    """Subset of a health_system row that's safe + useful for the wizard to resume from.

    Excludes credentials, password hashes, and any other secrets — only the form
    inputs the director already entered, plus the team list they've already added.

    The director is persisted in ``team_members`` with ``role='surgeon'`` and
    ``is_team_director=1`` after ``/finish``, so we filter on the new flag —
    Step 4's UI shows the Director in its own card, and Step 6's "TEAM members"
    stat counts them implicitly via ``members + 1``.
    """
    members = [
        _serialize_team_member(m)
        for m in ts.list_team_members(row["id"])
        if not bool(m.get("is_team_director") or 0)
    ]
    product = (row.get("product") or "archangel").strip().lower()
    director_email = (row.get("director_email") or "").strip()
    out = {
        "product": product,
        "director_first_name": (row.get("director_first_name") or "").strip(),
        "director_last_name": (row.get("director_last_name") or "").strip(),
        "director_email": director_email,
        "health_system_name": (row.get("name") or "").strip(),
        "surgery_department": (row.get("surgery_department") or "").strip(),
        "specialty": (row.get("specialty") or "").strip(),
        "phone": (row.get("phone") or "").strip(),
        "team_members": members,
    }
    if product == "asclepius":
        people = ts.list_asclepius_people(row["id"])
        out["asclepius_members"] = [
            {
                "id": p.get("id"),
                "full_name": p.get("full_name") or "",
                "email": p.get("email") or "",
                "clinical_role": p.get("clinical_role") or "",
                "role_label": _ASCLEPIUS_MEMBER_ROLES.get(
                    (p.get("clinical_role") or "").strip().lower(),
                    (p.get("clinical_role") or "").replace("_", " ").title(),
                ),
                "status": "Active" if p.get("onboarding_completed_at") else "Invited",
            }
            for p in people
            if not p.get("is_director")
        ]
        director = next((p for p in people if p.get("is_director")), None)
        if director:
            out["director_credentials"] = director.get("credentials") or {}
            out["director_attestations"] = director.get("attestations") or {}
    return out


@router.get("/session")
async def onboarding_session(token: str, request: Request):
    ts = _ts(request)
    row = ts.get_health_system_by_onboarding_token(token.strip())
    if not row:
        raise HTTPException(status_code=404, detail="Invalid or expired onboarding link.")
    if row.get("onboarding_completed_at"):
        slug = row.get("slug") or ""
        return {
            "status": "complete",
            "health_system_id": row["id"],
            "slug": slug,
            "sign_in_url": f"{_landing_base()}/t/{slug}/sign-in",
            **_hydrate_session_fields(ts, row),
        }
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    return {
        "status": "pending",
        "health_system_id": row["id"],
        "slug": row.get("slug"),
        "step": int(row.get("onboarding_step") or 0),
        **_hydrate_session_fields(ts, row),
    }


@router.post(
    "/self-serve",
    dependencies=[
        Depends(rate_limiter("onboarding_self_serve", 5, 600)),
        # Volumetric backstop: even with rotating source IPs, total link
        # creation is bounded (rows in health_systems + founder emails).
        Depends(global_rate_limiter("onboarding_self_serve_all", 60, 3600)),
    ],
)
async def self_serve_invite(body: SelfServeBody, request: Request):
    """Public: mint a physician-contributor onboarding link on demand.

    Issues the same magic link the admin "Generate Health System Link" button
    creates, so a physician clicking "Become a contributor" on the landing
    lands directly in the existing onboarding wizard. Abuse guards, layered:
    IP rate limit (5 / 10 min) → global cap (60/h) → honeypot → per-email cap
    (3 pending / 24h) → 7-day expiry (vs the admin default 30) → the wizard's
    own email-OTP step, which still gates every completion on proof of inbox
    control.
    """
    ts = _ts(request)
    email = str(body.email).lower().strip()

    # Honeypot: accept silently with a decoy link so a bot can't tell it was
    # caught. The token is random garbage — the wizard 404s it. Shape matches
    # the real success exactly (ok / onboarding_url / expires_at).
    if body.company_website.strip():
        decoy_expires = (
            (datetime.utcnow() + timedelta(days=_SELF_SERVE_EXPIRES_DAYS))
            .replace(microsecond=0)
            .isoformat()
        )
        return {
            "ok": True,
            "onboarding_url": f"{_landing_base()}/onboard/{secrets.token_urlsafe(32)}",
            "expires_at": decoy_expires,
        }

    if ts.count_recent_pending_invites_for_email(email, hours=24) >= _SELF_SERVE_EMAIL_CAP:
        raise HTTPException(
            status_code=429,
            detail=(
                "An onboarding link was already created for this email. "
                "Check your inbox, or try again tomorrow."
            ),
        )

    invite = ts.create_health_system_invite(
        invite_base_url=_landing_base(),
        expires_days=_SELF_SERVE_EXPIRES_DAYS,
        director_email=email,
    )

    # Best-effort provenance + founder visibility. Never fail the request on
    # either — the returned link is the deliverable.
    try:
        ts.record_lead_submission(
            "physician_onboard",
            email,
            f"Self-serve physician onboarding link issued ({invite['slug']}).",
            user_agent=request.headers.get("user-agent"),
            client_ip=client_ip(request),
        )
    except Exception:
        pass
    if _email_configured():
        safe_email = html.escape(email)
        safe_url = html.escape(invite["onboarding_url"])
        try:
            await send_html_email(
                email,
                "Your Archangel Health onboarding link",
                (
                    '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
                    'color:#1a1b1a;line-height:1.6">'
                    "<p>Here is your personal onboarding link — it stays valid for "
                    f"{_SELF_SERVE_EXPIRES_DAYS} days, and you can return to it any time "
                    "to resume where you left off:</p>"
                    f'<p><a href="{safe_url}">{safe_url}</a></p>'
                    "<p style='color:#8b8d89;font-size:13px'>If you didn't request this, "
                    "you can ignore this email.</p></div>"
                ),
            )
            await send_html_email(
                (os.getenv("LEAD_NOTIFY_EMAIL") or "tejpatel@berkeley.edu").strip(),
                f"[Onboarding] Physician contributor started — {email}",
                (
                    '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
                    'color:#1a1b1a;line-height:1.6">'
                    f"<p><strong>{safe_email}</strong> requested a physician-contributor "
                    "onboarding link from the landing page.</p>"
                    f"<p>Pending row: <code>{html.escape(invite['slug'])}</code> · "
                    f"expires {html.escape(invite['expires_at'])}</p></div>"
                ),
            )
        except Exception:
            pass

    return {
        "ok": True,
        "onboarding_url": invite["onboarding_url"],
        "expires_at": invite["expires_at"],
    }


@router.post("/step1-identity")
async def step1_identity(body: Step1Body, request: Request):
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    ts.update_health_system_director_identity(
        row["id"],
        first_name=body.first_name,
        last_name=body.last_name,
        email=str(body.email),
    )
    return {"ok": True, "step": 1}


@router.post("/request-otp", dependencies=[Depends(rate_limiter("onboarding_otp", 5, 60))])
async def request_otp(body: OnboardTokenBody, request: Request):
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    email = (row.get("director_email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Complete step 1 first.")
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    ts.create_otp_challenge(row["id"], email, code)
    subj = "Your Archangel Health verification code"
    html_body = build_verification_email(code=code)
    ok = await send_html_email(email, subj, html_body)
    if not ok:
        raise HTTPException(
            status_code=503,
            detail=(
                "Failed to send verification email. Check the backend log for [email_utils]. "
                "SendGrid 401 means this server's SENDGRID_API_KEY is wrong or not loaded (copy the same key as production into backend/.env). "
                "SendGrid 403 often means SENDGRID_FROM_EMAIL is not verified for that SendGrid account."
            ),
        )
    return {"ok": True}


@router.post("/verify-otp")
async def verify_otp(body: VerifyOtpBody, request: Request):
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    email = (row.get("director_email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Complete step 1 first.")
    if not ts.verify_otp_challenge(row["id"], email, body.code):
        raise HTTPException(status_code=400, detail="Invalid or expired code.")
    return {"ok": True, "step": 2}


@router.post("/step3-organization")
async def step3_organization(body: Step3Body, request: Request):
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    if int(row.get("onboarding_step") or 0) < 2:
        raise HTTPException(status_code=400, detail="Verify your email before continuing.")
    ts.update_health_system_org_details(
        row["id"],
        name=body.health_system_name,
        surgery_department=body.surgery_department,
        phone=body.phone,
    )
    new_slug = ts.maybe_update_slug_from_name(row["id"], body.health_system_name)
    return {"ok": True, "slug": new_slug, "step": 3}


@router.post("/add-team-member")
async def add_team_member(body: AddMemberBody, request: Request):
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    if int(row.get("onboarding_step") or 0) < 3:
        raise HTTPException(status_code=400, detail="Complete organization details first.")
    role = body.role.strip().lower()
    if role == "surgeon":
        raise HTTPException(
            status_code=409,
            detail="The team director is the only surgeon on the pod.",
        )
    if role not in _INVITABLE_ROLES:
        raise HTTPException(
            status_code=400,
            detail="Role must be rn_coordinator or np_pa.",
        )
    existing = ts.list_team_members(row["id"])
    non_director = [m for m in existing if not bool(m.get("is_team_director") or 0)]
    if role == "rn_coordinator" and any(
        (m.get("role") or "").strip().lower() == "rn_coordinator" for m in non_director
    ):
        raise HTTPException(
            status_code=409,
            detail="Team already has an RN care coordinator (cap: 1).",
        )
    if role == "np_pa" and sum(
        1 for m in non_director if (m.get("role") or "").strip().lower() == "np_pa"
    ) >= 2:
        raise HTTPException(
            status_code=409,
            detail="Team already has 2 NP/PAs (cap: 2).",
        )
    if len(non_director) >= 3:
        raise HTTPException(
            status_code=409,
            detail="Team is full (cap: 4 including director).",
        )
    pwd = generate_secure_password()
    full_name = body.full_name.strip()
    ts.insert_team_member(
        row["id"],
        email=str(body.email),
        name=full_name,
        role=role,
        password_hash=ts.hash_team_password(pwd),
    )
    row = ts.get_health_system_by_id(row["id"]) or row
    slug = row.get("slug") or ""
    sign_in = f"{_landing_base()}/t/{slug}/sign-in"
    director_full_name = " ".join(
        part for part in [
            (row.get("director_first_name") or "").strip(),
            (row.get("director_last_name") or "").strip(),
        ] if part
    ).strip()
    subj_org = (row.get("name") or "your health system").strip()
    subj_dept = (row.get("surgery_department") or "").strip()
    subj = (
        f"You're invited to {subj_org} {subj_dept} workspace"
        if subj_dept
        else f"You're invited to {subj_org} workspace"
    )
    html_body = build_invite_email(
        invitee_first_name=full_name.split(" ", 1)[0] if full_name else "",
        director_full_name=director_full_name,
        role_label=_ROLE_LABELS.get(role, role.replace("_", " ").title()),
        org_name=subj_org,
        department=subj_dept,
        temporary_password=pwd,
        sign_in_url=sign_in,
        invitee_email=str(body.email),
    )
    ok = await send_html_email(str(body.email), subj, html_body)
    if not ok:
        raise HTTPException(status_code=503, detail="Failed to send invitation email.")
    return {"ok": True}


@router.post("/finish")
async def finish_onboarding(body: FinishBody, request: Request):
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    if int(row.get("onboarding_step") or 0) < 3:
        raise HTTPException(status_code=400, detail="Complete all prior steps first.")
    row = ts.get_health_system_by_id(row["id"]) or row
    email = (row.get("director_email") or "").strip()
    fn = (row.get("director_first_name") or "").strip()
    ln = (row.get("director_last_name") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Missing director email.")
    director_pwd = generate_secure_password()
    ts.complete_onboarding_finalize(
        row["id"],
        director_email=email,
        director_first_name=fn,
        director_last_name=ln,
        director_password_hash=ts.hash_team_password(director_pwd),
    )
    row = ts.get_health_system_by_id(row["id"]) or row
    slug = row.get("slug") or ""
    sign_in = f"{_landing_base()}/t/{slug}/sign-in"
    subj = "Welcome to Archangel Health — onboarding complete"
    members_after_finalize = ts.list_team_members(row["id"])
    member_count = len(members_after_finalize)
    rn_count = sum(
        1
        for m in members_after_finalize
        if (m.get("role") or "").strip().lower() == "rn_coordinator"
    )
    nppa_count = sum(
        1
        for m in members_after_finalize
        if (m.get("role") or "").strip().lower() == "np_pa"
    )
    html_body = build_complete_email(
        director_email=email,
        org_name=(row.get("name") or "").strip(),
        department=(row.get("surgery_department") or "").strip(),
        member_count=member_count,
        rn_count=rn_count,
        nppa_count=nppa_count,
        temporary_password=director_pwd,
        workspace_url=sign_in,
    )
    await send_html_email(email, subj, html_body, importance_headers=True)
    return {"ok": True, "sign_in_url": sign_in}


# ═══════════════════════════════════════════════════════════════════════════
# Asclepius (data-training product) onboarding — Steps 3–8.
#
# Shares the magic-link / OTP / step machinery above (Steps 1–2); branches here
# once the director picks the Asclepius product. HIPAA/subprocessor gates do not
# apply to this plane — no PHI is collected.
# ═══════════════════════════════════════════════════════════════════════════


class SelectProductBody(OnboardTokenBody):
    product: str  # "archangel" | "asclepius"


class AsclepiusInstitutionBody(OnboardTokenBody):
    org_name: str
    specialty: str
    phone: str


class AsclepiusCredentialsBody(OnboardTokenBody):
    credentials: Dict[str, Any]


class AsclepiusAttestationsBody(OnboardTokenBody):
    attestations: Dict[str, Any]


class AsclepiusAddMemberBody(OnboardTokenBody):
    full_name: str
    email: EmailStr
    role: str  # physician | np | pa | resident_fellow


class MemberCredentialsBody(OnboardTokenBody):
    credentials: Dict[str, Any]


class MemberAttestationsBody(OnboardTokenBody):
    attestations: Dict[str, Any]


def _require_asclepius(row: Dict[str, Any]) -> None:
    if (row.get("product") or "archangel").strip().lower() != "asclepius":
        raise HTTPException(status_code=409, detail="This workspace is not an Asclepius workspace.")


def _provision_asclepius_user(
    request: Request,
    *,
    email: str,
    password: str,
    role: str,
    full_name: str,
    org_name: str,
    specialty: str,
    clinical_role: str,
    credentials: Dict[str, Any],
    attestations: Dict[str, Any],
) -> None:
    """Create/refresh the person's account in the Asclepius plane (asclepius.db)."""
    from asclepius import specialties as asc_specialties

    creds = credentials or {}
    # The verified legal name on the credential record is the authoritative name
    # attached to sold data; fall back to the identity name from onboarding.
    full_name = (creds.get("fullLegalName") or full_name or "").strip() or None
    # Asclepius tasks store canonical, lowercased specialties and the evaluator
    # queue matches case-sensitively. Normalize so a clinician who typed
    # "Nephrology" actually gets nephrology tasks; if the specialty isn't an
    # enabled registry specialty, leave it null so they fall into the "any open
    # task" queue rather than a permanently empty one (mirrors the SSO path).
    raw_specialty = (creds.get("primarySpecialty") or specialty or "").strip().lower()
    primary_specialty = raw_specialty if asc_specialties.is_enabled(raw_specialty) else None
    board_certs = creds.get("boardCertifications") or []
    board_cert = None
    if isinstance(board_certs, list) and board_certs:
        first = board_certs[0]
        if isinstance(first, dict):
            board_cert = " — ".join(
                p for p in [first.get("board"), first.get("specialty")] if p
            ) or None
        elif isinstance(first, str):
            board_cert = first
    years = creds.get("yearsInActivePractice")
    try:
        years = int(years) if years not in (None, "") else None
    except (TypeError, ValueError):
        years = None
    store = _asclepius_store(request)
    user = store.provision_user(
        email=email,
        password=password,
        role=role,
        full_name=full_name or None,
        org_name=org_name or None,
        clinical_role=clinical_role or None,
        specialty=primary_specialty,
        board_cert=board_cert,
        # B-5.1: store the NORMALIZED NPI. Every lookup uses the cleaned form
        # (get_cached_npi_fetch, find_users_by_npi), so a value posted as
        # "1234-567893" through the API matched no cache row and no duplicate
        # row — a dash defeated the duplicate-NPI blocker outright.
        npi=(_asc_credentialing().clean_npi(creds.get("npi") or "") or None),
        years_experience=years,
        credentials=creds,
        attestations=attestations or {},
    )
    # Advisor PRD §3.2 step 3: attach this signup to whichever advisor referred
    # them. Resolution is by the address the invite was addressed to — see
    # ``store.find_open_referral_for_email``. Best-effort: a referral that
    # cannot be claimed must never cost a physician their account.
    try:
        claimed = store.claim_referral_for_signup(email=email, user_id=user["id"])
        if claimed is not None:
            store.log_event(
                entity_type="user", entity_id=user["id"],
                event_type="referral_claimed", actor=email,
                payload={"referral_id": claimed["referral_id"],
                         "referrer_id": claimed["referrer_id"]},
            )
    except Exception:
        log.exception("[referral] could not attach signup to a referral (non-fatal)")

    # PRD-B: identity capture + credential verification. This function is
    # SYNCHRONOUS and both callers are async, so it must be reached through
    # ``run_in_threadpool`` — see the comment at each call site. Do not call it
    # directly from an async handler.
    _run_signup_verification(store, user, creds)


def _run_signup_verification(store: Any, user: Dict[str, Any], creds: Dict[str, Any]) -> None:
    """Capture PRD-B identity fields and run the NPI check for a fresh signup.

    Every failure path in here degrades to a 'pending' queue entry — never an
    exception out of the signup handler, never a rejection. Blocking the form
    on a third-party API is how launch day turns into a support queue.
    """
    from asclepius import credentialing

    uid = user["id"]
    try:
        store.update_identity_capture(
            uid,
            phone=(str(creds.get("phone") or "").strip() or None),
            linkedin_url=(str(creds.get("linkedinUrl") or creds.get("linkedin_url") or "").strip()
                          or None),
            email_domain_class=credentialing.classify_email_domain(user.get("email") or ""),
        )
    except Exception:
        log.exception("[credentialing] identity capture failed (non-fatal)")

    try:
        # The sha and its parse were both recorded server-side at upload time
        # (see asclepius_cv_upload). Nothing is parsed here: OCR on the signup
        # path is exactly the CPU-bound work B-1.1 is about, and the parse is
        # advisory anyway.
        cv_sha = str(creds.get("cvAssetSha") or "").strip()
        if cv_sha:
            parsed = creds.get("cvParsed")
            store.set_cv(uid, cv_sha, parsed if isinstance(parsed, dict) else None)
    except Exception:
        # A CV that cannot be attached is empty-fields + raw file for the admin.
        log.exception("[credentialing] CV attach failed (non-fatal)")

    npi = credentialing.clean_npi(str(creds.get("npi") or ""))
    if npi:
        family_name = credentialing.family_name_from_legal_name(
            str(creds.get("fullLegalName") or user.get("full_name") or ""))
        try:
            cached = store.get_cached_npi_fetch(npi)
            result = credentialing.verify_npi(npi, family_name, cached=cached)
            store.set_npi_result(uid, result)
            if result.get("result") == "verified":
                # Advisor PRD §3.2 step 4: the referrer's funnel follows the
                # invitee. 'verified' is the NPI coming back clean; 'approved'
                # is the admin's decision and is stamped from the verify router.
                try:
                    store.advance_referral_for_user(uid, "verified")
                except Exception:
                    log.exception("[referral] could not advance to verified (non-fatal)")
        except Exception:
            # "Could not check" is NOT "does not exist": route to manual review.
            log.exception("[credentialing] NPI check failed; queued for retry")
            try:
                store.set_npi_result(uid, {"result": "unavailable", "reason": "exception"})
            except Exception:
                log.exception("[credentialing] could not persist NPI result (non-fatal)")

    try:
        # Land in the admin verification queue — but never downgrade a decided
        # record: a re-onboard of an already approved/rejected physician keeps
        # the human decision until an admin changes it.
        current = store.get_user_by_id(uid) or {}
        if current.get("verification_status") in (None, "pending"):
            store.set_verification_status(uid, "pending")
            store.log_event(
                entity_type="user",
                entity_id=uid,
                event_type="verification_pending",
                actor=user.get("email"),
                payload={
                    "npi_result": ((current.get("npi_payload_json") and
                                    "checked") or ("submitted" if npi else "absent")),
                    "email_domain_class": current.get("email_domain_class"),
                },
            )
    except Exception:
        log.exception("[credentialing] could not mark signup pending (non-fatal)")


@router.post("/select-product")
async def select_product(body: SelectProductBody, request: Request):
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    if int(row.get("onboarding_step") or 0) < 2:
        raise HTTPException(status_code=400, detail="Verify your email before continuing.")
    product = (body.product or "").strip().lower()
    if product not in ("archangel", "asclepius"):
        raise HTTPException(status_code=400, detail="Choose Archangel or Asclepius.")
    ts.set_health_system_product(row["id"], product)
    return {"ok": True, "product": product}


@router.post("/asclepius/institution")
async def asclepius_institution(body: AsclepiusInstitutionBody, request: Request):
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    _require_asclepius(row)
    if int(row.get("onboarding_step") or 0) < 2:
        raise HTTPException(status_code=400, detail="Verify your email before continuing.")
    ts.update_asclepius_institution(
        row["id"],
        name=body.org_name,
        specialty=body.specialty,
        phone=body.phone,
    )
    new_slug = ts.maybe_update_slug_from_name(row["id"], body.org_name)
    # Seed the director as an Asclepius person so Steps 5–6 can save onto them.
    director_email = (row.get("director_email") or "").strip()
    director_name = " ".join(
        p for p in [
            (row.get("director_first_name") or "").strip(),
            (row.get("director_last_name") or "").strip(),
        ] if p
    ).strip()
    if director_email:
        ts.upsert_asclepius_person(
            row["id"],
            email=director_email,
            full_name=director_name,
            clinical_role="director",
            is_director=True,
        )
    return {"ok": True, "slug": new_slug, "step": 3}


@router.post("/asclepius/credentials")
async def asclepius_credentials(body: AsclepiusCredentialsBody, request: Request):
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    _require_asclepius(row)
    director_email = (row.get("director_email") or "").strip()
    if not director_email or not ts.get_asclepius_person(row["id"], director_email):
        raise HTTPException(status_code=400, detail="Complete your institution details first.")
    ts.save_asclepius_credentials(
        row["id"], director_email,
        _preserve_server_cv_fields(ts, row["id"], director_email, body.credentials))
    return {"ok": True}


@router.post("/asclepius/attestations")
async def asclepius_attestations(body: AsclepiusAttestationsBody, request: Request):
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    _require_asclepius(row)
    director_email = (row.get("director_email") or "").strip()
    if not director_email or not ts.get_asclepius_person(row["id"], director_email):
        raise HTTPException(status_code=400, detail="Complete your institution details first.")
    ts.save_asclepius_attestations(row["id"], director_email, body.attestations)
    return {"ok": True}


@router.post(
    "/asclepius/cv",
    dependencies=[Depends(rate_limiter("onboarding_cv", 10, 3600))],
)
async def asclepius_cv_upload(
    request: Request,
    background: BackgroundTasks,
    token: str = Form(...),
    file: UploadFile = File(...),
):
    """Optional CV upload during signup (PRD-B Phase 2/4).

    Accepts either the director onboarding token or an invited-member token,
    stores the raw document content-addressed, and records the sha **on the
    person's own row, server-side** (B-5.7). The sha is never round-tripped
    through the client: ``credentials`` is a free-form dict, so a client-set
    ``cvAssetSha`` would be an unvalidated reference into the shared asset
    store — which also holds de-identified clinical images.

    Parsing happens AFTER the response is sent (B-1.1): pdfminer/PyPDF2 and
    especially the OCR fallback are tens of CPU-seconds, and nothing about the
    CV needs to be parsed before the form returns.
    """
    from asclepius import credentialing

    ts = _ts(request)
    # Resolve which person this upload belongs to, from the token alone.
    hs_id = person_email = None
    try:
        row = _load_hs(request, token)
        row = ts.get_health_system_by_id(row["id"]) or row
        hs_id, person_email = row["id"], (row.get("director_email") or "").strip()
    except HTTPException:
        _ts_m, person, hs = _load_asclepius_member(request, token)  # 404s if invalid
        hs_id, person_email = hs["id"], person["email"]
    if not hs_id or not person_email:
        raise HTTPException(status_code=400, detail="Complete your institution details first.")

    data = await _read_capped(file, credentialing.CV_MAX_BYTES, request)
    try:
        meta = credentialing.store_cv(data, file.content_type or "")
    except credentialing.CvUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        # Asset-store trouble (disk, verification) must surface as a clean,
        # retryable error — the CV is optional and signup continues without it.
        log.exception("[credentialing] CV blob write failed")
        raise HTTPException(status_code=503,
                            detail="Could not store the CV right now — you can "
                                   "finish signup without it.")

    _record_cv_on_person(ts, hs_id, person_email, sha=meta["sha256"], mime=meta["mime"])
    # Sync function -> FastAPI runs it in a threadpool after the response.
    background.add_task(_parse_cv_into_person, ts, hs_id, person_email,
                        meta["sha256"], meta["mime"])
    return {"ok": True, "filename": file.filename, "byte_size": meta["byte_size"]}


async def _read_capped(file: UploadFile, max_bytes: int, request: Request) -> bytes:
    """Read an upload with a running cap (B-5.4).

    ``await file.read()`` buffers the entire body before any size check, so an
    arbitrarily large upload is resident in memory before it can be rejected —
    cheap memory pressure against a single-worker process. Reject on a
    declared Content-Length first, then stream and abort the moment the cap is
    passed rather than trusting the declaration.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + 8192:
        raise HTTPException(status_code=413,
                            detail=f"CV is too large; the limit is {max_bytes} bytes.")
    chunks, total = [], 0
    while True:
        chunk = await file.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413,
                                detail=f"CV is too large; the limit is {max_bytes} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def _record_cv_on_person(ts: Any, hs_id: str, email: str, *, sha: str, mime: str,
                         parsed: Optional[Dict[str, Any]] = None) -> None:
    """Merge CV facts into the person's stored credentials, server-side."""
    person = ts.get_asclepius_person(hs_id, email) or {}
    creds = dict(person.get("credentials") or {})
    creds["cvAssetSha"] = sha
    creds["cvMime"] = mime
    if parsed is not None:
        creds["cvParsed"] = parsed
    ts.save_asclepius_credentials(hs_id, email, creds)


def _parse_cv_into_person(ts: Any, hs_id: str, email: str, sha: str, mime: str) -> None:
    """Background CV parse. Best-effort by construction: a CV that cannot be
    parsed leaves the suggestions empty and the admin reads the raw file."""
    from asclepius import credentialing
    try:
        parsed = credentialing.parse_cv(sha, mime=mime)
        _record_cv_on_person(ts, hs_id, email, sha=sha, mime=mime, parsed=parsed)
    except Exception:
        log.exception("[credentialing] background CV parse failed (non-fatal)")


def _preserve_server_cv_fields(ts: Any, hs_id: str, email: str,
                               incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Strip client-supplied CV fields and restore the server-recorded ones.

    B-5.7: ``credentials`` is ``Dict[str, Any]``, so a signup could otherwise
    name any sha in the shared asset store and have it parsed and served back
    through the admin dossier. The sha is authoritative only when this server
    wrote it at upload time. This also stops the credentials POST — which the
    form sends after the upload — from erasing the recorded CV.
    """
    creds = {k: v for k, v in (incoming or {}).items()
             if k not in ("cvAssetSha", "cvMime", "cvParsed")}
    person = ts.get_asclepius_person(hs_id, email) or {}
    stored = person.get("credentials") or {}
    for key in ("cvAssetSha", "cvMime", "cvParsed"):
        if stored.get(key) is not None:
            creds[key] = stored[key]
    return creds


@router.post("/asclepius/add-member")
async def asclepius_add_member(body: AsclepiusAddMemberBody, request: Request):
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    _require_asclepius(row)
    if int(row.get("onboarding_step") or 0) < 3:
        raise HTTPException(status_code=400, detail="Complete your institution details first.")
    role = (body.role or "").strip().lower()
    if role not in _ASCLEPIUS_MEMBER_ROLES:
        raise HTTPException(status_code=400, detail="Pick a valid role for this team member.")
    member_email = str(body.email).lower().strip()
    director_email = (row.get("director_email") or "").strip().lower()
    if member_email == director_email:
        raise HTTPException(status_code=409, detail="You're already on the team as the director.")
    people = ts.list_asclepius_people(row["id"])
    invited = [p for p in people if not p.get("is_director")]
    already = next((p for p in invited if (p.get("email") or "").lower() == member_email), None)
    if not already and len(invited) >= _ASCLEPIUS_TEAM_CAP:
        raise HTTPException(
            status_code=409,
            detail=f"Team is full (cap: {_ASCLEPIUS_TEAM_CAP} invited clinicians).",
        )
    full_name = body.full_name.strip()
    ts.upsert_asclepius_person(
        row["id"],
        email=member_email,
        full_name=full_name,
        clinical_role=role,
        is_director=False,
    )
    member_token = ts.issue_asclepius_member_token(row["id"], member_email)
    onboarding_url = f"{_landing_base()}/onboard/m/{member_token}"
    director_name = " ".join(
        p for p in [
            (row.get("director_first_name") or "").strip(),
            (row.get("director_last_name") or "").strip(),
        ] if p
    ).strip()
    html_body = build_asclepius_invite_email(
        invitee_first_name=full_name.split(" ", 1)[0] if full_name else "",
        director_full_name=director_name,
        role_label=_ASCLEPIUS_MEMBER_ROLES[role],
        org_name=(row.get("name") or "").strip(),
        specialty=(row.get("specialty") or "").strip(),
        onboarding_url=onboarding_url,
        invitee_email=member_email,
    )
    subj = f"You're invited to label data with {(row.get('name') or 'your organization').strip()}"
    ok = await send_html_email(member_email, subj, html_body)
    if not ok:
        raise HTTPException(status_code=503, detail="Failed to send invitation email.")
    return {"ok": True}


@router.post(
    "/asclepius/finish",
    # Launch-day guard: signup completion is the expensive, account-creating
    # step. Keyed on the onboarding token, not the IP — see _signup_rate_guard
    # for why a per-IP bucket locked out whole hospitals.
    dependencies=[
        Depends(_signup_rate_guard),
        Depends(global_rate_limiter("asclepius_signup_all", *_SIGNUP_GLOBAL)),
    ],
)
async def asclepius_finish(body: OnboardTokenBody, request: Request):
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    _require_asclepius(row)
    director_email = (row.get("director_email") or "").strip()
    director = ts.get_asclepius_person(row["id"], director_email) if director_email else None
    if not director:
        raise HTTPException(status_code=400, detail="Complete your institution details first.")
    if not director.get("credentials"):
        raise HTTPException(status_code=400, detail="Add your credentials before finishing.")
    if not director.get("attestations"):
        raise HTTPException(status_code=400, detail="Sign the attestations before finishing.")

    director_pwd = generate_secure_password()
    org_name = (row.get("name") or "").strip()
    specialty = (row.get("specialty") or "").strip()
    # B-1.1: _provision_asclepius_user is synchronous and reaches a synchronous
    # httpx call to NPPES (plus pbkdf2 hashing and sqlite writes). Calling it
    # directly from this async handler runs all of that ON THE EVENT LOOP, so
    # one hung NPPES response stalls every request in the process — not just
    # this one. The try/except inside prevents a 500; it does not prevent a
    # hang. The threadpool hop is what makes the "non-blocking" claim true.
    await run_in_threadpool(
        _provision_asclepius_user,
        request,
        email=director_email,
        password=director_pwd,
        role="admin",
        full_name=director.get("full_name") or "",
        org_name=org_name,
        specialty=specialty,
        clinical_role="director",
        credentials=director.get("credentials") or {},
        attestations=director.get("attestations") or {},
    )
    ts.finalize_asclepius_person(
        row["id"], director_email, password_hash=ts.hash_team_password(director_pwd)
    )
    ts.complete_asclepius_onboarding(row["id"])

    invited = [p for p in ts.list_asclepius_people(row["id"]) if not p.get("is_director")]
    workspace_url = _asclepius_workspace_url()
    html_body = build_asclepius_complete_email(
        email=director_email,
        full_name=director.get("full_name") or "",
        role_label=_ASCLEPIUS_DIRECTOR_ROLE_LABEL,
        org_name=org_name,
        specialty=specialty,
        temporary_password=director_pwd,
        workspace_url=workspace_url,
        is_director=True,
        team_count=len(invited),
        verification_notice=True,
    )
    await send_html_email(
        director_email, "Your Asclepius workspace is ready", html_body, importance_headers=True
    )
    return {"ok": True, "workspace_url": workspace_url}


# ─── Invited-member flow (link → credentials → attestations → workspace) ──────


def _load_asclepius_member(request: Request, token: str):
    ts = _ts(request)
    person = ts.get_asclepius_person_by_member_token((token or "").strip())
    if not person:
        raise HTTPException(status_code=404, detail="Invalid or expired onboarding link.")
    if person.get("onboarding_completed_at"):
        raise HTTPException(status_code=410, detail="You've already completed onboarding.")
    if not ts.asclepius_member_token_valid(person):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    hs = ts.get_health_system_by_id(person["health_system_id"])
    if not hs:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return ts, person, hs


@router.get("/member/session")
async def member_session(token: str, request: Request):
    ts, person, hs = _load_asclepius_member(request, token)
    full = (person.get("full_name") or "").strip()
    first, _, last = full.partition(" ")
    role = (person.get("clinical_role") or "").strip().lower()
    return {
        "status": "pending",
        "mode": "asclepius_member",
        "email": person.get("email") or "",
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "clinical_role": role,
        "role_label": _ASCLEPIUS_MEMBER_ROLES.get(role, role.replace("_", " ").title()),
        "org_name": (hs.get("name") or "").strip(),
        "specialty": (hs.get("specialty") or "").strip(),
        "credentials": person.get("credentials") or {},
        "attestations": person.get("attestations") or {},
    }


@router.post("/member/credentials")
async def member_credentials(body: MemberCredentialsBody, request: Request):
    ts, person, hs = _load_asclepius_member(request, body.token)
    ts.save_asclepius_credentials(
        hs["id"], person["email"],
        _preserve_server_cv_fields(ts, hs["id"], person["email"], body.credentials))
    return {"ok": True}


@router.post("/member/attestations")
async def member_attestations(body: MemberAttestationsBody, request: Request):
    ts, person, hs = _load_asclepius_member(request, body.token)
    ts.save_asclepius_attestations(hs["id"], person["email"], body.attestations)
    return {"ok": True}


@router.post(
    "/member/finish",
    dependencies=[
        Depends(_signup_rate_guard),
        Depends(global_rate_limiter("asclepius_signup_all", *_SIGNUP_GLOBAL)),
    ],
)
async def member_finish(body: OnboardTokenBody, request: Request):
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    ts, person, hs = _load_asclepius_member(request, body.token)
    # Re-read the saved record (credentials/attestations live on the person row).
    person = ts.get_asclepius_person(hs["id"], person["email"]) or person
    if not person.get("credentials"):
        raise HTTPException(status_code=400, detail="Add your credentials before finishing.")
    if not person.get("attestations"):
        raise HTTPException(status_code=400, detail="Sign the attestations before finishing.")
    member_pwd = generate_secure_password()
    org_name = (hs.get("name") or "").strip()
    specialty = (hs.get("specialty") or "").strip()
    role = (person.get("clinical_role") or "").strip().lower()
    # B-1.1: see the director path — synchronous NPPES/pbkdf2/sqlite work must
    # not run on the event loop.
    await run_in_threadpool(
        _provision_asclepius_user,
        request,
        email=person["email"],
        password=member_pwd,
        role="evaluator",
        full_name=person.get("full_name") or "",
        org_name=org_name,
        specialty=specialty,
        clinical_role=role,
        credentials=person.get("credentials") or {},
        attestations=person.get("attestations") or {},
    )
    ts.finalize_asclepius_person(
        hs["id"], person["email"], password_hash=ts.hash_team_password(member_pwd)
    )
    workspace_url = _asclepius_workspace_url()
    html_body = build_asclepius_complete_email(
        email=person["email"],
        full_name=person.get("full_name") or "",
        role_label=_ASCLEPIUS_MEMBER_ROLES.get(role, role.replace("_", " ").title()),
        org_name=org_name,
        specialty=specialty,
        temporary_password=member_pwd,
        workspace_url=workspace_url,
        is_director=False,
        verification_notice=True,
    )
    await send_html_email(
        person["email"], "Your Asclepius workspace is ready", html_body, importance_headers=True
    )
    return {"ok": True, "workspace_url": workspace_url}
