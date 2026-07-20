"""Health system onboarding (magic link, email OTP, team invites)."""

import html
import os
import string
from typing import Any, Dict

import secrets
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from ratelimit import client_ip, rate_limiter

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


@router.post("/self-serve", dependencies=[Depends(rate_limiter("onboarding_self_serve", 5, 600))])
async def self_serve_invite(body: SelfServeBody, request: Request):
    """Public: mint a physician-contributor onboarding link on demand.

    Issues the same magic link the admin "Generate Health System Link" button
    creates, so a physician clicking "Become a contributor" on the landing
    lands directly in the existing onboarding wizard. Abuse guards, layered:
    IP rate limit (5 / 10 min) → honeypot → per-email cap (3 pending / 24h)
    → 7-day expiry (vs the admin default 30) → the wizard's own email-OTP
    step, which still gates every completion on proof of inbox control.
    """
    ts = _ts(request)
    email = str(body.email).lower().strip()

    # Honeypot: accept silently with a decoy link so a bot can't tell it was
    # caught. The token is random garbage — the wizard 404s it.
    if body.company_website.strip():
        return {
            "ok": True,
            "onboarding_url": f"{_landing_base()}/onboard/{secrets.token_urlsafe(32)}",
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
    _asclepius_store(request).provision_user(
        email=email,
        password=password,
        role=role,
        full_name=full_name or None,
        org_name=org_name or None,
        clinical_role=clinical_role or None,
        specialty=primary_specialty,
        board_cert=board_cert,
        npi=(creds.get("npi") or None),
        years_experience=years,
        credentials=creds,
        attestations=attestations or {},
    )


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
    ts.save_asclepius_credentials(row["id"], director_email, body.credentials)
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


@router.post("/asclepius/finish")
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
    _provision_asclepius_user(
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
    ts.save_asclepius_credentials(hs["id"], person["email"], body.credentials)
    return {"ok": True}


@router.post("/member/attestations")
async def member_attestations(body: MemberAttestationsBody, request: Request):
    ts, person, hs = _load_asclepius_member(request, body.token)
    ts.save_asclepius_attestations(hs["id"], person["email"], body.attestations)
    return {"ok": True}


@router.post("/member/finish")
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
    _provision_asclepius_user(
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
    )
    await send_html_email(
        person["email"], "Your Asclepius workspace is ready", html_body, importance_headers=True
    )
    return {"ok": True, "workspace_url": workspace_url}
