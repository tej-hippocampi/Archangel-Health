"""Health system onboarding (magic link, email OTP, team invites)."""

import html as html_lib
import os
import string
from typing import Any, Dict, Optional

import secrets
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from email_utils import is_email_transport_configured, send_html_email
from tenant_utils import generate_secure_password

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
        }
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    return {
        "status": "pending",
        "health_system_id": row["id"],
        "slug": row.get("slug"),
        "step": int(row.get("onboarding_step") or 0),
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
    html_body = f"""
    <div style="font-family:system-ui,Segoe UI,sans-serif;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin:0 0 12px;">Verification code</h2>
      <p style="color:#334155;">Enter this code to continue onboarding:</p>
      <p style="font-size:32px;font-weight:700;letter-spacing:0.2em;">{html_lib.escape(code)}</p>
      <p style="color:#64748b;font-size:13px;">This code expires in 15 minutes. If you did not request it, ignore this email.</p>
    </div>
    """
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
    ts.insert_team_member(
        row["id"],
        email=str(body.email),
        name=body.full_name.strip(),
        role=role,
        password_hash=ts.hash_team_password(pwd),
    )
    row = ts.get_health_system_by_id(row["id"]) or row
    slug = row.get("slug") or ""
    sign_in = f"{_landing_base()}/t/{slug}/sign-in"
    subj = f"You're invited to {row.get('name') or 'your health system'} on Archangel Health"
    html_body = f"""
    <div style="font-family:system-ui,Segoe UI,sans-serif;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin:0 0 12px;">Hello {html_lib.escape(body.full_name.strip())},</h2>
      <p>You have been added as a <strong>{html_lib.escape(role.title())}</strong> for your health system's Archangel Health workspace.</p>
      <p><strong>Your temporary password:</strong> <code style="font-size:15px;">{html_lib.escape(pwd)}</code></p>
      <p style="margin:24px 0;">
        <a href="{html_lib.escape(sign_in, quote=True)}" style="display:inline-block;padding:12px 20px;background:#0f766e;color:#fff;text-decoration:none;border-radius:8px;font-weight:600;">Sign in to your workspace</a>
      </p>
      <p style="color:#64748b;font-size:14px;">Please sign in and change your password on first login.</p>
    </div>
    """
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
    html_body = f"""
    <div style="font-family:system-ui,Segoe UI,sans-serif;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin:0 0 12px;">Thank you for completing onboarding</h2>
      <p>Your health system <strong>{html_lib.escape(row.get('name') or '')}</strong> is ready.</p>
      <p><strong>Your email:</strong> {html_lib.escape(email)}<br/>
         <strong>Role:</strong> Director of TEAM Initiative<br/>
         <strong>Temporary password:</strong> <code>{html_lib.escape(director_pwd)}</code></p>
      <p style="margin:24px 0;">
        <a href="{html_lib.escape(sign_in, quote=True)}" style="display:inline-block;padding:12px 20px;background:#0f766e;color:#fff;text-decoration:none;border-radius:8px;font-weight:600;">Open your workspace</a>
      </p>
      <p style="color:#64748b;font-size:14px;">Your team members have been sent their own credentials. Please change your password after first sign-in.</p>
    </div>
    """
    await send_html_email(email, subj, html_body, importance_headers=True)
    return {"ok": True, "sign_in_url": sign_in}
