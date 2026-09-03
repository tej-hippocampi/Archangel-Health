"""Health system onboarding (magic link, email OTP, team invites)."""

import hashlib
import html
import logging
import os
import string
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import secrets
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi import Form
from pydantic import BaseModel, EmailStr, Field
from starlette.concurrency import run_in_threadpool

from ratelimit import client_ip, global_rate_limiter, rate_limiter

from email_utils import is_email_dev_mode, is_email_transport_configured, send_html_email
from asclepius import passwords as asc_passwords
from asclepius import store as asc_store_mod
from onboarding_emails import (
    build_application_start_email,
    build_application_submitted_email,
    build_asclepius_complete_email,
    build_asclepius_invite_email,
    build_complete_email,
    build_internal_signup_alert,
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


def _is_production() -> bool:
    return (os.getenv("ENV") or "").strip().lower() == "production"


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


def _partner_intro_url(request: Request, email: str) -> str:
    """The /partner link the workspace-ready email hands a new physician.

    Attributed to them when their referral code can be minted, plain when it
    cannot. A physician who forwards this to a health system they already know
    is the cheapest introduction we get, and an unattributed forward is one
    where they never find out it worked.

    Best effort throughout. This runs inside the last request of a signup that
    has already provisioned an account, and losing a query parameter is not
    worth failing that request over.
    """
    from asclepius import referrals as asc_referrals

    code = None
    try:
        store = _asclepius_store(request)
        user = store.get_user_by_email(email)
        if user and asc_referrals.can_refer(user):
            code = store.ensure_referral_code(user["id"])
    except Exception:
        code = None
    return asc_referrals.partner_url(code, None)


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
    # /join extras, all optional so the landing's email-only modal keeps its
    # exact contract. Names prefill wizard step 1; the referral code attributes
    # a link signup. ``flavor`` says which door they came through:
    #   general   an invited non-clinical signer
    #   advisor   a non-clinical supporter who will mostly refer
    #   referrer  someone who holds a referral link and nothing else
    # All three relax the MD credential screens, which exist to check a
    # physician and have nothing to ask a person who is not claiming to be one.
    first_name: str = Field(default="", max_length=120)
    last_name: str = Field(default="", max_length=120)
    referral_code: str = Field(default="", max_length=32)
    flavor: str = Field(default="", max_length=20)


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
        "signup_flavor": (row.get("signup_flavor") or "").strip() or None,
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
            creds = director.get("credentials") or {}
            out["director_credentials"] = creds
            out["director_attestations"] = director.get("attestations") or {}
            # Onboarding v2 §3: the Review screen prefills from the parse, so a
            # physician who resumes from the emailed link must find their CV
            # suggestions still there rather than an empty form and a CV they
            # already uploaded. Server-owned keys only — see _SERVER_CV_KEYS.
            out["director_cv"] = {
                "uploaded": bool(creds.get("cvAssetSha")),
                "filename": creds.get("cvFilename"),
                "stage": creds.get("cvParseStage"),
                "parsed": creds.get("cvParsed"),
            }
    return out


@router.get("/credential-config")
async def credential_config():
    """What to ask a doctor for, country by country.

    Public and tokenless: it is a form schema, identical for everyone, and the
    wizard needs it before a token is necessarily in hand. Fetched once at
    mount rather than per keystroke — the whole table is a few kilobytes and a
    round trip per country change would make the form feel broken on a plane.
    """
    from asclepius.registry import config as registry_config

    return {
        "countries": list(registry_config.supported_countries()),
        # Everywhere else falls back to document review, so a doctor from a
        # country we have not configured still has a way through.
        "default": {
            "id_label": registry_config.DEFAULT_REGISTRY.id_label,
            "id_hint": registry_config.DEFAULT_REGISTRY.id_hint,
            "method": registry_config.DEFAULT_REGISTRY.method,
        },
        "qualifications": [
            "MD", "DO", "MBBS", "MBChB", "MBBCh", "BMBS",
            "Staatsexamen", "Other",
        ],
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
    # Already have an account? Say so, and let the wizard offer a sign-in
    # instead of walking them through a signup for an account that exists.
    #
    # Two ways to land here: a colleague forwards a /join link to someone who
    # signed up months ago, or a physician requests a fresh link having
    # forgotten they already have one. Both used to walk the entire wizard and
    # end at ``finish``, which passes ``password_hash=`` unconditionally and so
    # silently REPOINTED the live account's password to whatever they typed on
    # the way through.
    #
    # Not an account-existence oracle: the caller already holds a signed
    # onboarding token minted for this exact address, so they cannot ask about
    # anybody else's.
    director_email = (row.get("director_email") or "").strip()
    if director_email:
        try:
            existing = _asclepius_store(request).get_user_by_email(director_email)
        except Exception:
            existing = None
        # Onboarding v2 §2: a v2 application creates the account row at SUBMIT,
        # with no password (NO_PASSWORD_HASH). That is not "you already have an
        # account, go and sign in" — there is nothing to sign in with. It is
        # "your application is in review", which is a different screen and a
        # different sentence.
        if existing and asc_store_mod.password_is_unset(existing):
            return {
                "status": "application_pending",
                "health_system_id": row["id"],
                "slug": row.get("slug"),
                "step": int(row.get("onboarding_step") or 0),
                "verification_status": existing.get("verification_status"),
                **_hydrate_session_fields(ts, row),
            }
        if existing and (existing.get("password_hash") or "").strip():
            return {
                "status": "account_exists",
                "health_system_id": row["id"],
                "slug": row.get("slug"),
                "step": int(row.get("onboarding_step") or 0),
                **_hydrate_session_fields(ts, row),
            }
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
    #
    # The response stays silent to the CALLER, but it is no longer silent to us.
    # A false positive here is indistinguishable, from the physician's side, from
    # a broken product: they get a 200, a link, and then "Invalid or expired
    # onboarding link" with no way forward. That is exactly what happened when
    # the field was named `company_website` with a "Company website" label —
    # Chrome and Safari autofill address-profile fields on those signals and
    # ignore autocomplete="off" for them, so real doctors with a saved profile
    # were being classified as bots. Nothing was written and nothing was logged,
    # which is why it took a manual walkthrough to find. Log it.
    if body.company_website.strip():
        # Local import: `asc_referrals` is imported further down in this same
        # function for the referral path, which makes the name function-local
        # for the whole body. A module-level import would be shadowed and this
        # line would raise UnboundLocalError before it ever logged anything.
        from asclepius import referrals as asc_referrals  # noqa: PLC0415

        log.warning(
            "[self-serve] honeypot tripped for %s from %s — decoy link returned, "
            "no invite created. If this fires for real signups, the honeypot "
            "field is being autofilled; check its name/label/id.",
            asc_referrals.mask_email(email), client_ip(request),
        )
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
        product="asclepius",
    )

    # /join extras, all best-effort: the link is the deliverable and none of
    # these may fail the mint. Names land on the row through the same setter
    # step 1 uses, so the wizard hydrates them prefilled; the flavor rides the
    # row into the session payload; the referral code records attribution
    # (email-keyed claiming still binds it at provisioning, like every invite).
    first = (body.first_name or "").strip()
    last = (body.last_name or "").strip()
    if first or last:
        try:
            ts.update_health_system_director_identity(
                invite["health_system_id"],
                first_name=first, last_name=last, email=email)
        except Exception:
            pass
    if (body.flavor or "").strip():
        try:
            ts.set_health_system_signup_flavor(invite["health_system_id"], body.flavor)
        except Exception:
            pass
    if (body.referral_code or "").strip():
        try:
            from asclepius import referrals as asc_referrals  # noqa: PLC0415
            from asclepius.store import get_store as _asc_store  # noqa: PLC0415
            asc_referrals.attach_link_signup(
                _asc_store(), referral_code=body.referral_code,
                email=email, ip=client_ip(request))
        except Exception:
            pass

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
        try:
            # §4.1. In v2 this is the RESUME path, not the entry path: the
            # landing page redirects straight into the wizard with the fresh
            # token, and this arrives so the physician can stop and come back.
            # The card's promise — "we will email you the same link so you can
            # pause and resume any time" — is now literally true end to end.
            await send_html_email(
                email,
                "Your Archangel Health application — pick up any time",
                build_application_start_email(
                    first_name=(body.first_name or "").strip(),
                    onboarding_url=invite["onboarding_url"],
                    expires_days=_SELF_SERVE_EXPIRES_DAYS,
                ),
            )
            await send_html_email(
                (os.getenv("LEAD_NOTIFY_EMAIL") or "tejpatel@berkeley.edu").strip(),
                f"[Onboarding] Physician contributor started: {email}",
                build_internal_signup_alert(
                    physician_email=email,
                    slug=invite["slug"],
                    expires_at=invite["expires_at"],
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
    previous_email = (row.get("director_email") or "").strip()
    ts.update_health_system_director_identity(
        row["id"],
        first_name=body.first_name,
        last_name=body.last_name,
        email=str(body.email),
    )
    # Referral attribution is keyed on the address the invite was addressed to,
    # and this screen is where that address can change: someone opens a
    # colleague's link with a personal address and corrects it to their hospital
    # one, which is an entirely reasonable thing to do and used to cost the
    # referrer the credit silently. Best-effort by contract, like every other
    # attribution write: a signup must never fail because we could not work out
    # who to thank.
    new_email = str(body.email or "").strip()
    if previous_email and new_email.lower() != previous_email.lower():
        try:
            from asclepius.store import get_store as _asc_store  # noqa: PLC0415
            _asc_store().move_open_referrals(previous_email, new_email)
        except Exception:
            log.exception("[referral] could not follow an email change (non-fatal)")
    return {"ok": True, "step": 1}


@router.post("/request-otp", dependencies=[Depends(rate_limiter("onboarding_otp", 5, 60))])
async def request_otp(body: OnboardTokenBody, request: Request):
    dev_bypass = not _email_configured() and not _is_production()
    if not _email_configured() and not dev_bypass:
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
    if dev_bypass:
        log.warning("DEV ONBOARDING OTP (no email transport configured) for %s: %s", email, code)
        return {"ok": True}
    # EMAIL_DEV_MODE is NOT dev_bypass. `_email_configured()` returns True in dev
    # mode (that is the point — onboarding must not 503 without SendGrid), so the
    # branch above never runs locally and the code leaves only inside the printed
    # email body. That reads as though the OTP is logged when it is not, and it
    # makes local onboarding untestable without scraping stdout for the right
    # block. Log it here too, on the same marker, so one grep finds it either way.
    # Guarded on dev mode AND non-production: an OTP in a production log is a
    # credential in a log.
    if is_email_dev_mode() and not _is_production():
        log.warning("DEV ONBOARDING OTP (EMAIL_DEV_MODE) for %s: %s", email, code)
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


class AsclepiusPasswordBody(OnboardTokenBody):
    password: str


class AsclepiusAddMemberBody(OnboardTokenBody):
    full_name: str
    email: EmailStr
    role: str  # physician | np | pa | resident_fellow


class MemberCredentialsBody(OnboardTokenBody):
    credentials: Dict[str, Any]


class MemberAttestationsBody(OnboardTokenBody):
    attestations: Dict[str, Any]


def _ensure_director_person(ts: Any, row: Dict[str, Any]) -> Optional[str]:
    """Make sure the director's ``asclepius_people`` row exists. Returns their email.

    Every screen that saves onto that row (the CV upload, the credentials POST)
    used to depend on the INSTITUTION screen having run first, because that is
    where the row was seeded. Onboarding v2 §2 drops the institution screen from
    the physician flow, which would leave the CV upload and the Review screen
    both 400-ing with "Complete your institution details first" — an instruction
    pointing at a screen that no longer exists.

    So the seed moves to where it is actually needed. Idempotent (the same upsert
    the institution screen calls), and it writes nothing but identity: the org
    name and specialty still arrive from the form.
    """
    email = (row.get("director_email") or "").strip()
    if not email:
        return None
    if ts.get_asclepius_person(row["id"], email):
        return email
    name = " ".join(p for p in [
        (row.get("director_first_name") or "").strip(),
        (row.get("director_last_name") or "").strip(),
    ] if p).strip()
    ts.upsert_asclepius_person(
        row["id"], email=email, full_name=name,
        clinical_role="director", is_director=True,
    )
    return email


def _require_asclepius(row: Dict[str, Any]) -> None:
    if (row.get("product") or "archangel").strip().lower() != "asclepius":
        raise HTTPException(status_code=409, detail="This workspace is not an Asclepius workspace.")


#: Grace window before a signup alert sends. The agent normally reports in well
#: under this (NPPES is its only network call) and rewrites the row with its
#: findings. If it is broken or the process died, the plain version still goes
#: out, so a broken agent costs detail rather than the notification itself.
_ADMIN_ALERT_GRACE_SECONDS = int(os.getenv("ASCLEPIUS_ADMIN_ALERT_GRACE_SECONDS", "120") or 120)


def _admin_alert_recipients(store: Any) -> List[str]:
    """Explicit list, then the bootstrap admin, then every active admin account.

    The chain matters: a notification feature that silently no-ops because one
    env var is unset is worse than not having one.
    """
    raw = (os.getenv("ASCLEPIUS_ADMIN_NOTIFY_EMAILS") or "").strip()
    if raw:
        return [e.strip() for e in raw.split(",") if e.strip()]
    single = (os.getenv("ASCLEPIUS_ADMIN_EMAIL") or "").strip()
    if single:
        return [single]
    try:
        return [
            u["email"] for u in (store.list_users() or [])
            if u.get("role") == "admin" and u.get("active") and u.get("email")
        ]
    except Exception:
        return []


def _queue_verification_and_alert(store: Any, user_id: str) -> None:
    """Queue the agent run, and queue the admin alert it will later enrich."""
    from datetime import timedelta as _td  # noqa: PLC0415
    from onboarding_emails import build_asclepius_admin_signup_alert  # noqa: PLC0415

    store.enqueue_verification_job(user_id)
    user = store.get_user_by_id(user_id) or {}
    name = (user.get("full_name") or user.get("email") or "A physician").strip()
    send_after = (
        datetime.utcnow() + _td(seconds=_ADMIN_ALERT_GRACE_SECONDS)
    ).replace(microsecond=0).isoformat()
    body = build_asclepius_admin_signup_alert(
        physician_name=name,
        email=user.get("email") or "",
        specialty=user.get("specialty") or "",
        decision="New signup",
        recommendation="The verification agent has not reported yet.",
        reasons=[],
    )
    recipients = _admin_alert_recipients(store)
    for addr in recipients:
        store.enqueue_admin_notification(
            # One recipient keeps the bare key, because that is the key the
            # agent rewrites when it reports. With several, each gets its own.
            idempotency_key=(f"signup|{user_id}" if len(recipients) == 1
                             else f"signup|{user_id}|{addr}"),
            kind="signup",
            subject=f"[Asclepius] New signup: {name}",
            body_html=body,
            recipient_email=addr,
            send_after=send_after,
        )


ACCOUNT_KIND_BY_FLAVOR = {
    "advisor": "advisor",
    "referrer": "referrer",
}

#: Link flavors that skip the physician credential screens.
NON_CLINICAL_FLAVORS = ("general", "advisor", "referrer")


def _provision_asclepius_user(
    request: Request,
    *,
    email: str,
    password: Optional[str] = None,
    password_hash: Optional[str] = None,
    role: str,
    full_name: str,
    org_name: str,
    specialty: str,
    clinical_role: str,
    credentials: Dict[str, Any],
    attestations: Dict[str, Any],
    account_kind: Optional[str] = None,
    verify: bool = True,
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
    # Optional free-text niche / case-type description from onboarding. Stored
    # verbatim (unlike primary_specialty, which is normalized to the registry);
    # descriptive metadata only, never a scoring input.
    specialty_niche = (creds.get("specialtyNiche") or "").strip() or None
    years = creds.get("yearsInActivePractice")
    try:
        years = int(years) if years not in (None, "") else None
    except (TypeError, ValueError):
        years = None
    store = _asclepius_store(request)
    user = store.provision_user(
        email=email,
        password=password,
        password_hash=password_hash,
        role=role,
        full_name=full_name or None,
        org_name=org_name or None,
        clinical_role=clinical_role or None,
        specialty=primary_specialty,
        specialty_niche=specialty_niche,
        board_cert=board_cert,
        # B-5.1: store the NORMALIZED NPI. Every lookup uses the cleaned form
        # (get_cached_npi_fetch, find_users_by_npi), so a value posted as
        # "1234-567893" through the API matched no cache row and no duplicate
        # row — a dash defeated the duplicate-NPI blocker outright.
        npi=(_asc_credentialing().clean_npi(creds.get("npi") or "") or None),
        years_experience=years,
        credentials=creds,
        attestations=attestations or {},
        account_kind=account_kind,
    )
    # Attach this signup to whichever physician referred
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
    #
    # ``verify=False`` is for accounts that are not claiming to be physicians.
    # It leaves verification_status NULL, which reads as access level FULL --
    # and that is deliberate: what limits these accounts is the account_kind cap
    # in ``capabilities.surfaces()``, which holds however their verification
    # lands, NOT a pending state that an admin could clear by accident.
    if verify:
        _run_signup_verification(store, user, creds)


async def _welcome_into_community(email: str) -> None:
    """Introduce a new physician in #introductions.

    Fired at signup, not only at approval. A provisional physician is already
    inside the community -- they can read and post from the moment they finish
    the form -- so waiting for the credential check meant the room said nothing
    while they were in it, and then introduced them days later to people they
    had already been talking to. Idempotent: the welcome flag is claimed before
    the post, so the approval path will not repeat it.
    """
    try:
        from asclepius.store import get_store as _get_astore  # noqa: PLC0415
        from community.onboard import welcome_new_member  # noqa: PLC0415

        user = _get_astore().get_user_by_email(email)
        if user:
            await welcome_new_member(user)
    except Exception:
        log.exception("[community] welcome post failed (non-fatal)")


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

    # Where this doctor is licensed decides which registry answers for them.
    # A blank country is a US signup: that is who was signing up before the
    # form could ask.
    from asclepius.registry import config as registry_config

    licensure = registry_config.normalize_country(
        creds.get("countryOfLicensure") or creds.get("countryOfPractice")) or "US"
    practice = registry_config.normalize_country(
        creds.get("countryOfPractice")) or licensure
    registration = str(creds.get("registrationNumber") or "").strip()
    npi = credentialing.clean_npi(str(creds.get("npi") or ""))

    try:
        store.set_registry_country(
            uid, practice=practice, licensure=licensure,
            registry_id=(npi if licensure == "US" else registration) or None,
        )
    except Exception:
        log.exception("[credentialing] could not record countries (non-fatal)")

    family_name = credentialing.family_name_from_legal_name(
        str(creds.get("fullLegalName") or user.get("full_name") or ""))

    if licensure == "US" and npi:
        try:
            cached = store.get_cached_npi_fetch(npi)
            result = credentialing.verify_npi(npi, family_name, cached=cached)
            store.set_npi_result(uid, result)
            if result.get("result") == "verified":
                # The referrer's funnel follows the
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
    elif licensure != "US":
        # Deliberately NOT checked inline. NPPES answers in under a second;
        # a foreign register may not answer at all, and the signup form is the
        # last place to find that out. Record what we know, let the
        # verification agent do the lookup on the job we are about to queue.
        cfg = registry_config.for_country(licensure)
        queued = {
            "result": "document_only" if cfg.method == registry_config.METHOD_DOCUMENT
            else "queued",
            "registry": cfg.registry_name,
            "identifier": registration,
            "reason": None,
            "record": None,
        }
        try:
            store.set_registry_result(uid, queued)
        except Exception:
            log.exception("[credentialing] could not record registry state (non-fatal)")

    # Does the signup hold together? Findings route to the queue as review
    # flags; none of them rejects anybody.
    try:
        from asclepius import plausibility

        refreshed = store.get_user_by_id(uid) or user
        store.set_signup_flags(uid, plausibility.flags(refreshed, creds))
    except Exception:
        log.exception("[credentialing] plausibility check failed (non-fatal)")

    try:
        # Land in the admin verification queue — but never downgrade a decided
        # record: a re-onboard of an already approved/rejected physician keeps
        # the human decision until an admin changes it.
        current = store.get_user_by_id(uid) or {}
        if current.get("verification_status") in (None, "pending"):
            store.set_verification_status(uid, "pending")
            # Every signup gets an agent run and an admin alert, queued at the
            # one place that decides someone is pending, so "every signup is
            # looked at" is a fact rather than a hope.
            try:
                _queue_verification_and_alert(store, uid)
            except Exception:
                log.exception("[onboarding] could not queue verification for %s", uid)
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
    # v2 §2: the Review screen is the first thing that writes here, and the
    # institution screen it used to depend on is gone from this path.
    director_email = _ensure_director_person(ts, row)
    if not director_email:
        raise HTTPException(status_code=400, detail="Start your application first.")
    ts.save_asclepius_credentials(
        row["id"], director_email,
        _preserve_server_cv_fields(ts, row["id"], director_email, body.credentials))
    # The physician's specialty lives on the health_systems row too — the tier
    # scorer and the task router both read it from there — and v2 has no
    # institution screen to put it there. Mirror it from the one field the Review
    # screen requires, so the account that gets provisioned is routable.
    creds_in = body.credentials or {}
    specialty = str(creds_in.get("primarySpecialty") or "").strip()
    if specialty and not (row.get("specialty") or "").strip():
        # A physician signing up for themselves has no institution to name, so
        # the workspace is named after them — same fallback the institution
        # screen used when its org field was left blank.
        org_name = ((row.get("name") or "").strip()
                    or str(creds_in.get("fullLegalName") or "").strip()
                    or " ".join(p for p in [
                        (row.get("director_first_name") or "").strip(),
                        (row.get("director_last_name") or "").strip()] if p).strip()
                    or "My workspace")
        try:
            ts.update_asclepius_institution(
                row["id"], name=org_name, specialty=specialty,
                phone=str(creds_in.get("phone") or row.get("phone") or "").strip(),
            )
            ts.maybe_update_slug_from_name(row["id"], org_name)
        except Exception:
            log.exception("[onboarding] could not mirror specialty onto the invite row")
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
    director_email = _ensure_director_person(ts, row)
    if not director_email:
        raise HTTPException(status_code=400, detail="Start your application first.")
    ts.save_asclepius_attestations(row["id"], director_email, body.attestations)
    return {"ok": True}


@router.post(
    "/asclepius/password",
    dependencies=[Depends(rate_limiter("onboarding_password", 10, 60))],
)
async def asclepius_set_password(body: AsclepiusPasswordBody, request: Request):
    """The physician chooses their own password, right after the OTP.

    Persisted here rather than carried in React state because the wizard
    resumes from SERVER state (onboarding_step), so a refresh would otherwise
    drop the password and land the user past the step that collects it.
    """
    ts = _ts(request)
    row = _load_hs(request, body.token)
    _reject_if_completed(row)
    if not ts.onboarding_token_valid(row):
        raise HTTPException(status_code=404, detail="This onboarding link has expired.")
    row = ts.get_health_system_by_id(row["id"]) or row
    _require_asclepius(row)
    # The mailbox must be proven first. Setting a password before the OTP would
    # let a typo'd address end up with an account whose credential was chosen by
    # someone who cannot receive its mail.
    if int(row.get("onboarding_step") or 0) < 2:
        raise HTTPException(status_code=403, detail="Verify your email first.")
    director_email = (row.get("director_email") or "").strip()
    if not director_email:
        raise HTTPException(status_code=400, detail="Start your onboarding first.")
    # Seed the director's person row if the institution step has not run yet.
    # This step deliberately sits BEFORE institution (the password is chosen as
    # soon as the mailbox is proven), so it cannot rely on institution having
    # created the row — doing so made the self-serve door unfinishable: the
    # password 400'd here, and /asclepius/finish then refused with "Choose a
    # password before finishing" with no way back. Same upsert institution does.
    if not ts.get_asclepius_person(row["id"], director_email):
        director_name = " ".join(
            part for part in [
                (row.get("director_first_name") or "").strip(),
                (row.get("director_last_name") or "").strip(),
            ] if part
        ).strip()
        ts.upsert_asclepius_person(
            row["id"],
            email=director_email,
            full_name=director_name,
            clinical_role="director",
            is_director=True,
        )
    try:
        asc_passwords.validate(body.password, email=director_email)
    except asc_passwords.PasswordRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ts.set_asclepius_person_password_hash(
        row["id"], director_email, ts.hash_team_password(body.password)
    )
    return {"ok": True}


@router.post(
    "/member/password",
    dependencies=[Depends(rate_limiter("onboarding_password", 10, 60))],
)
async def member_set_password(body: AsclepiusPasswordBody, request: Request):
    ts = _ts(request)
    person = ts.get_asclepius_person_by_member_token((body.token or "").strip())
    if not person:
        raise HTTPException(status_code=404, detail="This invite link has expired.")
    if not person.get("email_verified_at"):
        raise HTTPException(status_code=403, detail="Verify your email first.")
    email = (person.get("email") or "").strip()
    try:
        asc_passwords.validate(body.password, email=email)
    except asc_passwords.PasswordRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ts.set_asclepius_person_password_hash(
        person["health_system_id"], email, ts.hash_team_password(body.password)
    )
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
        # v2 §2: the CV screen comes BEFORE anything that used to seed this row.
        hs_id, person_email = row["id"], _ensure_director_person(ts, row)
    except HTTPException:
        _ts_m, person, hs = _load_asclepius_member(request, token)  # 404s if invalid
        hs_id, person_email = hs["id"], person["email"]
    if not hs_id or not person_email:
        raise HTTPException(status_code=400, detail="Start your application first.")

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

    # 'reading' is stamped HERE, before the response, so the wizard's first poll
    # can never land in the window between "upload returned" and "background task
    # started" and read a stage of None as "nothing is happening".
    _record_cv_on_person(ts, hs_id, person_email, sha=meta["sha256"], mime=meta["mime"],
                         stage="reading", filename=(file.filename or None))
    # Sync function -> FastAPI runs it in a threadpool after the response.
    background.add_task(_parse_cv_into_person, ts, hs_id, person_email,
                        meta["sha256"], meta["mime"])
    return {"ok": True, "filename": file.filename, "byte_size": meta["byte_size"],
            "stage": "reading"}


@router.get("/asclepius/cv/status")
async def asclepius_cv_status(token: str, request: Request):
    """Where the CV parse has got to, and what it found (§2 screen 3).

    Polled by the wizard between the upload and the Review screen. Returns the
    parse SUGGESTIONS, not a decision: the Review screen prefills from them and
    the physician edits anything that is wrong, so nothing here is authoritative
    and nothing here can fail the application.
    """
    ts = _ts(request)
    hs_id = person_email = None
    try:
        row = _load_hs(request, token)
        row = ts.get_health_system_by_id(row["id"]) or row
        hs_id, person_email = row["id"], (row.get("director_email") or "").strip()
    except HTTPException:
        _ts_m, person, hs = _load_asclepius_member(request, token)  # 404s if invalid
        hs_id, person_email = hs["id"], person["email"]
    person = ts.get_asclepius_person(hs_id, person_email) if hs_id and person_email else None
    creds = (person or {}).get("credentials") or {}
    if not creds.get("cvAssetSha"):
        return {"uploaded": False, "stage": None, "parsed": None}
    stage = creds.get("cvParseStage") or "reading"
    parsed = creds.get("cvParsed")
    return {
        "uploaded": True,
        "filename": creds.get("cvFilename"),
        "stage": stage,
        # 'done' and 'failed' are the two terminal stages; the client stops
        # polling on either. Handing back a boolean instead would make the
        # caller re-derive the terminal set, which is how a poll loops forever.
        "finished": stage in ("done", "failed"),
        "ok": bool((parsed or {}).get("ok")),
        "parsed": parsed if stage in ("done", "failed") else None,
    }


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
                         parsed: Optional[Dict[str, Any]] = None,
                         stage: Optional[str] = None,
                         filename: Optional[str] = None) -> None:
    """Merge CV facts into the person's stored credentials, server-side."""
    person = ts.get_asclepius_person(hs_id, email) or {}
    creds = dict(person.get("credentials") or {})
    creds["cvAssetSha"] = sha
    creds["cvMime"] = mime
    if filename is not None:
        creds["cvFilename"] = filename
    if stage is not None:
        creds["cvParseStage"] = stage
    if parsed is not None:
        creds["cvParsed"] = parsed
    ts.save_asclepius_credentials(hs_id, email, creds)


def _parse_cv_into_person(ts: Any, hs_id: str, email: str, sha: str, mime: str) -> None:
    """Background CV parse. Best-effort by construction: a CV that cannot be
    parsed leaves the suggestions empty and the admin reads the raw file.

    Each stage is written as it BEGINS, so ``GET /asclepius/cv/status`` reports
    where the work actually is and the wizard's three captions track real
    progress rather than a timer (§2 screen 3, §7).
    """
    from asclepius import credentialing

    def _stage(name: str) -> None:
        _record_cv_on_person(ts, hs_id, email, sha=sha, mime=mime, stage=name)

    try:
        parsed = credentialing.parse_cv(sha, mime=mime, on_stage=_stage)
        _record_cv_on_person(ts, hs_id, email, sha=sha, mime=mime, parsed=parsed,
                             stage="done" if parsed.get("ok") else "failed")
    except Exception:
        log.exception("[credentialing] background CV parse failed (non-fatal)")
        # The screen must never hang on a spinner. A parse that died still
        # resolves the poll, and the Review screen it feeds shows empty states
        # rather than an error (§2: nothing on that page is an error).
        try:
            _record_cv_on_person(ts, hs_id, email, sha=sha, mime=mime, stage="failed")
        except Exception:
            log.exception("[credentialing] could not record the CV parse failure")


#: CV keys the SERVER owns. A client may not set any of them — ``credentials``
#: is a free-form dict, so a client-set sha would be an unvalidated reference
#: into the shared asset store, and a client-set parse or stage would let a
#: signup dictate what the admin dossier says about its own CV.
_SERVER_CV_KEYS = ("cvAssetSha", "cvMime", "cvParsed", "cvParseStage", "cvFilename")


def _preserve_server_cv_fields(ts: Any, hs_id: str, email: str,
                               incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Strip client-supplied CV fields and restore the server-recorded ones.

    B-5.7: ``credentials`` is ``Dict[str, Any]``, so a signup could otherwise
    name any sha in the shared asset store and have it parsed and served back
    through the admin dossier. The sha is authoritative only when this server
    wrote it at upload time. This also stops the credentials POST — which the
    form sends after the upload — from erasing the recorded CV.
    """
    creds = {k: v for k, v in (incoming or {}).items() if k not in _SERVER_CV_KEYS}
    person = ts.get_asclepius_person(hs_id, email) or {}
    stored = person.get("credentials") or {}
    for key in _SERVER_CV_KEYS:
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
        # v2 §2 deleted the institution screen from the physician flow, so the old
        # copy here pointed at a screen that no longer exists. Every screen that
        # writes onto this row seeds it (``_ensure_director_person``), which means
        # reaching finish without one is "you have not filled anything in yet".
        raise HTTPException(status_code=400, detail="Start your application first.")
    # Which door this link came from. An advisor and a referral partner walk a
    # four-screen signup that never shows the credential or attestation screens,
    # so demanding them here is demanding something the wizard never offered.
    # The set is exactly the flavors that produce a CAPPED account
    # (``capabilities._BY_ACCOUNT_KIND``): asking less is only safe because the
    # resulting account can do less.
    account_kind = ACCOUNT_KIND_BY_FLAVOR.get(
        (row.get("signup_flavor") or "").strip().lower())
    is_clinical = account_kind is None
    if is_clinical:
        # v2 §2: name, email and specialty are the WHOLE requirement.
        #
        # A non-empty credentials blob still has to be here, because it is the
        # proof that the physician reached and submitted the Review screen at
        # all — but nothing INSIDE it is mandatory any more. A missing NPI, CV or
        # board certification becomes a low-severity review flag
        # (``plausibility._missing_evidence_flags``), never a wall in front of a
        # doctor filling this in between patients. The Submit button on that
        # screen is always live, and this is what makes that true rather than a
        # promise the server breaks.
        creds_blob = director.get("credentials") or {}
        if not creds_blob:
            raise HTTPException(status_code=400, detail="Add your credentials before finishing.")
        if not director.get("attestations"):
            raise HTTPException(status_code=400, detail="Sign the attestations before finishing.")
        missing = []
        if not (creds_blob.get("fullLegalName") or director.get("full_name") or "").strip():
            missing.append("your name")
        if not (creds_blob.get("primarySpecialty") or row.get("specialty") or "").strip():
            missing.append("your specialty")
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"We still need {' and '.join(missing)} before we can send this in.")

    # Onboarding v2 §2: the physician wizard has NO password step. The account is
    # created here, `pending`, with no credential of any kind — credentials only
    # exist after a human approves the application (§5), and they arrive by email.
    #
    # The member and short-signup doors still choose their own password at
    # /asclepius/password, and a hash on the row is therefore honoured: this is
    # "we were not given one", never "we are erasing the one you have"
    # (``provision_user`` enforces the same rule a second time).
    director_hash = (director.get("password_hash") or "").strip() \
        or asc_store_mod.NO_PASSWORD_HASH
    credentials_deferred = director_hash == asc_store_mod.NO_PASSWORD_HASH
    org_name = (row.get("name") or "").strip()
    specialty = (row.get("specialty") or "").strip()
    # B-1.1: _provision_asclepius_user is synchronous and reaches a synchronous
    # httpx call to NPPES (plus pbkdf2 hashing and sqlite writes). Calling it
    # directly from this async handler runs all of that ON THE EVENT LOOP, so
    # one hung NPPES response stalls every request in the process — not just
    # this one. The try/except inside prevents a 500; it does not prevent a
    # hang. The threadpool hop is what makes the "non-blocking" claim true.
    # The director is a PHYSICIAN CONTRIBUTOR, not an operator of our back office.
    #
    # This provisioned ``role="admin"``, and ``/self-serve`` above is a public
    # endpoint — so anyone who signed up got an Asclepius admin. The consequences
    # chained in one hop: admins are exempt from the credential-verification gate
    # (auth.get_current_user), ``require_admin`` is a bare role check with no
    # tenant scoping across ~120 endpoints, and ``capabilities.can()``
    # short-circuits entirely on role == "admin". A self-signed-up account could
    # open the verification queue, read every physician's dossier, and approve
    # itself.
    #
    # Nothing in the product needed it. ``clinical_role="director"`` is stored and
    # carries the directorship; team members are added through the token-scoped
    # onboarding wizard (``/asclepius/add-member``), not the admin console. So the
    # director keeps everything they actually use and goes through credential
    # verification like every other clinician — which is the claim the whole
    # product is sold on.
    await run_in_threadpool(
        _provision_asclepius_user,
        request,
        email=director_email,
        password_hash=director_hash,
        role="evaluator",
        full_name=director.get("full_name") or "",
        org_name=org_name,
        specialty=specialty,
        clinical_role="director",
        credentials=director.get("credentials") or {},
        attestations=director.get("attestations") or {},
        # Which door they came through decides what kind of account this is.
        account_kind=account_kind,
        # A non-clinical account is not a doctor awaiting a credential check.
        # Running the physician verification path on one would put a row in the
        # admin queue that no admin can act on -- there is no NPI to look up and
        # no registration to match -- and would make the queue lie about how much
        # real work is waiting. They stay visible to admins under Physicians ->
        # Signups, which surfaces signup_flavor, so nobody becomes invisible.
        verify=is_clinical,
    )
    # The hash is already on the row; finalize only stamps completion.
    ts.finalize_asclepius_person(row["id"], director_email, password_hash=director_hash)
    ts.complete_asclepius_onboarding(row["id"])
    # #introductions is physicians introducing themselves to colleagues. An
    # advisor reading along is not a new colleague to announce, and a referral
    # partner never reaches the community at all.
    if is_clinical:
        await _welcome_into_community(director_email)

    # Mint an Asclepius session token so the wizard drops the doctor straight into
    # their workspace with no re-login (mirrors the doctor-portal auto-auth).
    #
    # This used to call ``authenticate(store, director_email, director_pwd)``, and
    # ``director_pwd`` does not exist in this function: the plaintext password was
    # last seen at POST /asclepius/password, several requests ago, and is stored
    # only as a hash. The NameError was swallowed by the bare except below, so
    # ``token`` came back None for EVERY signup and every new doctor landed on the
    # success screen with no session. Look up the row we just provisioned instead
    # -- the onboarding token and the mailbox OTP already proved who this is, so
    # re-checking a password we do not have proves nothing extra.
    #
    # An applicant now gets a session too, which REVERSES v2 §2.
    #
    # That rule was right for the product it was written against: there was
    # genuinely nothing behind the door, the queue opened on approval, and a
    # token would have dropped a physician into a portal that 403s every call.
    # Handing someone a key to an empty room is worse than asking them to wait.
    #
    # The room is no longer empty. The practice case is now a real piece of
    # work that an applicant does BEFORE we decide about them: it teaches what
    # the job actually is, and how they do it feeds the decision. That is the
    # thing the wait is for, and it cannot happen behind a door they cannot
    # open. The PROVISIONAL surface set is scoped to exactly it plus a
    # dashboard that says where they stand, so the portal they land in is one
    # we meant to show them rather than a wall of denials.
    #
    # This does not create a durable credential. There is still no password on
    # the account: approval remains the moment one comes into existence, so the
    # security reasoning at approval time is untouched. Coming BACK before a
    # decision goes through a single-use emailed sign-in link, which is the
    # weakest door that works.
    session_token = None
    try:
        from asclepius import auth as asc_auth
        asc_user = _asclepius_store(request).get_user_by_email(director_email)
        if asc_user:
            session_token = asc_auth.create_token(asc_user)
    except Exception:
        session_token = None

    invited = [p for p in ts.list_asclepius_people(row["id"]) if not p.get("is_director")]
    workspace_url = _asclepius_workspace_url()
    if credentials_deferred:
        # §4.3. Deliberately NOT the "your workspace is ready" email: nothing is
        # ready, and telling a physician it is would be the first thing we got
        # wrong. Credentials arrive on approval (§4.4).
        await send_html_email(
            director_email, "We've got your application",
            build_application_submitted_email(full_name=director.get("full_name") or ""),
            importance_headers=True,
        )
    else:
        html_body = build_asclepius_complete_email(
            email=director_email,
            full_name=director.get("full_name") or "",
            role_label=_ASCLEPIUS_DIRECTOR_ROLE_LABEL,
            org_name=org_name,
            specialty=specialty,
            workspace_url=workspace_url,
            is_director=True,
            team_count=len(invited),
            verification_notice=True,
            partner_url=_partner_intro_url(request, director_email),
        )
        await send_html_email(
            director_email, "Your Asclepius workspace is ready", html_body,
            importance_headers=True
        )
    return {"ok": True, "workspace_url": workspace_url, "token": session_token,
            # The wizard's success screen branches on this: an application that
            # is awaiting review says so, rather than offering a workspace link
            # that leads to a locked door.
            "awaiting_review": credentials_deferred}


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
    "/member/request-otp",
    dependencies=[Depends(rate_limiter("onboarding_otp", 5, 60))],
)
async def member_request_otp(body: OnboardTokenBody, request: Request):
    """Hard-gate email verification for invited clinicians (Feature B): mails a
    6-digit OTP to the same inbox that received the invite, via the existing
    ``otp_challenges`` machinery the director already uses."""
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    ts, person, hs = _load_asclepius_member(request, body.token)
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    ts.create_otp_challenge(hs["id"], person["email"], code)
    html_body = build_verification_email(code=code)
    ok = await send_html_email(person["email"], "Your Archangel Health verification code", html_body)
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


@router.post("/member/verify-otp")
async def member_verify_otp(body: VerifyOtpBody, request: Request):
    ts, person, hs = _load_asclepius_member(request, body.token)
    if not ts.verify_otp_challenge(hs["id"], person["email"], body.code):
        raise HTTPException(status_code=400, detail="Invalid or expired code.")
    ts.mark_asclepius_member_verified(hs["id"], person["email"])
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
    if not person.get("email_verified_at"):
        raise HTTPException(status_code=403, detail="Please verify your email before finishing onboarding.")
    member_hash = (person.get("password_hash") or "").strip()
    if not member_hash:
        raise HTTPException(status_code=400, detail="Choose a password before finishing.")
    org_name = (hs.get("name") or "").strip()
    specialty = (hs.get("specialty") or "").strip()
    role = (person.get("clinical_role") or "").strip().lower()
    # B-1.1: see the director path — synchronous NPPES/pbkdf2/sqlite work must
    # not run on the event loop.
    await run_in_threadpool(
        _provision_asclepius_user,
        request,
        email=person["email"],
        password_hash=member_hash,
        role="evaluator",
        full_name=person.get("full_name") or "",
        org_name=org_name,
        specialty=specialty,
        clinical_role=role,
        credentials=person.get("credentials") or {},
        attestations=person.get("attestations") or {},
    )
    ts.finalize_asclepius_person(hs["id"], person["email"], password_hash=member_hash)
    workspace_url = _asclepius_workspace_url()
    html_body = build_asclepius_complete_email(
        email=person["email"],
        full_name=person.get("full_name") or "",
        role_label=_ASCLEPIUS_MEMBER_ROLES.get(role, role.replace("_", " ").title()),
        org_name=org_name,
        specialty=specialty,
        workspace_url=workspace_url,
        is_director=False,
        verification_notice=True,
        partner_url=_partner_intro_url(request, person["email"]),
    )
    await send_html_email(
        person["email"], "Your Asclepius workspace is ready", html_body, importance_headers=True
    )
    # Mint a session, exactly as the director path does. This route returned no
    # token at all, so an INVITED clinician could never land signed in: they
    # finished the form, were told their workspace was ready, and got a login
    # screen. Same reasoning as the director's: the invite token and the OTP
    # already proved who this is, so there is nothing further to check.
    session_token = None
    try:
        from asclepius import auth as asc_auth
        asc_user = _asclepius_store(request).get_user_by_email(person["email"])
        if asc_user:
            session_token = asc_auth.create_token(asc_user)
    except Exception:
        session_token = None
    return {"ok": True, "workspace_url": workspace_url, "token": session_token}
