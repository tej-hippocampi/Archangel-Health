"""Health system onboarding (magic link, email OTP, team invites)."""

import os
import string
from typing import Any, Dict

import secrets
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from email_utils import is_email_transport_configured, send_html_email
from onboarding_emails import (
    build_complete_email,
    build_invite_email,
    build_verification_email,
)
from tenant_utils import generate_secure_password

# Mapping of API role values → display labels used in the new email templates.
# The frontend uses the labels directly; the API persists the lowercased token.
_ROLE_LABELS = {
    "doctor": "Doctor / Surgeon",
    "nurse": "Nurse / Care Coordinator",
}

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def _ts(request: Request):
    return request.app.state.team_store


def _landing_base() -> str:
    return (os.getenv("LANDING_URL") or "http://localhost:5173").strip().rstrip("/")


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
    role: str  # doctor | nurse

class FinishBody(OnboardTokenBody):
    pass


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

    Maps the API role token (`doctor`/`nurse`) to the display label the
    redesigned wizard uses (`Doctor / Surgeon` / `Nurse / Care Coordinator`).
    """
    full = (m.get("name") or "").strip()
    first, _, last = full.partition(" ")
    role = (m.get("role") or "").strip().lower()
    role_label = _ROLE_LABELS.get(role, "Doctor / Surgeon")
    return {
        "id": int(m.get("id") or 0),
        "first_name": first,
        "last_name": last,
        "email": (m.get("email") or "").strip(),
        "role": role_label,
        "status": "Invited",
    }


def _hydrate_session_fields(ts: Any, row: Dict[str, Any]) -> Dict[str, Any]:
    """Subset of a health_system row that's safe + useful for the wizard to resume from.

    Excludes credentials, password hashes, and any other secrets — only the form
    inputs the director already entered, plus the team list they've already added.

    The director is also persisted in ``team_members`` with ``role='director'``
    after ``/finish``, so we filter it out of the hydrated list — Step 4's UI
    shows the Director in its own card, and Step 6's "TEAM members" stat
    counts them implicitly via ``members + 1``.
    """
    members = [
        _serialize_team_member(m)
        for m in ts.list_team_members(row["id"])
        if (m.get("role") or "").strip().lower() != "director"
    ]
    return {
        "director_first_name": (row.get("director_first_name") or "").strip(),
        "director_last_name": (row.get("director_last_name") or "").strip(),
        "director_email": (row.get("director_email") or "").strip(),
        "health_system_name": (row.get("name") or "").strip(),
        "surgery_department": (row.get("surgery_department") or "").strip(),
        "phone": (row.get("phone") or "").strip(),
        "team_members": members,
    }


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


@router.post("/request-otp")
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
    if role not in ("doctor", "nurse"):
        raise HTTPException(status_code=400, detail="Role must be doctor or nurse.")
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
        role_label=_ROLE_LABELS.get(role, role.title()),
        org_name=subj_org,
        department=subj_dept,
        temporary_password=pwd,
        sign_in_url=sign_in,
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
    member_count = len(ts.list_team_members(row["id"]))
    html_body = build_complete_email(
        director_email=email,
        org_name=(row.get("name") or "").strip(),
        department=(row.get("surgery_department") or "").strip(),
        member_count=member_count,
        temporary_password=director_pwd,
        workspace_url=sign_in,
    )
    await send_html_email(email, subj, html_body, importance_headers=True)
    return {"ok": True, "sign_in_url": sign_in}
