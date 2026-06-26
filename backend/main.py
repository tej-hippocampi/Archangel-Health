"""
CareGuide — Surgical Patient Video Platform
FastAPI backend: EHR → Pipeline → Dashboard → SMS
"""

import asyncio
import os
import threading
import time
import re
from pathlib import Path
import json
import tempfile
import random
import sqlite3
import html as html_lib
import secrets
import string
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks, APIRouter, Depends, Request, Header, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from dotenv import dotenv_values

# Load env from predictable paths (uvicorn cwd is often repo root, not backend/).
_backend_dir = Path(__file__).resolve().parent
_repo_root = _backend_dir.parent


def _load_layered_dotenv(*paths: Path) -> None:
    """Load .env files in order; later files win, but blank values must not
    wipe secrets already set (backend/.env often ships empty placeholders)."""
    for path in paths:
        if not path.is_file():
            continue
        for key, val in dotenv_values(path).items():
            if val is None or val == "":
                continue
            os.environ[key] = val


_load_layered_dotenv(_repo_root / ".env", _backend_dir / ".env")

from pipeline.ingest   import IngestLayer
from pipeline.extract  import ExtractionLayer
from pipeline.classify import ClassificationLayer
from pipeline.generate import GenerationLayer
from pipeline.grounding_gate import apply_grounding_to_patient
from pipeline.streaming import StreamingPipelineContext, run_postop_stream, run_preop_stream
from pipeline.gated_synthesis import synthesize_script
from integrations.elevenlabs   import ElevenLabsClient
from integrations.tavus        import TavusClient
from integrations.twilio_client import TwilioClient
from routers.internal import _check_auth as _check_internal_auth, router as internal_router
from routers.admin    import router as admin_router
from routers.asclepius import router as asclepius_router
from routers.onboarding import router as onboarding_router
from routers.tenant_portal import router as tenant_portal_router
from routers.eligibility import router as eligibility_router
from routers.intraop import router as intraop_router
from routers.postop import router as postop_router
from routers.initial_tier import router as initial_tier_router
from routers.preop_retier import router as preop_retier_router
from routers.teachback import router as teachback_router
from routers.triage_explain import router as triage_explain_router
from routers.messaging import router as messaging_router
from routers.telehealth import router as telehealth_router
from eligibility import store as elig_store
import demo_credentials
import field_crypto
from staff_context import (
    StaffContext,
    assert_staff_patient_scope,
    get_staff_context_optional,
    require_clinical_auth,
)
from tenant_constants import (
    DEMO_HEALTH_SYSTEM_ID,
    DEMO_HEALTH_SYSTEM_SLUG,
    TRIAGEDM_CLINIC_CODE,
)
from triage_demo_seed import (
    effective_seed_strategy,
    ensure_triage_demo_staff,
    merge_triage_patients_into_store,
    seed_triage_demo_sqlite,
    spinal_fusion_postop_demo_resources,
    triage_demo_patient_ids,
)
from tenant_jwt import decode_tenant_staff_token
from patient_session import (
    PatientSessionMiddleware,
    clear_patient_session_cookie,
    consume_entry_token,
    create_entry_token,
    current_patient_session,
    revoke_patient_session,
    set_patient_session_cookie,
)
from http_security import (
    SecurityHeadersMiddleware,
    allowed_hosts,
    allowed_origin_regex,
    allowed_origins,
    assert_production_secrets,
    is_production,
)
from ratelimit import rate_limiter
from email_utils import (
    email_phi_allowed,
    is_email_transport_configured,
    send_html_email as _send_html_email_impl,
    send_html_email_with_reason as _send_html_email_with_reason_impl,
)
from auth import (
    UserCreate,
    UserLogin,
    UserOut,
    DoctorOnboard,
    DoctorProfileOut,
    register_user,
    authenticate_user,
    get_current_user_optional,
    get_current_user,
    create_access_token,
    get_doctor_profile,
    set_doctor_profile,
)
import auth as auth_module
from team_store import TeamStore
from preop_survey import (
    WINDOW_SURVEY_DAY,
    compute_window_tier,
    hours_until_surgery,
    parse_surgery_datetime,
    preop_escalation_trigger,
    questions_for_window,
    score_preop_survey,
    survey_window_state,
)
from intake_form_parser import (
    INTAKE_SECTION_BY_INDEX,
    apply_health_system_facility_name,
    merge_intake_ai_patch,
    parseTranscriptToFormData,
    reset_intake_section_for_interview_redo,
)
from intake_section_chat import accumulate_red_flags_from_section_messages, run_intake_section_turn
from ai.llm_client import call_llm_sync, first_text
from prompts.system import SEMANTIC_ESCALATION_PROMPT

# ─── App Setup ────────────────────────────────────────────────
app = FastAPI(
    title="Archangel Health Surgical Patient Platform",
    description="Personalized surgical education videos generated from EHR data.",
    version="0.2.0",
)

# Audit middleware added FIRST so it is the innermost layer: it runs with the
# patient-session ContextVar set and records ePHI access after the route (PRD-5).
from audit.middleware import AuditMiddleware  # noqa: E402

app.add_middleware(AuditMiddleware)

# CORS restricted to an explicit origin allowlist (PRD-2). Wildcard origins with
# credentials are invalid + unsafe; the landing app's origin must be allowlisted
# in production via ALLOWED_ORIGINS. The product's own https domains
# (archangelhealth.ai + subdomains) are additionally allowed via regex so a
# missing/stale ALLOWED_ORIGINS env var cannot break landing sign-in.
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_origin_regex=allowed_origin_regex(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Patient-Session", "X-Admin-Token"],
    max_age=600,
)

# Resolve the pt_session cookie into a per-request PatientSession (PRD-1).
app.add_middleware(PatientSessionMiddleware)

# Security headers (HSTS in prod, CSP report-only by default) on every response.
app.add_middleware(SecurityHeadersMiddleware)

# Production-only Host allowlist (no-op unless ALLOWED_HOSTS is set).
if is_production():
    _hosts = allowed_hosts()
    if _hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=_hosts)

# App-level HTTP->HTTPS redirect is OPT-IN and intentionally decoupled from ENV.
# It requires uvicorn to run with --proxy-headers, otherwise it loops forever
# behind a TLS-terminating proxy (Railway/Render) and breaks healthchecks. HSTS
# (set in production) plus the platform edge already enforce HTTPS, so this stays
# off unless explicitly enabled by an operator who has verified proxy headers.
if os.getenv("FORCE_HTTPS_REDIRECT", "0").strip().lower() in ("1", "true", "yes", "on"):
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

    app.add_middleware(HTTPSRedirectMiddleware)

_patient_store: dict = {}
app.state.patient_store = _patient_store
_team_store = TeamStore()
app.state.team_store = _team_store


import logging as _logging

_auth_logger = _logging.getLogger("patient_auth")

# Feature flag for staged rollout. When "0", unauthenticated patient access falls
# back to legacy (open) behavior with a WARNING. Default ON (enforce). Remove the
# flag once the rollout is verified (PRD-1 §12).
def _enforce_patient_auth() -> bool:
    return os.getenv("ENFORCE_PATIENT_AUTH", "1").strip().lower() not in ("0", "false", "no", "off")


def _patient_principal_ok(patient_id: str, staff: Optional[StaffContext]) -> bool:
    """True if the caller may access this patient: either authorized clinical
    staff (scoped to the patient's health system) or a patient session bound to
    this exact patient_id. Mirrors the prior lenient behavior for landing/demo
    staff so existing staff flows are unchanged."""
    if patient_id not in _patient_store:
        return False
    if staff is not None:
        # Mirror staff_context.assert_staff_patient_scope so the patient-facing
        # gate is consistent with the strict clinical gate: tenant staff are
        # scoped to their own health system; landing/demo staff are scoped to the
        # demo health system (a self-registered landing user must NOT be able to
        # read real tenant PHI). Patients with no health_system_id stay reachable.
        d = _patient_store.get(patient_id) or {}
        hs = str(d.get("health_system_id") or "")
        if staff.source == "tenant":
            return (not hs) or (bool(staff.tenant_id) and hs == str(staff.tenant_id))
        return (not hs) or hs == DEMO_HEALTH_SYSTEM_ID
    ps = current_patient_session()
    return ps is not None and ps.patient_id == patient_id


def _assert_staff_can_access_patient(patient_id: str, staff: Optional[StaffContext]) -> None:
    """Authorize a patient-or-staff route. Requires EITHER scoped clinical staff
    OR a patient session bound to this patient_id. Unauthenticated/wrong-patient
    access returns 404 (no id enumeration). Previously this returned early when
    ``staff is None``, which left every patient PHI route open (PRD-1)."""
    if _patient_principal_ok(patient_id, staff):
        return
    if staff is None and current_patient_session() is None and not _enforce_patient_auth():
        _auth_logger.warning(
            "ENFORCE_PATIENT_AUTH=0: allowing unauthenticated access to patient %s", patient_id
        )
        return
    raise HTTPException(status_code=404, detail="Patient not found")


# ─── Patient entry experience (code-entry page + session-expired page) ────────
# Shared chrome so the self-contained patient pages match the recovery email /
# dashboard brand (cyan header, Archangel Health wordmark, soft card on #f3f6f9).
_PATIENT_PAGE_CSS = (
    "*{box-sizing:border-box}"
    "body{margin:0;background:#f3f6f9;font-family:-apple-system,BlinkMacSystemFont,"
    "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a}"
    ".wrap{max-width:560px;margin:0 auto;padding:32px 16px}"
    ".card{background:#fff;border:1px solid #dbe5ec;border-radius:14px;overflow:hidden;"
    "box-shadow:0 1px 2px rgba(15,23,42,.04)}"
    ".head{background:#0891b2;padding:28px 24px;text-align:center}"
    ".brand{font-family:Georgia,serif;color:#fff;font-size:32px;font-weight:700;line-height:1.15}"
    ".sub{color:#e0f2fe;font-size:14px;margin-top:6px}"
    ".body{padding:26px 26px 30px}"
    "h1{font-size:20px;margin:0 0 6px}"
    "p.lead{color:#475569;font-size:15px;line-height:1.6;margin:0 0 8px}"
    "label{display:block;font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;"
    "color:#64748b;margin:18px 0 8px}"
    "input{width:100%;padding:14px 16px;border:1px solid #cbd5e1;border-radius:12px;background:#f8fafc;"
    "font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,'Courier New',monospace;font-size:20px;"
    "font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#111827}"
    "input:focus{outline:none;border-color:#0891b2;box-shadow:0 0 0 3px rgba(8,145,178,.15);background:#fff}"
    ".btn{display:block;width:100%;margin-top:22px;padding:15px 16px;background:#0891b2;border:1px solid #0e7490;"
    "border-radius:12px;color:#fff;font-size:17px;font-weight:700;cursor:pointer;text-align:center;text-decoration:none}"
    ".btn:disabled{opacity:.6;cursor:default}"
    ".msg{margin-top:14px;font-size:14px;color:#b91c1c;min-height:20px;text-align:center}"
    ".tip{margin-top:18px;background:#f0f9ff;border:1px solid #bae6fd;border-radius:12px;padding:12px 14px;"
    "color:#334155;font-size:13px;line-height:1.6;text-align:center}"
    ".foot{text-align:center;color:#64748b;font-size:12px;padding-top:16px}"
)


def _patient_entry_url() -> str:
    """Canonical patient code-entry URL. Prefer the landing app's recovery-plan
    form when LANDING_URL is configured; otherwise fall back to the self-contained
    backend code-entry page (/recovery) so patient links never dead-end on a bare
    dashboard URL (which now requires a session)."""
    landing = (os.getenv("LANDING_URL") or "").strip().rstrip("/")
    if landing:
        return f"{landing}/#recovery-plan"
    base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/recovery"


def _render_patient_entry_page(prefill_hs: str = "") -> str:
    hs_safe = html_lib.escape(prefill_hs or "", quote=True)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Open Your Plan — Archangel Health</title>"
        f"<style>{_PATIENT_PAGE_CSS}</style></head><body><div class='wrap'><div class='card'>"
        "<div class='head'><div class='brand'>Archangel Health</div>"
        "<div class='sub'>Open your personalized care plan</div></div>"
        "<div class='body'><h1>Enter your access codes</h1>"
        "<p class='lead'>These two codes are in the email or text from your care team. "
        "They keep your health information private.</p>"
        "<label for='hs'>Health System Code</label>"
        f"<input id='hs' autocomplete='off' autocapitalize='characters' spellcheck='false' "
        f"placeholder='ABCD1234' value='{hs_safe}'>"
        "<label for='rc'>Resource Code</label>"
        "<input id='rc' autocomplete='off' autocapitalize='characters' spellcheck='false' placeholder='WXYZ5678'>"
        "<button class='btn' id='go'>View My Plan</button>"
        "<p class='msg' id='msg' role='alert'></p>"
        "<div class='tip'>Tip: codes are not case-sensitive. For the best experience, open this on a computer.</div>"
        "</div></div><div class='foot'>Archangel Health · Your personalized healthcare companion</div></div>"
        "<script>"
        "var hs=document.getElementById('hs'),rc=document.getElementById('rc'),"
        "go=document.getElementById('go'),msg=document.getElementById('msg');"
        "(hs.value?rc:hs).focus();"
        "async function submit(){"
        "var h=hs.value.trim().toUpperCase(),r=rc.value.trim().toUpperCase();"
        "if(!h||!r){msg.textContent='Please enter both codes.';return;}"
        "go.disabled=true;msg.style.color='#475569';msg.textContent='Opening your plan…';"
        "try{var p=new URLSearchParams({health_system_code:h,resource_code:r});"
        "var res=await fetch('/api/patient/by-codes?'+p.toString());"
        "if(res.ok){var data=await res.json();window.location.href=data.dashboard_url;return;}"
        "var d=await res.json().catch(function(){return {};});msg.style.color='#b91c1c';"
        "msg.textContent=(res.status===429)?'Too many attempts. Please wait a minute and try again.':"
        "(d.detail||'We could not find a match. Check your codes and try again.');"
        "}catch(e){msg.style.color='#b91c1c';msg.textContent='Connection problem. Please try again.';}"
        "go.disabled=false;}"
        "go.addEventListener('click',submit);"
        "rc.addEventListener('keydown',function(e){if(e.key==='Enter')submit();});"
        "hs.addEventListener('keydown',function(e){if(e.key==='Enter')rc.focus();});"
        "</script></body></html>"
    )


def _patient_reentry_response() -> HTMLResponse:
    """Friendly 'session expired' page for unauthenticated patient *page* loads.
    Identical regardless of whether the patient_id exists (status 404, same body)
    so it cannot be used to enumerate valid patient ids (PRD-1)."""
    entry = html_lib.escape(_patient_entry_url(), quote=True)
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Session expired — Archangel Health</title>"
        f"<style>{_PATIENT_PAGE_CSS}</style></head><body><div class='wrap'><div class='card'>"
        "<div class='head'><div class='brand'>Archangel Health</div>"
        "<div class='sub'>Secure patient access</div></div>"
        "<div class='body' style='text-align:center'><h1>Your secure session has ended</h1>"
        "<p class='lead'>For your privacy, access expires after a period of inactivity. "
        "Re-enter your access codes (from your email or text) to continue.</p>"
        f"<a class='btn' href='{entry}'>Enter my codes</a>"
        "</div></div><div class='foot'>Archangel Health · Your personalized healthcare companion</div></div>"
        "</body></html>"
    )
    return HTMLResponse(content=body, status_code=404)


def _patient_page_entry(
    request: Request,
    patient_id: str,
    k: Optional[str],
    staff: Optional[StaffContext],
):
    """Guard for server-rendered patient pages. Returns a RedirectResponse (which
    mints the pt_session cookie from a one-time entry token ``?k=``) that the
    caller must return; returns None when the caller is already authorized and the
    page should render; otherwise returns a friendly 404 re-entry page."""
    if patient_id in _patient_store:
        if _patient_principal_ok(patient_id, staff):
            return None
        if k:
            sess = consume_entry_token(k)
            if sess and sess.patient_id == patient_id:
                resp = RedirectResponse(url=request.url.path, status_code=302)
                set_patient_session_cookie(resp, patient_id, sess.health_system_id)
                return resp
        if current_patient_session() is None and staff is None and not _enforce_patient_auth():
            _auth_logger.warning(
                "ENFORCE_PATIENT_AUTH=0: rendering patient page %s without auth", patient_id
            )
            return None
    # Unauthorized OR nonexistent -> identical response (no id enumeration).
    return _patient_reentry_response()


def _require_clinical_staff(staff: Optional[StaffContext]) -> StaffContext:
    return require_clinical_auth(staff)


def _assert_clinical_staff_can_access_patient(patient_id: str, staff: Optional[StaffContext]) -> None:
    patient = _patient_store.get(patient_id)
    assert_staff_patient_scope(patient=patient, staff=staff)


def _staff_actor_id(staff: Optional[StaffContext]) -> str:
    if staff and staff.email:
        return staff.email
    return "unknown"


def _normalize_tier(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value in (1, 2, 3) else None
    raw = str(value).strip().upper()
    if raw in ("1", "2", "3"):
        return int(raw)
    if raw.startswith("TIER_"):
        tail = raw.split("_")[-1]
        if tail in ("1", "2", "3"):
            return int(tail)
    return None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _provider_role_display(role: str) -> str:
    norm = str(role or "").strip().lower()
    if norm == "surgeon":
        return "Surgeon"
    if norm == "rn_coordinator":
        return "RN Coordinator"
    if norm == "np_pa":
        return "NP/PA"
    if norm == "system_admin":
        return "Administrator"
    return norm.replace("_", " ").title() if norm else "Clinician"


def _provider_email_signature(staff: Optional[StaffContext]) -> str:
    if not staff:
        return "Care Team, Clinician, Archangel Health"
    provider_name = (staff.name or "").strip() or (staff.email or "").strip() or "Care Team"
    provider_role = _provider_role_display(staff.role)
    institution = "Archangel Health"
    if staff.tenant_id:
        hs = _team_store.get_health_system_by_id(staff.tenant_id) or {}
        institution = str(hs.get("name") or institution).strip() or institution
    return f"{provider_name}, {provider_role}, {institution}"


def _validate_patient_iso_date(value: str, field_name: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be YYYY-MM-DD") from exc


def _patient_anchor_for_roster(d: Dict[str, Any], sd: Dict[str, Any]) -> str:
    fam = (d.get("anchor_procedure_family") or "").strip()
    if fam in _ANCHOR_PROCEDURES:
        return fam
    proc = (sd.get("procedure_name") or "").strip()
    if proc in _ANCHOR_PROCEDURES:
        return proc
    return fam or proc or ""


def _patient_dob_for_roster(sd: Dict[str, Any]) -> str:
    return (sd.get("dob") or sd.get("date_of_birth") or "").strip()


def _facility_display_name_for_patient(patient_id: str) -> str:
    d = _patient_store.get(patient_id) or {}
    hs_id = d.get("health_system_id") or ""
    if hs_id:
        rec = _team_store.get_health_system_by_id(hs_id)
        name = (rec or {}).get("name") or ""
        if str(name).strip():
            return str(name).strip()
    return ""


def _patient_prep_document(patient_id: str) -> Dict[str, Any]:
    d = _patient_store.get(patient_id) or {}
    sd = d.get("structured_data") or {}
    return {
        "procedure_name": sd.get("procedure_name", ""),
        "procedure_site": sd.get("surgical_site", ""),
        "laterality": sd.get("laterality", ""),
        "cpt_codes": sd.get("cpt_codes", []),
        "surgeon_name": sd.get("surgeon_name", ""),
        "anesthesiologist": sd.get("anesthesiologist", ""),
        "procedure_date": sd.get("procedure_date", ""),
        "facility": sd.get("facility", ""),
        "procedure_type": d.get("specialty") or "",
        "estimated_duration": sd.get("estimated_duration", ""),
        "pre_op_diagnosis": sd.get("pre_op_diagnosis", ""),
        "pre_op_instructions": sd.get("pre_op_instructions", ""),
        "medications_to_hold": sd.get("medications_to_hold", []),
        "medications_to_take_morning_of": sd.get("medications_to_take_morning_of", []),
        "labs_ordered": sd.get("labs_ordered", ""),
        "pre_op_clearance_letters": sd.get("pre_op_clearance_letters", ""),
    }


def _intake_doctor_recipients(patient_id: str) -> List[str]:
    d = _patient_store.get(patient_id) or {}
    hs_id = d.get("health_system_id") or ""
    recipients: List[str] = []
    if hs_id:
        for member in _team_store.list_team_members(hs_id):
            role = str(member.get("role") or "").lower()
            if role in {"surgeon", "rn_coordinator", "np_pa"}:
                recipients.append(f"tenant:{member.get('email', '').lower().strip()}")
    # Public/demo fallback recipient used by legacy doctor portal.
    if not recipients:
        recipients.append("doctor:default")
    return sorted(set(recipients))


def _create_intake_notifications(patient_id: str, intake_form_id: str, notif_type: str, message: str) -> None:
    for doctor_id in _intake_doctor_recipients(patient_id):
        _team_store.create_intake_notification(
            notification_id=str(uuid.uuid4()),
            doctor_id=doctor_id,
            intake_form_id=intake_form_id,
            notification_type=notif_type,
            message=message,
        )

SURVEY_DAY_CONFIG = {7: 6, 14: 13, 30: 29}  # days from open_date

DAY_7_QUESTIONS = [
    "How clear was the information about prescription medications to take at home?",
    "How clear was the information about potential side effects and what to watch for?",
    "How clear was the information about which medications to stop, avoid, or take differently?",
    "How well did the information apply to your specific health situation and recovery needs?",
    "How clear was the information about symptoms or warning signs to watch for?",
    "How clear was the information about when and how to contact your care team?",
    "How clear was the information about physical activities to do or avoid?",
    "How clear was the information about diet or eating restrictions?",
    "How clear was the information about returning to daily activities (driving, working, exercising)?",
]

DAY_7_OPTIONS = ["Very Clear", "Somewhat Clear", "Not Clear", "Does not apply"]
DAY_14_30_OPTIONS = ["Strongly Agree", "Agree", "Disagree", "Strongly Disagree"]
DAY_14_QUESTIONS = [
    "I still clearly understand the instructions my care team gave me at discharge.",
    "I know which symptoms require contacting my care team vs. managing at home.",
    "I feel confident managing my recovery without needing to visit the ER.",
    "I have been able to reach my care team easily when I had questions.",
    "I feel well-prepared for the remainder of my recovery.",
]
DAY_30_QUESTIONS = [
    "My discharge instructions were communicated clearly and in a way I could understand.",
    "The information I received adequately prepared me for recovery at home.",
    "Throughout my recovery I always knew when to seek medical help vs. manage symptoms on my own.",
    "Overall I am satisfied with how my care team communicated with me after my procedure.",
]

HARD_TIER_1_PHRASES = {
    "can't breathe",
    "chest pain",
    "can't feel my legs",
    "can't control my bladder",
    "want to die",
    "bleeding won't stop",
    "can't swallow",
    "throat is swelling",
}

TIER_1_RESPONSE = (
    "What you're describing sounds like it needs immediate medical attention. "
    "Please call 911 or go to the emergency room right now. "
    "I'm also notifying your care team. Do not wait."
)

AI_DISCLAIMER = (
    "This was created with AI assistance. AI can make mistakes. This is not medical advice or a diagnosis. "
    "Always talk to a doctor about your health. If this is an emergency, call 911."
)

SPECIALTY_FORM_LIBRARY_PATH = os.path.join(os.path.dirname(__file__), "intake_form_library.json")
SPECIALTY_FRAMEWORK_PATH = os.path.join(os.path.dirname(__file__), "intake_frameworks.json")
ENABLE_PREOP_INTAKE_BOT_V2 = os.getenv("ENABLE_PREOP_INTAKE_BOT_V2", "1") == "1"

DEFAULT_FORM_LIBRARY: Dict[str, Dict[str, Any]] = {
    "Orthopedic": {
        "template_name": "Orthopedic Intake Form",
        "header_fields": ["Patient Name", "DOB", "MRN", "Procedure", "Surgeon", "Surgery Date"],
        "sections": {
            "Pre-Op Testing Acknowledgment": [
                "Blood work ordered/reviewed",
                "EKG reviewed if indicated",
                "Imaging reviewed for operative site",
            ],
            "Medication Instructions Acknowledged": [
                "Blood thinner stop date reviewed",
                "NSAID stop guidance reviewed",
                "Diabetes medication plan reviewed",
            ],
            "Day-of-Surgery Prep": [
                "NPO timing confirmed",
                "CHG wash instructions confirmed",
                "Arrival time and check-in location confirmed",
            ],
            "Home Preparation Confirmed": [
                "Ride home arranged",
                "Caregiver support confirmed",
                "Home safety adjustments reviewed",
            ],
            "Consent Forms": [
                "Procedure consent",
                "Anesthesia consent",
                "HIPAA acknowledgement",
            ],
        },
        "final_review_fields": [
            "Questions from patient / items needing follow-up",
            "Additional instructions given",
            "Reviewed by (staff name / role)",
            "Date",
            "Patient initials confirming review",
            "Patient signature",
        ],
    },
    "Cardiac": {
        "template_name": "Cardiac Surgical Intake Form",
        "header_fields": ["Patient Name", "DOB", "MRN", "Procedure", "Surgeon", "Surgery Date"],
        "sections": {
            "Pre-Op Testing Acknowledgment": [
                "Cardiac clearance reviewed",
                "Echo/EKG reviewed",
                "Recent labs reviewed",
            ],
            "Medication Instructions Acknowledged": [
                "Anticoagulant hold/restart plan reviewed",
                "Beta blocker instructions reviewed",
                "Diabetes/insulin peri-op instructions reviewed",
            ],
            "Day-of-Surgery Prep": [
                "NPO timing confirmed",
                "Morning medication plan confirmed",
                "Arrival and admission details reviewed",
            ],
            "Home Preparation Confirmed": [
                "Discharge support arranged",
                "Transportation plan confirmed",
            ],
            "Consent Forms": [
                "Procedure consent",
                "Blood product consent",
                "Anesthesia consent",
            ],
        },
        "final_review_fields": [
            "Questions from patient / items needing follow-up",
            "Additional instructions given",
            "Reviewed by (staff name / role)",
            "Date",
            "Patient initials confirming review",
            "Patient signature",
        ],
    },
    "Spine": {
        "template_name": "Spine Procedure Intake Form",
        "header_fields": ["Patient Name", "DOB", "MRN", "Procedure", "Surgeon", "Surgery Date"],
        "sections": {
            "Pre-Op Testing Acknowledgment": [
                "Spine imaging reviewed",
                "Neuro baseline documented",
                "Pre-op labs reviewed",
            ],
            "Medication Instructions Acknowledged": [
                "Blood thinner stop date reviewed",
                "Pain medication plan reviewed",
                "Supplements/herbal hold list reviewed",
            ],
            "Day-of-Surgery Prep": [
                "NPO timing confirmed",
                "Skin prep instructions confirmed",
                "Arrival time and location confirmed",
            ],
            "Home Preparation Confirmed": [
                "Post-op support person confirmed",
                "Home mobility support reviewed",
            ],
            "Consent Forms": [
                "Procedure consent",
                "Implant consent",
                "Anesthesia consent",
            ],
        },
        "final_review_fields": [
            "Questions from patient / items needing follow-up",
            "Additional instructions given",
            "Reviewed by (staff name / role)",
            "Date",
            "Patient initials confirming review",
            "Patient signature",
        ],
    },
    "General Surgery": {
        "template_name": "General Surgery Intake Form",
        "header_fields": ["Patient Name", "DOB", "MRN", "Procedure", "Surgeon", "Surgery Date"],
        "sections": {
            "Pre-Op Testing Acknowledgment": [
                "Required labs reviewed",
                "Procedure-specific imaging reviewed",
            ],
            "Medication Instructions Acknowledged": [
                "Medication hold list reviewed",
                "Morning-of-surgery medication plan reviewed",
            ],
            "Day-of-Surgery Prep": [
                "NPO timing confirmed",
                "Arrival and registration timing confirmed",
            ],
            "Home Preparation Confirmed": [
                "Discharge transportation arranged",
                "Recovery support available",
            ],
            "Consent Forms": [
                "Procedure consent",
                "Anesthesia consent",
            ],
        },
        "final_review_fields": [
            "Questions from patient / items needing follow-up",
            "Additional instructions given",
            "Reviewed by (staff name / role)",
            "Date",
            "Patient initials confirming review",
            "Patient signature",
        ],
    },
}

DEFAULT_FRAMEWORKS: Dict[str, str] = {
    "Orthopedic": "Ask one question at a time. Emphasize joint/spine symptoms, mobility limits, and prior orthopedic surgeries.",
    "Cardiac": "Ask one question at a time. Emphasize cardiopulmonary symptoms, exertional tolerance, and anticoagulation history.",
    "Spine": "Ask one question at a time. Emphasize neuro symptoms, radiation pattern, motor weakness, and prior spine interventions.",
    "General Surgery": "Ask one question at a time. Emphasize abdominal symptoms, bowel patterns, prior abdominal surgery, and infection risk.",
}


DEMO_DOCTOR_EMAIL = "manan.vyas@cedarssinai.com"
DEMO_DOCTOR_PASSWORD = (os.getenv("DEMO_DOCTOR_PASSWORD") or "ChangeMeCedarDemo!").strip()
DEMO_CLINIC_CODE = "CDRSNAI1"


def _is_demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "0").strip().lower() in ("1", "true", "yes", "on")


def _disable_public_demo_account() -> bool:
    """When true, skip seeding the shared landing demo user (see DEMO_DOCTOR_EMAIL)."""
    return os.getenv("DISABLE_PUBLIC_DEMO_ACCOUNT", "0").strip().lower() in ("1", "true", "yes", "on")


def _disable_scheduler_in_demo() -> bool:
    return os.getenv("DEMO_DISABLE_TEAM_SCHEDULER", "1").strip().lower() in ("1", "true", "yes", "on")


def _demo_seed_strategy() -> str:
    strategy = os.getenv("DEMO_SEED_STRATEGY", "preserve").strip().lower()
    return "reset" if strategy == "reset" else "preserve"


def _triage_demo_enabled() -> bool:
    return os.getenv("ENABLE_TRIAGE_DEMO", "1").strip().lower() in ("1", "true", "yes", "on")


def _demo_patient_store_persistence_enabled() -> bool:
    if not _is_demo_mode():
        return False
    v = os.getenv("DEMO_PERSIST_PATIENT_STORE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _demo_patient_store_snapshot_path() -> Optional[str]:
    if not _demo_patient_store_persistence_enabled():
        return None
    p = (os.getenv("DEMO_PATIENT_STORE_PATH") or "").strip()
    if p:
        return os.path.abspath(p)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "demo_patient_store.json")


# PHI fields encrypted at rest in the persisted patient-store snapshot (PRD-6).
# String fields are encrypted directly; dict fields are JSON-serialized first.
_SNAPSHOT_PHI_STR_FIELDS = ("name", "phone", "email", "voice_script", "battlecard_html")
_SNAPSHOT_PHI_JSON_FIELDS = ("structured_data", "resources")


def _encrypt_patient_blob(blob: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(blob)
    for f in _SNAPSHOT_PHI_STR_FIELDS:
        v = out.get(f)
        # Skip already-encrypted values so re-encrypting stays idempotent
        # (a second pass would otherwise double-encrypt and break decryption).
        if isinstance(v, str) and v and not field_crypto.is_encrypted(v):
            out[f] = field_crypto.encrypt_field(v)
    for f in _SNAPSHOT_PHI_JSON_FIELDS:
        v = out.get(f)
        # Already-encrypted JSON fields are tokens (str), so this dict/list check
        # naturally skips them.
        if isinstance(v, (dict, list)) and v:
            out[f] = field_crypto.encrypt_value(v)
    return out


def _decrypt_patient_blob(blob: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(blob)
    for f in _SNAPSHOT_PHI_STR_FIELDS:
        if field_crypto.is_encrypted(out.get(f)):
            out[f] = field_crypto.decrypt_field(out[f])
    for f in _SNAPSHOT_PHI_JSON_FIELDS:
        if field_crypto.is_encrypted(out.get(f)):
            out[f] = field_crypto.decrypt_value(out[f])
    return out


def _load_demo_patient_store_snapshot() -> None:
    path = _demo_patient_store_snapshot_path()
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[demo-persist] failed to load snapshot {path}: {e}")
        return
    if not isinstance(data, dict):
        print(f"[demo-persist] snapshot must be a JSON object, got {type(data).__name__}")
        return
    for pid, entry in data.items():
        if isinstance(entry, dict):
            try:
                _patient_store[str(pid)] = _decrypt_patient_blob(entry)
            except ValueError as e:
                print(f"[demo-persist] could not decrypt snapshot entry {pid}: {e}")


def _persist_demo_patient_store() -> None:
    path = _demo_patient_store_snapshot_path()
    if not path:
        return
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        # Encrypt PHI fields at rest before writing the snapshot (PRD-6). No-op
        # when no key is configured (dev) — fields are written as-is.
        encrypted = {pid: _encrypt_patient_blob(blob) for pid, blob in _patient_store.items()}
        payload = json.dumps(encrypted, indent=2, default=str, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(prefix=".demo_patient_store_", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        print(f"[demo-persist] failed to write snapshot {path}: {e}")


def _ensure_demo_doctor() -> None:
    if not DEMO_DOCTOR_PASSWORD:
        print("[demo-seed] DEMO_DOCTOR_PASSWORD unset; skipping shared landing demo doctor seed.")
        return
    users = auth_module._get_users()  # noqa: SLF001
    key = DEMO_DOCTOR_EMAIL.lower().strip()
    if key not in users:
        register_user(DEMO_DOCTOR_EMAIL, DEMO_DOCTOR_PASSWORD, "Dr. Manan Vyas")
        users = auth_module._get_users()  # noqa: SLF001
    users[key].update(
        {
            "email": key,
            "password_hash": auth_module.pwd_context.hash(DEMO_DOCTOR_PASSWORD),  # noqa: SLF001
            "name": "Dr. Manan Vyas",
            "role": "surgeon",
            "office_phone": "(310) 555-0100",
            "doctor_type": "General Surgeon",
            "hospital_affiliations": "Cedars-Sinai Medical Center",
            "clinic_code": DEMO_CLINIC_CODE,
        }
    )
    auth_module._persist_users()  # noqa: SLF001


def _build_demo_battlecard(title: str, bullets: List[str]) -> str:
    bullet_html = "".join(f"<li>{html_lib.escape(item)}</li>" for item in bullets)
    return (
        "<div style='font-family:Inter,Arial,sans-serif;max-width:700px;margin:0 auto;"
        "border:1px solid #dbe5ec;border-radius:12px;overflow:hidden;background:#fff;'>"
        f"<div style='background:#0ea5b3;color:#fff;padding:12px 14px;font-weight:700;'>{html_lib.escape(title)}</div>"
        f"<ul style='margin:0;padding:14px 20px 16px 32px;color:#1f2937;font-size:14px;line-height:1.55;'>{bullet_html}</ul>"
        "</div>"
    )


def _demo_patient_blueprint() -> List[Dict[str, Any]]:
    names = [
        "Thenuk Rodrigo",
        "Tej Patel",
        "Arya Bhatia",
        "James Wilson", "Aisha Khan", "Michael Thompson", "Sophia Lee", "Noah Patel", "Emma Garcia",
        "Liam Brooks", "Olivia Cruz", "Ethan Kim", "Mia Turner", "Lucas Reed", "Ava Foster",
        "Mason Diaz", "Ella Bennett", "Logan Ward", "Harper Cox", "Jacob Long", "Amelia Hayes",
        "Jackson Ross", "Evelyn Bell", "Aiden Bailey", "Abigail Howard", "Sebastian Jenkins",
        "Scarlett Perry", "Carter Gray", "Grace Simmons", "Wyatt Coleman", "Chloe Richardson",
        "Luke Hughes", "Lily Bryant", "Owen Ramirez", "Aria Murphy", "Gabriel James", "Nora Cook",
        "Henry Morris", "Zoey Price", "Levi Griffin", "Hannah Kelly", "Isaac Peterson", "Layla Butler",
        "Caleb Gonzales", "Stella Barnes", "Ryan Powell", "Lucy Hill", "Nathan Flores", "Aurora Adams",
        "Adrian Young",
    ]
    procedures = [
        "Laparoscopic Appendectomy", "Laparoscopic Cholecystectomy", "Inguinal Hernia Repair", "Umbilical Hernia Repair",
        "Hiatal Hernia Repair", "Colectomy", "Bowel Resection", "Sigmoidectomy", "Thyroidectomy",
        "Whipple Procedure", "Gastric Bypass", "Splenectomy", "Gastrectomy", "Nissen Fundoplication",
        "Adrenalectomy", "Pancreatectomy",
    ]
    rows: List[Dict[str, Any]] = []
    for idx, name in enumerate(names):
        first, last = name.split(" ", 1)
        pipeline = "pre_op" if idx < 22 else "post_op"
        patient_id = f"demo_{first.lower()}_{last.lower()}_{idx+1:03d}".replace(" ", "_")
        procedure = procedures[idx % len(procedures)]
        if name == "Thenuk Rodrigo":
            patient_id = "demo_thenuk_001"
            pipeline = "post_op"
            procedure = "Inguinal Hernia Repair"
        elif name == "Tej Patel":
            patient_id = "demo_tej_patel_001"
            pipeline = "post_op"
            procedure = "Laparoscopic Cholecystectomy"
        elif name == "Arya Bhatia":
            patient_id = "demo_arya_bhatia_001"
            pipeline = "post_op"
            procedure = "Laparoscopic Appendectomy"

        rows.append(
            {
                "idx": idx,
                "id": patient_id,
                "name": name,
                "first": first,
                "last": last,
                "pipeline_type": pipeline,
                "procedure_name": procedure,
                "pcp_referral_sent": idx % 4 != 0,  # ~75%
            }
        )
    return rows


def _seed_demo_patient_store() -> List[Dict[str, Any]]:
    today = date.today()
    rows = _demo_patient_blueprint()
    _patient_store.clear()
    for row in rows:
        i = row["idx"]
        is_pre = row["pipeline_type"] == "pre_op"
        procedure_date = (today + timedelta(days=(i % 15) + 2)) if is_pre else (today - timedelta(days=(i % 20)))
        phone = f"+1 (310) 555-{1000 + i:04d}"
        email = f"{row['first'].lower()}.{row['last'].lower()}@email.com".replace(" ", "")
        resource_code = f"CDR{i+1:05d}"[-8:]
        preop_resource = {
            "voice_script": (
                f"[reassuring] Hey {row['first']}... your surgery prep is straightforward. "
                "Follow fasting and medication hold instructions. Confirm your ride and arrive early."
            ),
            "battlecard_html": _build_demo_battlecard(
                f"{row['procedure_name']} - Pre-Op Preparation Card",
                [
                    "What to expect from check-in through recovery",
                    "Day before surgery checklist and fasting plan",
                    "Day of surgery arrival instructions",
                    "What to bring and warning signs to report",
                ],
            ),
            "voice_audio_url": None,
        }
        diagnosis = {
            "voice_script": f"[clear] {row['first']}, your {row['procedure_name']} was completed and findings were reviewed.",
            "battlecard_html": _build_demo_battlecard(
                f"{row['procedure_name']} - Diagnosis Summary",
                ["Procedure completed", "Clinical findings explained", "Expected recovery milestones outlined"],
            ),
            "voice_audio_url": None,
        }
        treatment = {
            "voice_script": f"[reassuring] {row['first']}, follow medication timing, wound care, activity limits, and warning signs.",
            "battlecard_html": _build_demo_battlecard(
                f"{row['procedure_name']} - Recovery Plan",
                ["Medication and pain-control plan", "Diet and mobility progression", "When to call your care team"],
            ),
            "voice_audio_url": None,
        }
        pcp_name = None

        resources = {"preop": preop_resource} if is_pre else {"diagnosis": diagnosis, "treatment": treatment}
        _patient_store[row["id"]] = {
            "name": row["name"],
            "health_system_id": DEMO_HEALTH_SYSTEM_ID,
            "phone": phone,
            "email": email,
            "pipeline_type": row["pipeline_type"],
            "voice_audio_url": None,
            "battlecard_html": (resources.get("preop") or resources.get("diagnosis") or {}).get("battlecard_html"),
            "avatar_url": None,
            "voice_script": (resources.get("preop") or resources.get("diagnosis") or {}).get("voice_script"),
            "structured_data": {
                "patient_name": row["name"],
                "procedure_name": row["procedure_name"],
                "procedure_date": procedure_date.isoformat(),
                "status": "scheduled" if is_pre else "completed",
                "pcp_referral_sent": row["pcp_referral_sent"],
                "pcp_name": pcp_name,
            },
            "clinic_code": DEMO_CLINIC_CODE,
            "resource_code": resource_code,
            "office_phone": os.getenv("CARE_TEAM_PHONE", ""),
            "resources": resources,
            "pcp_referral_sent": row["pcp_referral_sent"],
            "pcp_name": pcp_name,
        }
    return rows


def _clear_demo_sqlite_rows(patient_ids: List[str]) -> None:
    if not patient_ids:
        return
    placeholders = ",".join("?" for _ in patient_ids)
    with sqlite3.connect(_team_store.db_path) as conn:
        conn.execute(f"DELETE FROM escalations WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM survey_responses WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM survey_sends WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM daily_reminders WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM event_logs WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM episodes WHERE patient_id IN ({placeholders})", patient_ids)


def _seed_demo_sqlite(rows: List[Dict[str, Any]], strategy: str) -> None:
    clinic_episodes = [ep for ep in _team_store.list_active_episodes() if (ep.get("clinic_code") or "").upper() == DEMO_CLINIC_CODE]
    if strategy == "preserve" and len(clinic_episodes) >= len(rows):
        return
    patient_ids = [r["id"] for r in rows]
    if strategy == "reset":
        _clear_demo_sqlite_rows(patient_ids)

    rng = random.Random(42)
    today = date.today()
    post_ids = [r["id"] for r in rows if r["pipeline_type"] == "post_op"]
    pre_ids = [r["id"] for r in rows if r["pipeline_type"] == "pre_op"]
    open_dates: Dict[str, date] = {}

    for row in rows:
        i = int(row["idx"])
        if row["id"] == "demo_thenuk_001":
            # Day 17 in episode (current_day = diff + 1).
            open_date = today - timedelta(days=16)
        else:
            open_date = today if row["pipeline_type"] == "pre_op" else (today - timedelta(days=(i % 18)))
        _team_store.ensure_episode(
            patient_id=row["id"],
            open_date=open_date.isoformat(),
            procedure_type=row["procedure_name"],
            clinic_code=DEMO_CLINIC_CODE,
            resource_code=_patient_store[row["id"]]["resource_code"],
            health_system_id=DEMO_HEALTH_SYSTEM_ID,
        )
        open_dates[row["id"]] = open_date

    def _event(pid: str, day_offset: int, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        ts = datetime.combine(open_dates[pid] + timedelta(days=max(0, day_offset)), datetime.utcnow().time()).replace(microsecond=0)
        _team_store.log_event(patient_id=pid, event_type=event_type, occurred_at=ts.isoformat(), payload=payload or {})

    for pid in post_ids:
        if rng.random() < 0.85:
            _event(pid, 0, "platform_opened")
        if rng.random() < 0.70:
            _event(pid, 1, "diagnosis_video_watched")
        if rng.random() < 0.60:
            _event(pid, 2, "treatment_video_watched")
        if rng.random() < 0.35:
            _event(pid, 3, "avatar_chat")
        if _patient_store[pid].get("pcp_referral_sent"):
            _event(pid, 4, "email_sent", {"channel": "pcp_summary_referral"})
    for pid in pre_ids:
        if rng.random() < 0.85:
            _event(pid, 0, "platform_opened")
        if rng.random() < 0.65:
            _event(pid, 1, "preop_video_watched")
        if rng.random() < 0.55:
            _event(pid, 2, "preop_intake_submitted")

    thenuk_id = "demo_thenuk_001"
    thenuk_answers = []
    thenuk_answer_options = ["Strongly Agree", "Agree", "Strongly Agree", "Agree", "Strongly Agree"]
    for idx, _question in enumerate(DAY_14_QUESTIONS, start=1):
        thenuk_answers.append({"question_index": idx, "response": thenuk_answer_options[idx - 1]})
    thenuk_score = _score_survey_answers(thenuk_answers)
    thenuk_day7_answers = [
        {"question_index": 1, "response": "Very Clear"},
        {"question_index": 2, "response": "Somewhat Clear"},
        {"question_index": 3, "response": "Not Clear"},
        {"question_index": 4, "response": "Somewhat Clear"},
        {"question_index": 5, "response": "Very Clear"},
        {"question_index": 6, "response": "Somewhat Clear"},
        {"question_index": 7, "response": "Not Clear"},
        {"question_index": 8, "response": "Somewhat Clear"},
        {"question_index": 9, "response": "Very Clear"},
    ]
    thenuk_day7_score = _score_survey_answers(thenuk_day7_answers)
    _team_store.save_survey_response(
        patient_id=thenuk_id,
        survey_day=7,
        answers=thenuk_day7_answers,
        score=thenuk_day7_score.get("score"),
        tier=thenuk_day7_score.get("tier"),
    )
    _event(thenuk_id, 7, "survey_completed", {"survey_day": 7, "score": thenuk_day7_score.get("score")})
    _team_store.save_survey_response(
        patient_id=thenuk_id,
        survey_day=14,
        answers=thenuk_answers,
        score=thenuk_score.get("score"),
        tier=thenuk_score.get("tier"),
    )
    _event(thenuk_id, 14, "survey_completed", {"survey_day": 14, "score": thenuk_score.get("score")})

    escalation_ids = post_ids[:12]
    tiers = ([1] * 4) + ([2] * 8) + ([3] * 8)
    for i, tier in enumerate(tiers):
        pid = escalation_ids[i % len(escalation_ids)]
        day = 2 + (i % 12)
        _team_store.create_escalation(
            patient_id=pid,
            tier=tier,
            trigger_type="care_team_notification_demo",
            message=f"Demo escalation tier {tier} for symptom concern",
            conversation_snapshot=[
                {"role": "patient", "content": "I have worsening symptoms."},
                {"role": "assistant", "content": "Thanks for sharing this. I am escalating to your care team."},
            ],
            created_at=(datetime.combine(open_dates[pid] + timedelta(days=day), datetime.utcnow().time()).replace(microsecond=0).isoformat()),
            health_system_id=_patient_store.get(pid, {}).get("health_system_id") or DEMO_HEALTH_SYSTEM_ID,
        )


async def _seed_demo_mode_data() -> None:
    if not _is_demo_mode():
        return
    _team_store.ensure_demo_health_system(
        hs_id=DEMO_HEALTH_SYSTEM_ID,
        slug=DEMO_HEALTH_SYSTEM_SLUG,
        name="Cedars-Sinai Surgical Care",
        health_system_code=DEMO_CLINIC_CODE,
    )
    rows = _seed_demo_patient_store()
    _load_demo_patient_store_snapshot()
    _seed_demo_sqlite(rows, _demo_seed_strategy())


def _ensure_triage_demo_tenant_seeded() -> None:
    """Merge TRIAGEDM in-memory patients + SQLite rows on every startup.

    Tenant staff sign-in works without ``DEMO_MODE``, but the roster reads
    ``_patient_store``; seeding triage only inside ``_seed_demo_mode_data`` left
    an empty roster when ``DEMO_MODE`` was off.
    """
    ensure_triage_demo_staff(_team_store)
    # Resolve preserve→reset once (stale anchor / blueprint change) so the
    # in-memory merge and the SQLite seed agree on the episode timeline.
    strategy = effective_seed_strategy(_team_store, _demo_seed_strategy())
    merge_triage_patients_into_store(
        _patient_store,
        battlecard_fn=_build_demo_battlecard,
        team_store=_team_store,
        strategy=strategy,
    )
    seed_triage_demo_sqlite(_team_store, _patient_store, strategy=strategy)


_triage_demo_reseed_lock = threading.Lock()


def _refresh_triage_demo_seed_if_needed() -> None:
    """Self-heal the TRIAGEDM demo at sign-in time.

    Seeding only runs at process startup, so a server that stays up past
    ``DEMO_SEED_MAX_AGE_DAYS`` keeps serving a drifted demo timeline (and an
    instance that lost in-memory demo patients serves an empty roster) until
    someone restarts it. Called on demo-tenant staff login: a cheap freshness
    probe on every login, full reseed only when patients are missing from the
    in-memory store or the stored seed went stale.
    """
    if not _triage_demo_enabled():
        return
    with _triage_demo_reseed_lock:
        missing = any(pid not in _patient_store for pid in triage_demo_patient_ids())
        if not missing and effective_seed_strategy(_team_store, "preserve") == "preserve":
            return
        _ensure_triage_demo_tenant_seeded()


def _frontend_cache_version() -> str:
    """Version from frontend asset mtimes so patient dashboard always loads latest CSS/JS (no stale cache)."""
    base = os.path.join(os.path.dirname(__file__), "..", "frontend")
    paths = [os.path.join(base, "styles.css"), os.path.join(base, "app.js")]
    try:
        mtimes = [os.path.getmtime(p) for p in paths if os.path.isfile(p)]
        return str(int(max(mtimes))) if mtimes else ""
    except OSError:
        return ""


def _apply_cache_bust(html: str) -> str:
    v = _frontend_cache_version()
    if not v:
        return html
    html = html.replace('href="/static/styles.css"', f'href="/static/styles.css?v={v}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={v}"')
    return html


def _build_recovery_resources_email_html(
    *,
    first_name: str,
    clinic_code: str,
    resource_code: str,
    recovery_plan_entry_url: str,
    hero_image_src: str,
    logo_image_src: str,
    is_preop: bool = False,
) -> str:
    """
    Build a clean, image-free resources email body.
    Keeps broad email-client compatibility (Gmail/Outlook/Apple Mail).
    Wording adapts to pre-op (surgery preparation) vs post-op (recovery).
    """
    first_name_safe = html_lib.escape(first_name or "Patient")
    clinic_code_safe = html_lib.escape(clinic_code or "N/A")
    resource_code_safe = html_lib.escape(resource_code or "N/A")
    recovery_url_safe = html_lib.escape(recovery_plan_entry_url or "#", quote=True)
    _ = hero_image_src
    _ = logo_image_src

    if is_preop:
        header_sub = "Your surgery preparation resources are ready"
        intro_line = "Your care team has prepared personalized surgery preparation resources for you, including voice explanations and quick reference guides."
        codes_hint = "Use these codes to open your personalized preparation dashboard"
        cta_label = "View Your Preparation Plan"
        tip_line = "Save these codes somewhere safe. You can re-open your resources anytime before your surgery."
    else:
        header_sub = "Your recovery resources are ready"
        intro_line = "Your care team has prepared personalized recovery resources for you, including voice explanations and quick reference guides."
        codes_hint = "Use these codes to open your personalized recovery dashboard"
        cta_label = "View Your Recovery Plan"
        tip_line = "Save these codes somewhere safe. You can re-open your resources anytime during recovery."

    return f"""
    <div style="margin:0;padding:0;background:#f3f6f9;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f3f6f9;">
        <tr>
          <td align="center" style="padding:24px 12px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="680" style="width:680px;max-width:680px;background:#ffffff;border:1px solid #dbe5ec;border-radius:14px;overflow:hidden;margin:0 auto;">
              <tr>
                <td style="padding:0;background:#0891b2;">
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                    <tr>
                      <td style="padding:28px 24px 24px 24px;text-align:center;">
                        <div style="font-family:Georgia,serif;color:#ffffff;font-size:36px;line-height:1.15;font-weight:700;">
                          Archangel Health
                        </div>
                        <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#e0f2fe;font-size:14px;line-height:1.6;margin-top:8px;">
                          {header_sub}
                        </div>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td style="padding:28px 28px 8px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;font-size:20px;line-height:1.5;font-weight:600;">
                  Hi <span style="font-weight:700;color:#111827;">{first_name_safe}</span>,
                </td>
              </tr>
              <tr>
                <td style="padding:0 28px 22px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#334155;font-size:16px;line-height:1.75;">
                  {intro_line}
                </td>
              </tr>
              <tr>
                <td style="padding:0 28px;">
                  <div style="height:1px;background:#e2e8f0;"></div>
                </td>
              </tr>

              <tr>
                <td align="center" style="padding:22px 28px 8px 28px;">
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0891b2;font-size:12px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">Your Access Codes</div>
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#64748b;font-size:13px;line-height:1.6;margin-top:6px;">{codes_hint}</div>
                </td>
              </tr>

              <tr>
                <td style="padding:10px 28px 0 28px;">
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#64748b;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;text-align:center;margin-bottom:10px;">Health System Code</div>
                  <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px;padding:18px;text-align:center;">
                    <div style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,'Courier New',monospace;color:#111827;font-size:34px;font-weight:800;letter-spacing:0.15em;line-height:1.2;">{clinic_code_safe}</div>
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:16px 28px 0 28px;">
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#64748b;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;text-align:center;margin-bottom:10px;">Resource Code</div>
                  <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px;padding:18px;text-align:center;">
                    <div style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,'Courier New',monospace;color:#111827;font-size:34px;font-weight:800;letter-spacing:0.15em;line-height:1.2;">{resource_code_safe}</div>
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:22px 28px 14px 28px;">
                  <a href="{recovery_url_safe}" style="display:block;text-decoration:none;text-align:center;background:#0891b2;color:#ffffff;border:1px solid #0e7490;border-radius:12px;padding:15px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:17px;font-weight:700;line-height:1.2;">
                    {cta_label}
                  </a>
                </td>
              </tr>

              <tr>
                <td style="padding:0 28px 22px 28px;">
                  <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:12px;padding:13px 14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#334155;font-size:13px;line-height:1.65;text-align:center;">
                    <strong style="color:#0e7490;">Tip:</strong> {tip_line}
                  </div>
                </td>
              </tr>

              <tr>
                <td style="border-top:1px solid #e2e8f0;padding:16px 28px 6px 28px;text-align:center;">
                  <div style="font-family:Georgia,serif;color:#0f172a;font-size:16px;font-weight:700;">Archangel Health</div>
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#64748b;font-size:12px;padding-top:4px;">Your personalized healthcare companion</div>
                </td>
              </tr>
            </table>
            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#64748b;font-size:12px;line-height:1.5;text-align:center;padding-top:14px;">
              Questions? Reply to this email or contact your care team.
            </div>
          </td>
        </tr>
      </table>
    </div>
    """


def _is_public_http_url(url: str) -> bool:
    if not url:
        return False
    s = url.strip().lower()
    return (
        (s.startswith("http://") or s.startswith("https://"))
        and "localhost" not in s
        and "127.0.0.1" not in s
        and "0.0.0.0" not in s
    )


def _email_asset_base_url() -> str:
    candidate = (
        os.getenv("EMAIL_PUBLIC_ASSET_BASE_URL")
        or os.getenv("BASE_URL")
        or "https://archangelhealth.ai"
    ).strip().rstrip("/")
    return candidate if _is_public_http_url(candidate) else "https://archangelhealth.ai"


def _minify_email_html(html: str) -> str:
    # Keep textual spaces intact but remove indentation/newlines between tags.
    out = re.sub(r">\s+<", "><", html)
    return out.strip()


def _render_recovery_email_html(
    *,
    first_name: str,
    clinic_code: str,
    resource_code: str,
    recovery_plan_entry_url: str,
    use_local_preview_assets: bool = False,
    is_preop: bool = False,
) -> str:
    _ = use_local_preview_assets

    html_body = _build_recovery_resources_email_html(
        first_name=first_name,
        clinic_code=clinic_code,
        resource_code=resource_code,
        recovery_plan_entry_url=recovery_plan_entry_url,
        hero_image_src="",
        logo_image_src="",
        is_preop=is_preop,
    )
    return _minify_email_html(html_body)


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _episode_day_from_open(open_date_str: str, ts_iso: str) -> Optional[int]:
    try:
        open_dt = date.fromisoformat(open_date_str)
        event_date = datetime.fromisoformat(ts_iso.replace("Z", "")).date()
        return (event_date - open_dt).days + 1
    except Exception:
        return None


# Internal post-op cron/audit rows — hidden from the doctor episode timeline.
# 30-Day Calendar shows ONLY patient-facing engagement events. Internal pipeline
# events (llm_call, grounding_check, retier, daily-checkin internals, etc.) are
# never surfaced here. Survey completed/pending markers are added separately.
_TIMELINE_VISIBLE_EVENT_TYPES = frozenset({
    "platform_opened",            # 🟢 Platform Opened
    "diagnosis_video_watched",    # 🔵 Discharge Video Watched
    "treatment_video_watched",    # 🔵 Discharge Video Watched
    "sms_sent",                   # 📩 SMS Sent
})


def _timeline_event_visible(event_type: str) -> bool:
    return str(event_type or "") in _TIMELINE_VISIBLE_EVENT_TYPES


def _question_set_for_day(survey_day: int) -> dict:
    if survey_day == 7:
        return {"questions": DAY_7_QUESTIONS, "options": DAY_7_OPTIONS}
    if survey_day == 14:
        return {"questions": DAY_14_QUESTIONS, "options": DAY_14_30_OPTIONS}
    if survey_day == 30:
        return {"questions": DAY_30_QUESTIONS, "options": DAY_14_30_OPTIONS}
    raise HTTPException(status_code=400, detail="Unsupported survey day. Use 7, 14, or 30.")


def _score_survey_answers(answers: List[dict]) -> dict:
    score_map = {
        "Very Clear": 100,
        "Strongly Agree": 100,
        "Somewhat Clear": 50,
        "Agree": 50,
        "Not Clear": 0,
        "Disagree": 0,
        "Strongly Disagree": 0,
    }
    applicable = []
    for ans in answers:
        response = (ans.get("response") or "").strip()
        if response == "Does not apply":
            continue
        applicable.append(score_map.get(response, 0))
    score = None if not applicable else round(sum(applicable) / len(applicable), 2)
    if score is None:
        tier = None
    elif score >= 80:
        tier = "green"
    elif score >= 60:
        tier = "yellow"
    elif score >= 40:
        tier = "orange"
    else:
        tier = "red"
    return {"score": score, "tier": tier}


def _survey_link_for_patient(patient: dict, survey_day: int) -> str:
    base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    query = urlencode(
        {
            "clinic_code": (patient.get("clinic_code") or ""),
            "resource_code": (patient.get("resource_code") or ""),
            "day": survey_day,
        }
    )
    return f"{base_url}/survey?{query}"


def _detect_hard_tier_1(message: str) -> bool:
    msg = (message or "").lower()
    return any(phrase in msg for phrase in HARD_TIER_1_PHRASES)


def _heuristic_semantic_escalation(message: str) -> Optional[dict]:
    msg = (message or "").lower()
    tier2_signals = [
        "wound drainage",
        "foul smell",
        "stitches opening",
        "staples separating",
        "fever",
        "101",
        "101.5",
        "new numbness",
        "new weakness",
        "foot drop",
        "swollen calf",
        "can't keep medication",
        "haven't taken",
    ]
    tier3_signals = [
        "overwhelmed",
        "really scared",
        "something is really wrong",
        "this doesn't feel right",
        "don't understand",
        "confused",
        "no support",
        "hopeless",
        "urgent care",
        "er",
        "new medication",
    ]
    if any(s in msg for s in tier2_signals):
        return {"tier": 2, "trigger_type": "semantic"}
    if any(s in msg for s in tier3_signals):
        return {"tier": 3, "trigger_type": "semantic"}
    return None


async def _evaluate_semantic_escalation_llm(message: str, conversation_history: List[dict]) -> Optional[dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _heuristic_semantic_escalation(message)
    try:
        convo = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in (conversation_history or [])]
        convo.append({"role": "user", "content": message})
        resp, _ = call_llm_sync(
            role="escalation_classifier",
            prompt_id="semantic_escalation",
            patient_id=None,
            system=SEMANTIC_ESCALATION_PROMPT,
            messages=convo,
        )
        text = first_text(resp).strip()
        parsed = json.loads(text)
        tier = int(parsed.get("tier", 0))
        if tier in (2, 3):
            return {"tier": tier, "trigger_type": "semantic", "reason": parsed.get("reason", "")}
    except Exception:
        return _heuristic_semantic_escalation(message)
    return None


def _load_json_file(path: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if os.path.isfile(path):
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return json.loads(json.dumps(fallback))


def _save_json_file(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def _load_form_library() -> Dict[str, Any]:
    return _load_json_file(SPECIALTY_FORM_LIBRARY_PATH, DEFAULT_FORM_LIBRARY)


def _save_form_library(library: Dict[str, Any]) -> None:
    _save_json_file(SPECIALTY_FORM_LIBRARY_PATH, library)


def _load_frameworks() -> Dict[str, Any]:
    return _load_json_file(SPECIALTY_FRAMEWORK_PATH, DEFAULT_FRAMEWORKS)


def _save_frameworks(frameworks: Dict[str, Any]) -> None:
    _save_json_file(SPECIALTY_FRAMEWORK_PATH, frameworks)


def _specialty_from_procedure(procedure_name: str) -> str:
    proc = (procedure_name or "").lower()
    if any(k in proc for k in ("spine", "lumbar", "cervical", "fusion")):
        return "Spine"
    if any(k in proc for k in ("cabg", "cardiac", "coronary", "heart")):
        return "Cardiac"
    if any(k in proc for k in ("knee", "hip", "orthopedic", "arthro", "joint")):
        return "Orthopedic"
    return "General Surgery"


def _blank_form_from_template(patient: dict, template: Dict[str, Any]) -> Dict[str, Any]:
    sd = patient.get("structured_data") or {}
    header = {
        "Patient Name": patient.get("name", ""),
        "DOB": sd.get("date_of_birth", ""),
        "MRN": sd.get("mrn", ""),
        "Procedure": sd.get("procedure_name", ""),
        "Surgeon": sd.get("surgeon_name", ""),
        "Surgery Date": sd.get("procedure_date", ""),
    }
    sections = {}
    for section_name, items in (template.get("sections") or {}).items():
        sections[section_name] = [{"label": item, "status": "N/A", "comments": ""} for item in (items or [])]
    final_review = {field: "" for field in (template.get("final_review_fields") or [])}
    return {"header": header, "sections": sections, "finalReview": final_review}


def _build_prefill_from_session(patient: dict, session: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    form = _blank_form_from_template(patient, template)
    answers = session.get("answers", {})
    pattern = answers.get("pattern", "")
    exposure = answers.get("exposure", "")
    anatomy = answers.get("anatomy", "")
    root = answers.get("root", "")
    for sec_items in form["sections"].values():
        for item in sec_items:
            item["status"] = "Yes"
            item["comments"] = f"Auto-filled from intake: {pattern[:65]}" if pattern else "Auto-filled from intake interview."
    form["finalReview"]["Questions from patient / items needing follow-up"] = anatomy or "Needs follow-up clarification."
    form["finalReview"]["Additional instructions given"] = exposure or "Reviewed preparation instructions."
    form["finalReview"]["Reviewed by (staff name / role)"] = "Digital Care Companion"
    form["finalReview"]["Date"] = date.today().isoformat()
    form["finalReview"]["Patient initials confirming review"] = (patient.get("name", "P")[:2]).upper()
    form["finalReview"]["Patient signature"] = patient.get("name", "")
    if root:
        form["header"]["MRN"] = form["header"].get("MRN") or "Pending"
    return {
        "header": form["header"],
        "preOpTesting": form["sections"].get("Pre-Op Testing Acknowledgment", []),
        "medicationInstructions": form["sections"].get("Medication Instructions Acknowledged", []),
        "dayOfSurgery": form["sections"].get("Day-of-Surgery Prep", []),
        "homePreparation": form["sections"].get("Home Preparation Confirmed", []),
        "consentForms": form["sections"].get("Consent Forms", []),
        "finalReview": form["finalReview"],
        "templateName": template.get("template_name", "Pre-Op Intake Form"),
    }


def _care_message_for_tier(tier: int, patient_data: dict) -> str:
    if tier == 1:
        return TIER_1_RESPONSE
    if tier == 2:
        doctor_phone = patient_data.get("office_phone") or os.getenv("CARE_TEAM_PHONE", "")
        return (
            "This is something your surgeon needs to hear about today. "
            f"Please call {doctor_phone or 'your surgeon office'} as soon as possible. "
            "I'm flagging this for your care team now."
        )
    return (
        "I want to make sure you get the right support for this. "
        "I'm going to let your care navigator know so they can follow up with you directly. Is that okay?"
    )


async def _classify_and_create_escalation(
    *,
    patient_id: str,
    message: str,
    conversation_history: List[dict],
    source: str,
) -> Optional[Dict[str, Any]]:
    patient_data = _patient_store[patient_id]
    snapshot = list(conversation_history or []) + [{"role": "user", "content": message}]
    if source != "chat":
        snapshot.append({"role": "system", "content": f"Origin: {source}"})

    tier = None
    trigger_type = source
    semantic_verdict: Optional[Dict[str, Any]] = None
    if _detect_hard_tier_1(message):
        tier = 1
        trigger_type = f"{source}:hard_keyword"
    else:
        semantic = await _evaluate_semantic_escalation_llm(message, conversation_history)
        if semantic and semantic.get("tier") in (2, 3):
            tier = semantic.get("tier")
            trigger_type = f"{source}:{semantic.get('trigger_type', 'semantic')}"
            semantic_verdict = semantic
    if tier is None:
        return None
    esc_id = _team_store.create_escalation(
        patient_id=patient_id,
        tier=int(tier),
        trigger_type=trigger_type,
        message=message,
        conversation_snapshot=snapshot,
        health_system_id=patient_data.get("health_system_id"),
    )

    # Triage Suite Pass 3 §3.1 — when the verdict came from the LLM
    # semantic-escalation path AND the source was the Care Companion
    # chat, persist a `care_companion_semantic_escalation` row in the
    # event log so the post-op re-tier algorithm can read it as a soft
    # contributor (tier-2) or hard escalator (tier-3) on its next run.
    if semantic_verdict is not None and source == "chat":
        try:
            _team_store.log_event(
                patient_id=patient_id,
                event_type="care_companion_semantic_escalation",
                payload={
                    "tier": int(semantic_verdict.get("tier") or 0),
                    "reason": semantic_verdict.get("reason", ""),
                    "trigger_type": trigger_type,
                    "escalation_id": esc_id,
                    "message_excerpt": (message or "")[:500],
                },
            )
        except Exception:
            # Audit failure must never block the reply.
            pass
    return {
        "tier": int(tier),
        "escalation_id": esc_id,
        "requires_consent": int(tier) == 3 and source == "chat",
        "response": _care_message_for_tier(int(tier), patient_data),
    }


# ─── Patient store starts empty (no demo seed) ─────────────────

# ─── Request / Response Models ────────────────────────────────
class EHRBundle(BaseModel):
    patient_id:         str
    patient_name:       str
    phone_number:       str
    pmh:                str
    procedure_context:  str
    after_visit_summary: str
    clinical_notes:     str
    medication_list:    str
    allergies:          str
    problem_list:       str

class DischargeInput(BaseModel):
    patient_name:       str
    discharge_notes:    str
    patient_id:         Optional[str] = None
    phone_number:       Optional[str] = None
    email:              Optional[str] = None
    doctor_office_phone: Optional[str] = None
    doctor_clinic_code: Optional[str] = None
    resource_code: Optional[str] = None

class ResourceSet(BaseModel):
    voice_script:       str
    battlecard_html:    str
    voice_audio_url:    Optional[str] = None

class DischargeResponse(BaseModel):
    patient_id:         str
    dashboard_url:      str
    diagnosis:          ResourceSet
    treatment:          ResourceSet
    structured_data:    dict

class ProcessResponse(BaseModel):
    patient_id:       str
    pipeline_type:    str
    dashboard_url:    str
    voice_audio_url:  Optional[str]
    battlecard_html:  str
    avatar_url:       Optional[str]

class ChatRequest(BaseModel):
    patient_id:           str
    message:              str
    conversation_history: List[dict] = []

class ChatResponse(BaseModel):
    response:   str
    patient_id: str
    audio_url:  Optional[str] = None
    escalation: Optional[dict] = None


class EventTrackRequest(BaseModel):
    event_type: str
    payload: Optional[dict] = None


class PCPReferralUpdateRequest(BaseModel):
    pcp_name: Optional[str] = None
    sent: bool = True


class PatientUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    mbi: Optional[str] = None
    dob: Optional[str] = None
    scheduled_surgery_date: Optional[str] = None
    anchor_procedure: Optional[str] = None


_ANCHOR_PROCEDURES = frozenset({"LEJR", "HIP_FEMUR", "SPINAL_FUSION", "CABG", "MAJOR_BOWEL"})
_MBI_RE = re.compile(r"^[1-9][A-Z][A-Z0-9][0-9][A-Z][A-Z0-9][0-9][A-Z][A-Z][0-9]{2}$")


class SurveySubmitRequest(BaseModel):
    clinic_code: str
    resource_code: str
    day: int
    answers: List[dict]


class PreOpSurveySubmitBody(BaseModel):
    patient_id: str
    window: str
    answers: List[dict]


class PreOpWindowActionBody(BaseModel):
    action: str


class EscalationResolveRequest(BaseModel):
    resolved: bool


class EscalationInterventionRequest(BaseModel):
    message: str


class EscalationConsentRequest(BaseModel):
    escalation_id: int
    consent: str


class PreOpInput(BaseModel):
    patient_name: str
    preparation_notes: str
    patient_id: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    doctor_office_phone: Optional[str] = None
    doctor_clinic_code: Optional[str] = None
    resource_code: Optional[str] = None
    procedure_type: Optional[str] = None
    scheduled_surgery_date: Optional[str] = None


class IntakeStartRequest(BaseModel):
    patient_id: str


class IntakeAnswerRequest(BaseModel):
    patient_id: str
    message: str
    conversation_history: List[dict] = []


class IntakeSubmitRequest(BaseModel):
    patient_id: str
    form_data: Dict[str, Any]


class CareTeamNotificationRequest(BaseModel):
    patient_id: str
    message: str


class IntakeFormsStartInterviewBody(BaseModel):
    patientId: str
    surgeryId: Optional[str] = None


class IntakeFormsCompleteInterviewBody(BaseModel):
    transcript: List[Dict[str, Any]]
    duration: Optional[int] = None
    audioBlobUrl: Optional[str] = None


class IntakeFormsPatchBody(BaseModel):
    section: str
    field: str
    value: Any


class IntakeSectionMessageBody(BaseModel):
    section: int
    message: str = ""
    conversationHistory: List[Dict[str, Any]] = []


class IntakeCompleteSectionBody(BaseModel):
    section: int
    """For section 11, pass acknowledgement field values."""
    acknowledgements: Optional[Dict[str, Any]] = None
    forceComplete: bool = False
    """Patient finished reviewing the form for this section (after interview or pre-filled form)."""
    confirmReview: bool = False


class IntakeResetSectionBody(BaseModel):
    section: int


class IntakeFormsSubmitBody(BaseModel):
    pass


# ─── Doctor portal token handoff ──────────────────────────────
_PORTAL_HANDOFF_TTL_SECONDS = 60
_PORTAL_HANDOFF_STORE: Dict[str, Dict[str, Any]] = {}


def _cleanup_portal_handoffs(now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    expired = [k for k, v in _PORTAL_HANDOFF_STORE.items() if v.get("expires_at") and v["expires_at"] <= now]
    for key in expired:
        _PORTAL_HANDOFF_STORE.pop(key, None)


class PortalHandoffCreateResponse(BaseModel):
    handoff_code: str
    expires_in_seconds: int


class PortalHandoffConsumeRequest(BaseModel):
    handoff_code: str


# ─── Auth (Elysium Health landing) ────────────────────────────
@app.post("/api/auth/register", dependencies=[Depends(rate_limiter("auth_register", 10, 60))])
async def auth_register(body: UserCreate):
    """Register a new user; returns access token and user."""
    try:
        user = register_user(body.email, body.password, body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = create_access_token(user["email"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserOut(email=user["email"], name=user.get("name"), role=user.get("role")),
    }


def _require_staff_mfa() -> bool:
    return os.getenv("REQUIRE_STAFF_MFA", "0").strip().lower() in ("1", "true", "yes", "on")


def _issue_login_response(user: dict) -> dict:
    """Return the access-token payload, or an `mfa_required` challenge when the
    account has TOTP enabled (PRD-3, opt-in)."""
    email = user["email"]
    mfa_on = auth_module.user_mfa_enabled(email)
    if _require_staff_mfa() and not mfa_on:
        raise HTTPException(
            status_code=403,
            detail="MFA enrollment required. Set up an authenticator app to continue.",
        )
    if mfa_on:
        return {"mfa_required": True, "mfa_token": auth_module.create_mfa_pending_token(email)}
    token = create_access_token(email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserOut(email=email, name=user.get("name"), role=user.get("role")),
    }


@app.post("/api/auth/login", dependencies=[Depends(rate_limiter("auth_login", 10, 60))])
async def auth_login(body: UserLogin):
    """Sign in; returns access token and user."""
    # Shared public demo account (marketing landing): always authenticate here first so
    # production isn't blocked when DEMO_MODE=0 (no auth user seed) or the same email
    # exists in team_members (tenant SSO path would otherwise return 403 before password check).
    demo_key = DEMO_DOCTOR_EMAIL.lower().strip()
    if body.email.lower().strip() == demo_key:
        user = authenticate_user(body.email, body.password)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        return _issue_login_response(user)
    tm = _team_store.find_team_member_by_email_any_hs(body.email)
    if tm:
        hs = _team_store.get_health_system_by_id(tm.get("health_system_id") or "")
        slug = (hs or {}).get("slug") or "your-workspace"
        landing = (os.getenv("LANDING_URL") or "http://localhost:5173").strip().rstrip("/")
        tenant_url = f"{landing}/t/{slug}/sign-in"
        raise HTTPException(
            status_code=403,
            detail=(
                "This account signs in through your health system workspace, not the public demo site. "
                f"Use: {tenant_url}"
            ),
        )
    user = authenticate_user(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return _issue_login_response(user)


class MfaCodeBody(BaseModel):
    code: str


class MfaLoginBody(BaseModel):
    mfa_token: str
    code: str


@app.post("/api/auth/mfa/enroll")
async def mfa_enroll(user: UserOut = Depends(get_current_user)):
    """Begin TOTP enrollment: returns the secret + otpauth URI to render a QR.
    Not active until confirmed via /api/auth/mfa/verify."""
    secret, uri = auth_module.mfa_begin_enrollment(user.email)
    return {"secret": secret, "otpauth_uri": uri, "issuer": auth_module.MFA_ISSUER}


@app.post("/api/auth/mfa/verify")
async def mfa_verify_enroll(body: MfaCodeBody, user: UserOut = Depends(get_current_user)):
    if not auth_module.mfa_confirm_enrollment(user.email, body.code):
        raise HTTPException(status_code=400, detail="Invalid code. Please try again.")
    return {"ok": True, "mfa_enabled": True}


@app.post("/api/auth/mfa/disable")
async def mfa_disable_endpoint(body: MfaCodeBody, user: UserOut = Depends(get_current_user)):
    if not auth_module.mfa_disable(user.email, body.code):
        raise HTTPException(status_code=400, detail="Invalid code.")
    return {"ok": True, "mfa_enabled": False}


@app.get("/api/auth/mfa/status")
async def mfa_status(user: UserOut = Depends(get_current_user)):
    return {"mfa_enabled": auth_module.user_mfa_enabled(user.email)}


@app.post("/api/auth/mfa/login", dependencies=[Depends(rate_limiter("mfa_login", 10, 60))])
async def mfa_login(body: MfaLoginBody):
    """Second step of login: exchange the mfa_token + TOTP code for an access token."""
    email = auth_module.decode_mfa_pending_token(body.mfa_token)
    if not email:
        raise HTTPException(status_code=401, detail="MFA session expired. Please sign in again.")
    if not auth_module.mfa_verify(email, body.code):
        raise HTTPException(status_code=401, detail="Invalid authentication code.")
    u = auth_module._get_users().get(email) or {}  # noqa: SLF001
    token = create_access_token(email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserOut(email=email, name=u.get("name"), role=u.get("role")),
    }


@app.post("/api/auth/logout")
async def auth_logout(authorization: Optional[str] = Header(None)):
    """Revoke the presented staff token (landing or tenant) so it can't be reused (PRD-3)."""
    from token_revocation import revoke_token

    if authorization and authorization.startswith("Bearer "):
        revoke_token(authorization.removeprefix("Bearer ").strip())
    return {"ok": True}


@app.post("/api/auth/portal-handoff", response_model=PortalHandoffCreateResponse)
async def create_portal_handoff(
    authorization: Optional[str] = Header(None),
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if not staff:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    _cleanup_portal_handoffs()
    token = authorization.removeprefix("Bearer ").strip()
    code = secrets.token_urlsafe(24)
    _PORTAL_HANDOFF_STORE[code] = {
        "access_token": token,
        "expires_at": datetime.utcnow() + timedelta(seconds=_PORTAL_HANDOFF_TTL_SECONDS),
        "created_at": datetime.utcnow(),
    }
    return PortalHandoffCreateResponse(
        handoff_code=code,
        expires_in_seconds=_PORTAL_HANDOFF_TTL_SECONDS,
    )


@app.post("/api/auth/portal-handoff/consume")
async def consume_portal_handoff(body: PortalHandoffConsumeRequest):
    _cleanup_portal_handoffs()
    code = (body.handoff_code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="handoff_code is required")
    payload = _PORTAL_HANDOFF_STORE.pop(code, None)
    if not payload:
        raise HTTPException(status_code=404, detail="Handoff code not found or expired")
    return {"access_token": payload["access_token"], "token_type": "bearer"}


@app.get("/api/auth/me", response_model=UserOut)
async def auth_me(user: UserOut = Depends(get_current_user)):
    """Return current user from Bearer token."""
    return user


@app.get("/auth/signout")
async def auth_signout():
    """Redirect to landing page with signout=1 so landing can clear auth state."""
    landing_url = (os.getenv("LANDING_URL") or "http://localhost:5173").strip().rstrip("/")
    signout_url = f"{landing_url}?signout=1"
    return RedirectResponse(url=signout_url, status_code=302)


def _generate_resource_code() -> str:
    """Generate a random alphanumeric resource code (8 chars)."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ─── Doctor profile & onboarding ──────────────────────────────
@app.get("/api/doctor/profile", response_model=DoctorProfileOut)
async def doctor_profile(
    authorization: Optional[str] = Header(None),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    """Return current doctor's profile (landing auth) or tenant staff profile."""
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    td = decode_tenant_staff_token(token) if token else None
    if td:
        hs = _team_store.get_health_system_by_id(td.get("tid") or "")
        if not hs:
            raise HTTPException(status_code=404, detail="Health system not found.")
        from staff_context import _normalize_legacy_role  # local import: avoid cycle
        role = _normalize_legacy_role(td.get("role"))
        # Director-ness drives the audit-log tab and other surgeon-only affordances.
        # Pass-4 tokens carry `itd`; legacy `role: "director"` still maps to True.
        is_director = bool(td.get("itd")) or (
            (td.get("role") or "").strip().lower() == "director"
        )
        if role == "surgeon":
            dtype = "Director of TEAM Initiative" if is_director else "Surgeon"
        elif role == "rn_coordinator":
            dtype = "RN Care Coordinator"
        elif role == "np_pa":
            dtype = "NP / PA"
        else:
            dtype = "Care Team"
        code = hs.get("health_system_code") or ""
        return DoctorProfileOut(
            name=td.get("name") or "",
            email=td.get("sub") or "",
            office_phone=hs.get("phone") or "",
            doctor_type=dtype,
            hospital_affiliations=hs.get("name") or "",
            clinic_code=code,
            health_system_code=code,
            tenant_slug=hs.get("slug"),
            is_team_director=is_director,
            role=role,
        )
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    profile = get_doctor_profile(user.email)
    if not profile:
        raise HTTPException(
            status_code=404,
            detail="Doctor profile not found. Complete onboarding first.",
        )
    profile.setdefault("health_system_code", profile.get("clinic_code") or "")
    profile.setdefault("tenant_slug", None)
    profile.setdefault("is_team_director", False)
    profile.setdefault("role", "surgeon")
    return DoctorProfileOut(**profile)


@app.post("/api/doctor/onboard", response_model=DoctorProfileOut)
async def doctor_onboard(body: DoctorOnboard, user: UserOut = Depends(get_current_user)):
    """Set doctor profile and generate health system code. Email must match current user."""
    if user.email.lower() != body.email.lower():
        raise HTTPException(status_code=400, detail="Email must match your account.")
    try:
        profile = set_doctor_profile(
            user.email,
            name=body.name,
            office_phone=body.office_phone,
            doctor_type=body.doctor_type,
            hospital_affiliations=body.hospital_affiliations,
        )
        profile.setdefault("health_system_code", profile.get("clinic_code") or "")
        return DoctorProfileOut(**profile)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Patient access by codes (for landing code-entry form) ───────
@app.get("/api/patient/by-codes", dependencies=[Depends(rate_limiter("by_codes", 10, 60))])
async def patient_by_codes(
    clinic_code: Optional[str] = None,
    resource_code: str = "",
    health_system_code: Optional[str] = None,
):
    """Resolve health system (clinic) code + resource_code to patient_id and dashboard URL."""
    cc = (health_system_code or clinic_code or "").strip().upper()
    resource_code = (resource_code or "").strip().upper()
    if not cc or not resource_code:
        raise HTTPException(status_code=400, detail="Health system code and resource code are required.")
    for pid, d in _patient_store.items():
        if (d.get("clinic_code") or "").upper() == cc and (d.get("resource_code") or "").upper() == resource_code:
            base_url = os.getenv("BASE_URL", "http://localhost:8000")
            _team_store.ensure_episode(
                patient_id=pid,
                procedure_type=(d.get("structured_data") or {}).get("procedure_name", ""),
                clinic_code=d.get("clinic_code") or "",
                resource_code=d.get("resource_code") or "",
                health_system_id=d.get("health_system_id"),
            )
            # Explicitly mark successful code-based platform entry.
            _team_store.log_event(patient_id=pid, event_type="platform_opened", payload={"clinic_code": cc})
            is_preop = (d.get("pipeline_type") or "").lower() == "pre_op"
            dashboard_path = f"/patient/{pid}/pre-op" if is_preop else f"/patient/{pid}"
            # Mint a one-time entry token; the page route exchanges it for an
            # HttpOnly pt_session cookie (PRD-1). Cross-origin-safe: the token
            # rides into the backend origin on a first-party navigation.
            entry = create_entry_token(pid, d.get("health_system_id"))
            return {
                "patient_id": pid,
                "dashboard_url": f"{base_url}{dashboard_path}?k={entry}",
            }
    raise HTTPException(status_code=404, detail="No patient found for these codes. Check and try again.")


@app.post("/api/patient/logout")
async def patient_logout(request: Request, response: Response):
    """Clear and revoke the current patient session cookie (PRD-1 §10)."""
    tok = request.cookies.get("pt_session")
    if tok:
        revoke_patient_session(tok)
    clear_patient_session_cookie(response)
    return {"ok": True}


@app.get("/recovery", response_class=HTMLResponse)
@app.get("/care-plan", response_class=HTMLResponse)
async def patient_code_entry(hs: Optional[str] = None):
    """Self-contained patient code-entry page. Used as the entry point when the
    landing app isn't configured (LANDING_URL unset) so patient SMS/email links
    always reach a real code-entry experience instead of a bare dashboard URL.
    Optional ?hs= pre-fills the (non-sensitive) health-system code."""
    return HTMLResponse(content=_render_patient_entry_page(prefill_hs=hs or ""))


async def _maybe_trigger_preop_outreach(app: FastAPI) -> None:
    now_m = time.monotonic()
    last = getattr(app.state, "last_preop_outreach_mono", 0.0)
    if now_m - last < 900:
        return
    app.state.last_preop_outreach_mono = now_m

    async def _run() -> None:
        try:
            await _run_preop_survey_outreach()
        except Exception as e:
            print(f"[preop-outreach] {e}")

    # Fire-and-forget: the outreach pass can hit email/SMS providers and loop
    # the whole patient store — running it inline made the first roster load
    # after a quiet period (i.e. right after sign-in) hang on it.
    task = asyncio.create_task(_run())
    app.state.preop_outreach_inline_task = task


async def _run_preop_survey_outreach() -> None:
    base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    now_dt = datetime.utcnow()
    for pid, d in _patient_store.items():
        if (d.get("pipeline_type") or "").lower() != "pre_op":
            continue
        sd = d.get("structured_data") or {}
        surgery = parse_surgery_datetime(sd.get("procedure_date") or "")
        if not surgery:
            continue
        email = (d.get("email") or "").strip()
        phone_raw = (d.get("phone") or "").strip()
        h = hours_until_surgery(surgery, now_dt)
        for window in ("t96", "t48", "t24"):
            if survey_window_state(window, h) != "open":
                continue
            survey_day = WINDOW_SURVEY_DAY[window]
            if _team_store.has_survey_send(pid, survey_day):
                continue
            link = f"{base}/static/preop-survey.html?window={window}&patient={pid}"
            label = {"t96": "T-96h", "t48": "T-48h", "t24": "T-24h"}[window]
            html_body = (
                f"<p>Your pre-operative readiness survey ({label}) is ready.</p>"
                f"<p><a href='{html_lib.escape(link, quote=True)}'>Complete survey</a></p>"
                "<p>This helps your care team keep your surgery on track.</p>"
            )
            delivered = False
            if email and is_email_transport_configured():
                delivered = bool(
                    await _send_html_email(
                        email,
                        f"Pre-Op Readiness Survey ({label}) - Archangel Health",
                        html_body,
                    )
                )
            if not delivered and phone_raw:
                try:
                    body = f"Archangel Health: complete your pre-op survey ({label}): {link}"
                    TwilioClient().send(to=phone_raw, body=body[:1500])
                    delivered = True
                except Exception:
                    pass
            if delivered:
                _team_store.mark_survey_sent(pid, survey_day)
                _team_store.log_event(
                    patient_id=pid,
                    event_type="preop_survey_sent",
                    payload={"window": window, "survey_day": survey_day},
                )


# ─── Doctor Portal ────────────────────────────────────────────
@app.get("/")
async def doctor_portal_entry(request: Request):
    """Entry point: tenant sign-in first, then /doctor/app for the roster UI."""
    host = request.headers.get("host", "")
    if "admin." in host:
        return RedirectResponse(url="/admin", status_code=301)
    return RedirectResponse(url="/doctor/sign-in", status_code=302)


@app.get("/doctor/sign-in", response_class=HTMLResponse)
async def doctor_tenant_sign_in_page(request: Request):
    """Health-system tenant login (surgeon / RN); stores JWT in archangel_doctor_auth_token."""
    host = request.headers.get("host", "")
    if "admin." in host:
        return RedirectResponse(url="/admin", status_code=301)
    path = os.path.join(os.path.dirname(__file__), "../frontend/doctor-sign-in.html")
    with open(path) as f:
        return HTMLResponse(content=f.read())


@app.get("/doctor/app", response_class=HTMLResponse)
async def doctor_portal_app(request: Request):
    """Main doctor roster / console (after sign-in)."""
    host = request.headers.get("host", "")
    if "admin." in host:
        return RedirectResponse(url="/admin", status_code=301)
    html_path = os.path.join(os.path.dirname(__file__), "../frontend/doctor.html")
    with open(html_path) as f:
        return HTMLResponse(content=f.read())


@app.get("/doctor")
async def doctor_portal_legacy_path(request: Request):
    """Bookmarks to /doctor land on sign-in; dashboard is /doctor/app."""
    host = request.headers.get("host", "")
    if "admin." in host:
        return RedirectResponse(url="/admin", status_code=301)
    return RedirectResponse(url="/doctor/sign-in", status_code=307)


@app.get("/dev", response_class=HTMLResponse)
async def dev_login_shortcut():
    """Local-dev one-click sign-in. Gated on EMAIL_DEV_MODE so it never ships
    to prod. Auto-creates the dev doctor on first hit, issues a JWT, drops it
    into localStorage on the same origin, and redirects to the portal — so
    `python3 -m uvicorn main:app` + opening http://localhost:8000/dev is all
    you need to see the doctor UI."""
    if os.getenv("EMAIL_DEV_MODE") not in ("1", "true", "True"):
        raise HTTPException(status_code=404)

    email = "dev@local"
    password = "dev-password-not-secret"
    name = "Dev Doctor"
    if not get_doctor_profile(email):
        try:
            register_user(email, password, name)
        except ValueError:
            pass  # already exists from a prior run
        try:
            set_doctor_profile(
                email,
                name=name,
                office_phone="5555555555",
                doctor_type="Care Team",
                hospital_affiliations="Local Dev Hospital",
            )
        except Exception:
            pass

    token = create_access_token(email)
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Dev login</title></head>
<body><p>Signing in…</p>
<script>
  localStorage.setItem("archangel_doctor_auth_token", {json.dumps(token)});
  location.replace("/doctor/app");
</script>
</body></html>"""
    return HTMLResponse(content=html)


# Friendly labels for the per-rule TEAM eligibility verdicts. Used by the
# patient roster row to surface the FIRST failing rule as a hover tooltip on
# the "Not TEAM eligible" badge (e.g. "Enrolled in Medicare Advantage").
_TEAM_FAIL_LABELS = {
    "partA_active":     "Part A inactive",
    "partB_active":     "Part B inactive",
    "not_ma":           "Medicare Advantage",
    "medicare_primary": "Medicare not primary",
    "not_esrd_basis":   "ESRD-basis entitlement",
    "not_umwa":         "UMWA Health Plan",
}
# Display order matches PRD §6.4 — surface the highest-priority failure first.
_TEAM_FAIL_ORDER = [
    "not_ma",
    "not_esrd_basis",
    "not_umwa",
    "medicare_primary",
    "partA_active",
    "partB_active",
]


def _first_failing_rule_label(verdicts: Optional[Dict[str, Any]]) -> Optional[str]:
    if not verdicts:
        return None
    for key in _TEAM_FAIL_ORDER:
        if str(verdicts.get(key) or "").upper() == "FAIL":
            return _TEAM_FAIL_LABELS.get(key)
    return None


@app.get("/api/patients")
async def list_patients(
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Return all patients in the store for the doctor roster.

    Excludes draft patients (``is_draft=True``) — these are work-in-progress
    records created during the TEAM eligibility flow and are not yet committed
    to the roster. They are either promoted (on finalize) or hard-deleted (on
    cancel). If a draft is left behind by an interrupted session, it stays
    invisible until the server restarts.
    """
    staff = _require_clinical_staff(staff)
    await _maybe_trigger_preop_outreach(request.app)
    patients = []
    for pid, d in _patient_store.items():
        if d.get("is_draft"):
            continue
        patient_hs_id = str(d.get("health_system_id") or "")
        if staff.source == "tenant" and staff.tenant_id:
            if patient_hs_id and patient_hs_id != str(staff.tenant_id):
                continue
        elif staff.source == "landing":
            if patient_hs_id and patient_hs_id != DEMO_HEALTH_SYSTEM_ID:
                continue
        sd = d.get("structured_data") or {}
        episode = _team_store.get_episode(pid) or _team_store.ensure_episode(
            patient_id=pid,
            procedure_type=sd.get("procedure_name", ""),
            clinic_code=d.get("clinic_code") or "",
            resource_code=d.get("resource_code") or "",
            health_system_id=d.get("health_system_id"),
        )
        latest_intake = _team_store.get_latest_intake_form_for_patient(pid)
        open_dt = date.fromisoformat(episode["open_date"])
        day_in_episode = max(1, (date.today() - open_dt).days + 1)
        day_in_episode = min(day_in_episode, 30)
        row = {
            "id": pid,
            "name": d.get("name", "Unknown"),
            "procedure": sd.get("procedure_name", ""),
            "date": sd.get("procedure_date", ""),
            "pcpReferralSent": bool(d.get("pcp_referral_sent") or sd.get("pcp_referral_sent")),
            "pcpName": (d.get("pcp_name") or sd.get("pcp_name") or ""),
            "hasResources": d.get("resources") is not None,
            "pipelineType": d.get("pipeline_type", "post_op"),
            "phone": d.get("phone", ""),
            "email": d.get("email", ""),
            "mbi": (d.get("mbi") or sd.get("mbi") or ""),
            "dob": _patient_dob_for_roster(sd),
            "anchorProcedure": _patient_anchor_for_roster(d, sd),
            "health_system_code": d.get("clinic_code") or "",
            "phase": d.get("phase"),
            "orStartedAt": d.get("or_started_at"),
            "orEndedAt": d.get("or_ended_at"),
            "tierLastChanged": d.get("tier_last_changed"),
            "hasActiveSelfFlag": _team_store.has_active_self_flag(pid),
            "intakeFormStatus": (latest_intake or {}).get("status") or "NOT_STARTED",
            "intakeFormId": (latest_intake or {}).get("id"),
            "eligibilityStatus": d.get("eligibility_status"),
            "eligibilityCheckId": d.get("eligibility_check_id"),
            "eligibilityFailingRule": _first_failing_rule_label(
                (elig_store.get_check(d.get("eligibility_check_id")) or {}).get("verdicts")
                if d.get("eligibility_check_id")
                else None
            ),
            "episode": {
                "openDate": episode["open_date"],
                "closeDate": episode["close_date"],
                "status": episode["status"],
                "currentDay": day_in_episode,
            },
        }
        # Triage Suite Pass 3 §4.3 — three-tier chain so the doctor
        # roster can render `T@upload → T@intake → Now`. Both
        # `initial_tier` and `post_intake_tier` are stable across the
        # episode (immutable once stamped); `current_tier` is the
        # rolling live value. On cold start the blob may have lost
        # these fields, so we hydrate from `episode_snapshots`.
        post_intake_tier = d.get("post_intake_tier")
        if post_intake_tier in (None, ""):
            try:
                snap = _team_store.get_episode_snapshot(pid) or {}
                snap_pit = snap.get("post_intake_tier")
                if snap_pit:
                    d["post_intake_tier"] = snap_pit
                    post_intake_tier = snap_pit
            except Exception:
                post_intake_tier = None
        row["initialTier"] = d.get("initial_tier")
        row["postIntakeTier"] = post_intake_tier
        row["currentTier"] = d.get("current_tier")
        # True once generated materials (voice/battlecard) exist on the patient, so
        # the UI can switch the "Generate resources" CTA to View / Send and persist it.
        _res = d.get("resources") or {}
        def _track_has_materials(_t):
            _td = _res.get(_t) or {}
            return bool(_td.get("voice_audio_url") or _td.get("battlecard_html"))
        row["materialsReady"] = bool(
            d.get("voice_audio_url")
            or d.get("battlecard_html")
            or _track_has_materials("preop")
            or _track_has_materials("diagnosis")
            or _track_has_materials("treatment")
        )
        row["requiresClinicianReview"] = bool(d.get("requires_clinician_review"))
        row["groundingSummaries"] = d.get("grounding_summaries") or {}
        row["groundingPendingTracks"] = d.get("grounding_pending_tracks") or []
        if (d.get("pipeline_type") or "").lower() == "pre_op" and parse_surgery_datetime(sd.get("procedure_date") or ""):
            row["windows"] = {
                "t96": compute_window_tier(patient_id=pid, window="t96", team_store=_team_store, patient_dict=d),
                "t48": compute_window_tier(patient_id=pid, window="t48", team_store=_team_store, patient_dict=d),
                "t24": compute_window_tier(patient_id=pid, window="t24", team_store=_team_store, patient_dict=d),
            }
        patients.append(row)
    return {"patients": patients}


@app.get("/api/patient/{patient_id}/discharge-materials")
async def get_discharge_materials(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _assert_clinical_staff_can_access_patient(patient_id, staff)
    d = _patient_store[patient_id]
    resources = d.get("resources") or {}
    return {
        "patient_id": patient_id,
        "diagnosis": resources.get("diagnosis"),
        "treatment": resources.get("treatment"),
        "preop": resources.get("preop"),
    }


@app.post("/api/patient/{patient_id}/events")
async def track_patient_event(
    patient_id: str,
    body: EventTrackRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    allowed = {
        "platform_opened",
        "email_sent",
        "diagnosis_video_watched",
        "treatment_video_watched",
        "preop_video_watched",
        "avatar_chat",
        "care_team_notification",
        "preop_intake_submitted",
        "survey_pending",
        "survey_completed",
        "sms_sent",
        "intake_started",
        "intake_completed",
        "preop_survey_opened",
    }
    event_type = (body.event_type or "").strip()
    if event_type not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported event_type")
    _team_store.log_event(patient_id=patient_id, event_type=event_type, payload=body.payload or {})
    return {"ok": True}


@app.patch("/api/patient/{patient_id}/pcp-referral")
async def update_pcp_referral(
    patient_id: str,
    body: PCPReferralUpdateRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _assert_clinical_staff_can_access_patient(patient_id, staff)
    p = _patient_store[patient_id]
    sd = p.get("structured_data") or {}
    pcp_name = (body.pcp_name or "").strip()
    sent = bool(body.sent)
    p["pcp_referral_sent"] = sent
    p["pcp_name"] = pcp_name or None
    sd["pcp_referral_sent"] = sent
    sd["pcp_name"] = pcp_name or None
    p["structured_data"] = sd
    _team_store.log_event(
        patient_id=patient_id,
        event_type="email_sent" if sent else "care_team_notification",
        payload={"channel": "pcp_summary_referral", "pcp_name": pcp_name},
    )
    _persist_demo_patient_store()
    return {"ok": True, "pcpReferralSent": sent, "pcpName": pcp_name}


@app.get("/api/demo/sign-in-routes")
async def demo_sign_in_routes():
    """Public email → auth routing hints for landing sign-in (no passwords)."""
    return demo_credentials.sign_in_routes(cedar_email=DEMO_DOCTOR_EMAIL)


@app.patch("/api/patient/{patient_id}")
async def update_patient_details(
    patient_id: str,
    body: PatientUpdateRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _assert_clinical_staff_can_access_patient(patient_id, staff)
    p = _patient_store[patient_id]
    sd = dict(p.get("structured_data") or {})
    changed: Dict[str, Any] = {}

    if body.name is not None:
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Patient name cannot be empty")
        if len(name) > 120:
            raise HTTPException(status_code=400, detail="Patient name must be 1-120 chars")
        p["name"] = name
        sd["patient_name"] = name
        changed["name"] = name

    if body.phone is not None:
        phone = (body.phone or "").strip()
        p["phone"] = phone
        changed["phone"] = phone

    if body.email is not None:
        email = (body.email or "").strip()
        p["email"] = email
        changed["email"] = email

    if body.mbi is not None:
        mbi = (body.mbi or "").strip().upper()
        if mbi and not _MBI_RE.match(mbi):
            raise HTTPException(status_code=400, detail="MBI format invalid")
        if mbi:
            for other_pid, other in _patient_store.items():
                if other_pid == patient_id:
                    continue
                other_mbi = str((other.get("structured_data") or {}).get("mbi") or other.get("mbi") or "").upper().strip()
                if other_mbi and other_mbi == mbi:
                    raise HTTPException(
                        status_code=409,
                        detail=f"MBI already assigned to patient {other.get('name') or other_pid}",
                    )
        p["mbi"] = mbi
        sd["mbi"] = mbi
        changed["mbi"] = mbi

    if body.dob is not None:
        dob = (body.dob or "").strip()
        if dob:
            _validate_patient_iso_date(dob, "dob")
        sd["dob"] = dob or None
        sd["date_of_birth"] = dob or None
        changed["dob"] = dob

    if body.scheduled_surgery_date is not None:
        surgery = (body.scheduled_surgery_date or "").strip()
        if surgery:
            _validate_patient_iso_date(surgery, "scheduled_surgery_date")
        sd["procedure_date"] = surgery
        changed["scheduled_surgery_date"] = surgery

    if body.anchor_procedure is not None:
        anchor = (body.anchor_procedure or "").strip()
        if anchor and anchor not in _ANCHOR_PROCEDURES:
            raise HTTPException(status_code=400, detail="Invalid anchor procedure")
        sd["procedure_name"] = anchor
        if anchor:
            p["anchor_procedure_family"] = anchor
        changed["anchor_procedure"] = anchor

    p["structured_data"] = sd

    if body.anchor_procedure is not None or body.scheduled_surgery_date is not None:
        _team_store.ensure_episode(
            patient_id=patient_id,
            procedure_type=sd.get("procedure_name", ""),
            clinic_code=p.get("clinic_code") or "",
            resource_code=p.get("resource_code") or "",
            health_system_id=p.get("health_system_id"),
        )

    elig_store.append_audit(
        action="patient_details_updated",
        actor=_staff_actor_id(staff),
        patient_id=patient_id,
        meta=changed,
    )
    _persist_demo_patient_store()

    return {
        "ok": True,
        "patient": {
            "id": patient_id,
            "name": p.get("name"),
            "phone": p.get("phone"),
            "email": p.get("email"),
            "mbi": p.get("mbi") or sd.get("mbi"),
            "dob": _patient_dob_for_roster(sd),
            "date": sd.get("procedure_date", ""),
            "procedure": sd.get("procedure_name", ""),
            "anchorProcedure": _patient_anchor_for_roster(p, sd),
        },
    }


@app.get("/api/patient/{patient_id}/timeline")
async def get_patient_timeline(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _assert_clinical_staff_can_access_patient(patient_id, staff)
    d = _patient_store[patient_id]
    sd = d.get("structured_data") or {}
    episode = _team_store.get_episode(patient_id) or _team_store.ensure_episode(
        patient_id=patient_id,
        procedure_type=sd.get("procedure_name", ""),
        clinic_code=d.get("clinic_code") or "",
        resource_code=d.get("resource_code") or "",
        health_system_id=d.get("health_system_id"),
    )
    open_dt = date.fromisoformat(episode["open_date"])
    close_dt = date.fromisoformat(episode["close_date"])
    current_day = max(1, min((date.today() - open_dt).days + 1, 30))
    events = _team_store.get_events(patient_id)
    markers = {str(i): [] for i in range(1, 31)}
    for ev in events:
        if not _timeline_event_visible(ev.get("event_type")):
            continue
        day_num = _episode_day_from_open(episode["open_date"], ev["occurred_at"])
        if day_num is None or day_num < 1 or day_num > 30:
            continue
        markers[str(day_num)].append(
            {
                "id": ev["id"],
                "type": ev["event_type"],
                "timestamp": ev["occurred_at"],
                "payload": ev.get("payload", {}),
            }
        )
    survey_responses = _team_store.get_survey_responses(patient_id)
    for sr in survey_responses:
        st = str(sr.get("survey_type") or "postop")
        if st != "postop":
            continue
        day = int(sr["survey_day"])
        if day < 1 or day > 30:
            continue
        markers[str(day)].append(
            {
                "id": sr["id"],
                "type": "survey_completed",
                "timestamp": sr["submitted_at"],
                "payload": {"survey_day": day, "score": sr.get("score"), "tier": sr.get("tier")},
            }
        )
    if (d.get("clinic_code") or "").upper() == TRIAGEDM_CLINIC_CODE:
        completed_survey_days = {
            int(sr["survey_day"])
            for sr in survey_responses
            if str(sr.get("survey_type") or "postop") == "postop"
        }
        for send_row in _team_store.get_survey_sends(patient_id):
            day = int(send_row["survey_day"])
            if day in completed_survey_days:
                continue
            if day < 1 or day > 30:
                continue
            markers[str(day)].append(
                {
                    "id": f"survey-pending-{day}",
                    "type": "survey_pending",
                    "timestamp": send_row.get("sent_at"),
                    "payload": {"survey_day": day},
                }
            )
    return {
        "patient_id": patient_id,
        "episode": {
            "openDate": episode["open_date"],
            "closeDate": episode["close_date"],
            "status": episode["status"],
            "currentDay": current_day,
            "procedureType": episode.get("procedure_type") or sd.get("procedure_name", ""),
        },
        "markers": markers,
        "surveys": survey_responses,
        "compositeScore": _team_store.get_composite_score(patient_id),
    }


@app.get("/api/escalations")
async def list_escalations(staff: Optional[StaffContext] = Depends(get_staff_context_optional)):
    staff = _require_clinical_staff(staff)
    rows = _team_store.list_escalations()
    out = []
    filter_applied = (
        "surgeon_tier3_only" if staff.source == "tenant" and staff.role == "surgeon" else None
    )
    for row in rows:
        patient = _patient_store.get(row["patient_id"], {})
        if staff.source == "tenant" and staff.tenant_id:
            if (patient.get("health_system_id") or "") != staff.tenant_id:
                continue
        elif staff.source == "landing":
            if (patient.get("health_system_id") or "") != DEMO_HEALTH_SYSTEM_ID:
                continue
        trigger = row["trigger_type"]
        origin = "Care Team Notification" if str(trigger).startswith("care_team_notification") else "Chat"
        tier_val = row["tier"]
        current_tier = _normalize_tier(patient.get("current_tier")) or _normalize_tier(tier_val) or 1
        episode_phase = (patient.get("phase") or patient.get("pipeline_type") or "post_op")
        if (
            staff
            and staff.source == "tenant"
            and staff.role == "surgeon"
            and int(tier_val) != 3
        ):
            continue
        out.append(
            {
                "id": row["id"],
                "patient_id": row["patient_id"],
                "patient_name": patient.get("name", row["patient_id"]),
                "tier": tier_val,
                "current_tier": current_tier,
                "episode_phase": episode_phase,
                "trigger_type": row["trigger_type"],
                "origin": origin,
                "message": row.get("message", ""),
                "consent": row.get("consent"),
                "consent_at": row.get("consent_at"),
                "resolved": bool(row.get("resolved")),
                "created_at": row["created_at"],
                "conversation_snapshot": row.get("conversation_snapshot", []),
            }
        )
    resolved_count = sum(1 for r in out if r["resolved"])
    body = {"escalations": out, "resolved_count": resolved_count, "total_count": len(out)}
    if filter_applied:
        body["filter_applied"] = filter_applied
    return body


@app.patch("/api/escalations/{escalation_id}/resolved")
async def set_escalation_resolved(
    escalation_id: int,
    body: EscalationResolveRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    row = _team_store.get_escalation(escalation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Escalation not found")
    _assert_clinical_staff_can_access_patient(row["patient_id"], staff)
    _team_store.set_escalation_resolved(escalation_id, body.resolved)
    return {"ok": True, "resolved": body.resolved}


@app.get("/api/escalations/{escalation_id}/triage-timeline")
async def get_triage_timeline(
    escalation_id: int,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    row = _team_store.get_escalation(escalation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Escalation not found")
    patient_id = row["patient_id"]
    _assert_clinical_staff_can_access_patient(patient_id, staff)
    staff = _require_clinical_staff(staff)
    patient = _patient_store.get(patient_id) or {}
    snapshot = _team_store.get_episode_snapshot(patient_id) or {}

    surgery = {
        "procedure_date": (patient.get("structured_data") or {}).get("procedure_date"),
        "or_started_at": patient.get("or_started_at"),
        "or_ended_at": patient.get("or_ended_at"),
        "discharge_at": patient.get("discharge_at"),
    }
    or_started_dt = _parse_iso_datetime(surgery["or_started_at"])
    or_ended_dt = _parse_iso_datetime(surgery["or_ended_at"])
    discharge_dt = _parse_iso_datetime(surgery["discharge_at"])

    def _postop_day_number(
        inputs_snapshot: Optional[Dict[str, Any]],
        at_dt: Optional[datetime] = None,
    ) -> Optional[int]:
        snap = inputs_snapshot or {}
        days = snap.get("days_since_discharge")
        if days is not None:
            try:
                days_i = int(days)
                if days_i > 0:
                    return days_i
            except (TypeError, ValueError):
                pass
        day_alt = snap.get("day")
        if day_alt is not None:
            try:
                day_i = int(day_alt)
                if day_i > 0:
                    return day_i
            except (TypeError, ValueError):
                pass
        if at_dt and discharge_dt and at_dt >= discharge_dt:
            return max(1, int((at_dt - discharge_dt).days) + 1)
        return None

    def classify_phase(
        at_iso: Optional[str],
        source: str,
        inputs_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        at_dt = _parse_iso_datetime(at_iso)
        src = (source or "").strip().lower()

        if src == "postop":
            day_n = _postop_day_number(inputs_snapshot, at_dt)
            if day_n:
                return {"phase": "POST_OP", "phase_label": f"Post-Op — Day {day_n}"}
            return {"phase": "POST_OP", "phase_label": "Post-Op"}

        if src == "intraop":
            if (
                at_dt
                and or_ended_dt
                and or_started_dt
                and or_started_dt <= at_dt <= or_ended_dt
            ):
                return {"phase": "INTRA_OP", "phase_label": "Intra-Op (in OR)"}
            return {"phase": "INTRA_OP", "phase_label": "After Intra-Op Procedure"}

        if src == "preop":
            return {"phase": "PRE_OP", "phase_label": "Pre-Op"}

        if at_dt is None:
            return {"phase": "PRE_OP", "phase_label": "Pre-Op"}

        if or_started_dt is None or at_dt < or_started_dt:
            return {"phase": "PRE_OP", "phase_label": "Pre-Op"}
        if or_ended_dt and or_started_dt and or_started_dt <= at_dt <= or_ended_dt:
            return {"phase": "INTRA_OP", "phase_label": "Intra-Op (in OR)"}
        if or_ended_dt and at_dt > or_ended_dt:
            if discharge_dt and at_dt < discharge_dt:
                return {"phase": "INTRA_OP", "phase_label": "After Intra-Op Procedure"}
            if discharge_dt and at_dt >= discharge_dt:
                day_n = max(1, int((at_dt - discharge_dt).days) + 1)
                return {"phase": "POST_OP", "phase_label": f"Post-Op — Day {day_n}"}
            return {"phase": "INTRA_OP", "phase_label": "After Intra-Op Procedure"}
        return {"phase": "PRE_OP", "phase_label": "Pre-Op"}

    timeline: List[Dict[str, Any]] = []

    initial_tier = _normalize_tier(patient.get("initial_tier") or snapshot.get("initial_tier"))
    current_tier_fallback = _normalize_tier(row.get("tier"))
    if initial_tier is None and current_tier_fallback is not None:
        initial_tier = current_tier_fallback
    if initial_tier is not None:
        initial_at = (
            patient.get("initial_tier_assigned_at")
            or patient.get("tier_last_changed")
            or row.get("created_at")
        )
        init_phase = classify_phase(initial_at, "initial")
        timeline.append(
            {
                "at": initial_at,
                "phase": init_phase["phase"],
                "phase_label": init_phase["phase_label"],
                "tier_before": None,
                "tier_after": initial_tier,
                "changed": True,
                "triggered_by": "INITIAL_ASSESSMENT",
                "source": "initial",
                "reasons": list(patient.get("initial_tier_reasons") or []),
            }
        )

    for rec in _team_store.list_preop_retier_events(patient_id, limit=200):
        at = rec.get("created_at")
        phase = classify_phase(at, "preop", rec.get("inputs_snapshot") or {})
        timeline.append(
            {
                "at": at,
                "phase": phase["phase"],
                "phase_label": phase["phase_label"],
                "tier_before": _normalize_tier(rec.get("tier_before")),
                "tier_after": _normalize_tier(rec.get("tier_after")),
                "changed": bool(rec.get("changed")),
                "triggered_by": rec.get("triggered_by"),
                "source": "preop",
                "reasons": list(rec.get("reasons") or []),
            }
        )

    for rec in _team_store.list_intraop_reassessments(patient_id):
        at = rec.get("triggered_at")
        before_t = _normalize_tier(rec.get("pre_or_current_tier"))
        after_t = _normalize_tier(rec.get("final_tier"))
        phase = classify_phase(at, "intraop", rec.get("form_snapshot") or {})
        timeline.append(
            {
                "at": at,
                "phase": phase["phase"],
                "phase_label": phase["phase_label"],
                "tier_before": before_t,
                "tier_after": after_t,
                "changed": bool(before_t is not None and after_t is not None and before_t != after_t),
                "triggered_by": rec.get("triggered_by"),
                "source": "intraop",
                "reasons": list(rec.get("reasons") or []),
            }
        )

    for rec in _team_store.list_postop_retier_events(patient_id, limit=200):
        at = rec.get("created_at")
        phase = classify_phase(at, "postop", rec.get("inputs_snapshot") or {})
        timeline.append(
            {
                "at": at,
                "phase": phase["phase"],
                "phase_label": phase["phase_label"],
                "tier_before": _normalize_tier(rec.get("tier_before")),
                "tier_after": _normalize_tier(rec.get("tier_after")),
                "changed": bool(rec.get("changed")),
                "triggered_by": rec.get("triggered_by"),
                "source": "postop",
                "reasons": list(rec.get("reasons") or []),
            }
        )

    def sort_key(item: Dict[str, Any]) -> tuple:
        dt = _parse_iso_datetime(item.get("at"))
        return (dt is None, dt or datetime.min)

    timeline.sort(key=sort_key)
    for node in timeline:
        if node.get("phase") and node.get("phase_label"):
            continue
        phase = classify_phase(node.get("at"), str(node.get("source") or ""), {})
        node["phase"] = phase["phase"]
        node["phase_label"] = phase["phase_label"]

    current_tier = _normalize_tier(patient.get("current_tier")) or current_tier_fallback or 1
    current_tier_since = patient.get("tier_last_changed")
    if not current_tier_since:
        changed_nodes = [n for n in timeline if n.get("changed")]
        if changed_nodes:
            current_tier_since = changed_nodes[-1].get("at")

    return {
        "patient_id": patient_id,
        "patient_name": patient.get("name") or row.get("patient_id"),
        "episode_phase": (patient.get("phase") or patient.get("pipeline_type") or "post_op"),
        "current_tier": current_tier,
        "current_tier_since": current_tier_since,
        "intervention_subject": f"{_provider_email_signature(staff)} — URGENT CARE MESSAGE",
        "surgery": surgery,
        "timeline": timeline,
    }


@app.post("/api/escalations/{escalation_id}/intervention")
async def send_intervention(
    escalation_id: int,
    body: EscalationInterventionRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    row = _team_store.get_escalation(escalation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Escalation not found")
    patient_id = row["patient_id"]
    _assert_clinical_staff_can_access_patient(patient_id, staff)
    staff = _require_clinical_staff(staff)

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required.")

    from routers.messaging import persist_and_notify_care_team_message

    urgent = int(row.get("tier") or 0) >= 3
    result = await persist_and_notify_care_team_message(
        request,
        patient_id=patient_id,
        message=message,
        staff=staff,
        escalation_id=escalation_id,
        urgent=urgent,
    )
    return result


@app.post("/api/escalations/consent")
async def submit_escalation_consent(body: EscalationConsentRequest):
    esc = _team_store.get_escalation(body.escalation_id)
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")
    consent = (body.consent or "").strip().lower()
    if consent not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="Consent must be 'yes' or 'no'")
    _team_store.set_escalation_consent(body.escalation_id, consent)
    return {"ok": True}


@app.get("/survey", response_class=HTMLResponse)
async def survey_page(clinic_code: str, resource_code: str, day: int):
    if day not in (7, 14, 30):
        raise HTTPException(status_code=400, detail="Day must be 7, 14, or 30")
    clinic_code = (clinic_code or "").strip().upper()
    resource_code = (resource_code or "").strip().upper()
    patient_id = None
    for pid, d in _patient_store.items():
        if (d.get("clinic_code") or "").upper() == clinic_code and (d.get("resource_code") or "").upper() == resource_code:
            patient_id = pid
            break
    if not patient_id:
        raise HTTPException(status_code=404, detail="Patient not found for credentials")
    qs = _question_set_for_day(day)
    opts = "".join([f"<option value='{html_lib.escape(o)}'>{html_lib.escape(o)}</option>" for o in qs["options"]])
    question_rows = []
    for idx, q in enumerate(qs["questions"], start=1):
        question_rows.append(
            f"""
            <div style='margin-bottom:14px;'>
              <label style='display:block;font-weight:600;margin-bottom:6px;'>{idx}. {html_lib.escape(q)}</label>
              <select id='q_{idx}' style='width:100%;padding:10px;border:1px solid #d1d5db;border-radius:8px;'>
                <option value=''>Select response</option>{opts}
              </select>
            </div>
            """
        )
    html = f"""
    <html><head><title>Recovery Survey Day {day}</title></head>
    <body style="font-family:Inter,Arial,sans-serif;background:#f8fafc;margin:0;padding:24px;">
      <div style="max-width:760px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;">
        <h1 style="margin:0 0 8px 0;">Recovery Survey - Day {day}</h1>
        <p style="color:#64748b;margin-top:0;">Thank you for completing this check-in. Your responses are shared with your care team.</p>
        {''.join(question_rows)}
        <button id="submitBtn" style="margin-top:10px;padding:10px 16px;background:#1d4ed8;border:0;border-radius:8px;color:#fff;font-weight:600;cursor:pointer;">Submit Survey</button>
        <p id="statusMsg" style="margin-top:12px;color:#475569;"></p>
      </div>
      <script>
        document.getElementById('submitBtn').addEventListener('click', async () => {{
          const answers = [];
          const count = {len(qs["questions"])};
          for (let i = 1; i <= count; i += 1) {{
            const v = document.getElementById('q_' + i).value;
            if (!v) {{
              document.getElementById('statusMsg').textContent = 'Please answer all questions.';
              return;
            }}
            answers.push({{question_index: i, response: v}});
          }}
          const res = await fetch('/api/survey/submit', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
              clinic_code: {json.dumps(clinic_code)},
              resource_code: {json.dumps(resource_code)},
              day: {day},
              answers
            }})
          }});
          if (res.ok) {{
            document.getElementById('statusMsg').textContent = 'Submitted. Thank you.';
            document.getElementById('submitBtn').disabled = true;
          }} else {{
            const data = await res.json().catch(() => ({{}}));
            document.getElementById('statusMsg').textContent = data.detail || 'Could not submit survey.';
          }}
        }});
      </script>
    </body></html>
    """
    return HTMLResponse(content=html)


@app.post("/api/survey/submit", dependencies=[Depends(rate_limiter("survey_submit", 30, 60))])
async def submit_survey(body: SurveySubmitRequest):
    day = int(body.day)
    if day not in (7, 14, 30):
        raise HTTPException(status_code=400, detail="Day must be 7, 14, or 30")
    patient = None
    patient_id = None
    for pid, d in _patient_store.items():
        if (d.get("clinic_code") or "").upper() == body.clinic_code.strip().upper() and (d.get("resource_code") or "").upper() == body.resource_code.strip().upper():
            patient_id = pid
            patient = d
            break
    if not patient_id:
        raise HTTPException(status_code=404, detail="Patient not found for credentials")
    score_info = _score_survey_answers(body.answers)
    _team_store.save_survey_response(
        patient_id=patient_id,
        survey_day=day,
        answers=body.answers,
        score=score_info["score"],
        tier=score_info["tier"],
    )
    _team_store.log_event(
        patient_id=patient_id,
        event_type="survey_completed",
        payload={"survey_day": day, "tier": score_info["tier"]},
    )
    return {"ok": True, "patient_id": patient_id}


def _preop_window_answer_map(answers: List[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for a in answers or []:
        qid = str(a.get("id") or a.get("question_id") or "").strip()
        if not qid:
            continue
        out[qid] = str(a.get("response") or a.get("value") or "").strip()
    return out


def _build_preop_window_detail(patient_id: str, window: str) -> Dict[str, Any]:
    w = (window or "").lower()
    if w not in WINDOW_SURVEY_DAY:
        raise HTTPException(status_code=400, detail="window must be t96, t48, or t24")
    d = _patient_store[patient_id]
    sd = d.get("structured_data") or {}
    surgery = parse_surgery_datetime(sd.get("procedure_date") or "")
    qs = questions_for_window(w, sd)
    day = WINDOW_SURVEY_DAY[w]
    row = _team_store.get_survey_response(patient_id, day, survey_type="preop")
    answers = (row or {}).get("answers") or []
    by_id = _preop_window_answer_map(answers)
    scored = None
    if answers and surgery:
        scored = score_preop_survey(w, answers, surgery, sd)
    per_red = {str(p["id"]): bool(p.get("red")) for p in (scored or {}).get("per_question", []) if p.get("id")}
    questions_out = []
    for q in qs:
        qid = q["id"]
        ans = by_id.get(qid, "")
        questions_out.append(
            {
                "id": qid,
                "text": q.get("text"),
                "type": q.get("type"),
                "options": q.get("options") or [],
                "patient_answer": ans,
                "highlight_red": bool(per_red.get(qid)),
            }
        )
    tier_info = compute_window_tier(patient_id=patient_id, window=w, team_store=_team_store, patient_dict=d)
    h = tier_info.get("hours_until_surgery")
    sw = tier_info.get("survey_window")
    msg = ""
    if tier_info.get("survey_submitted"):
        msg = "Survey completed."
    elif sw == "not_yet_open":
        oi = tier_info.get("opens_in_hours")
        if oi is not None and h is not None:
            msg = f"Survey not yet available. Opens in about {int(oi)}h."
    elif sw == "open":
        msg = "Due now — patient has not responded."
    else:
        msg = "Survey window closed; no response on file."
    return {
        "patient_id": patient_id,
        "window": w,
        "procedure_date": sd.get("procedure_date", ""),
        "summary": tier_info,
        "questions": questions_out,
        "survey_scoring": scored,
        "status_message": msg,
    }


@app.get("/api/preop-survey/questions")
async def get_preop_survey_questions(window: str, patient_id: Optional[str] = None):
    w = (window or "").lower()
    sd: Dict[str, Any] = {}
    if patient_id and patient_id in _patient_store:
        sd = _patient_store[patient_id].get("structured_data") or {}
    try:
        qs = questions_for_window(w, sd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    surgery = parse_surgery_datetime(sd.get("procedure_date") or "")
    return {
        "window": w,
        "questions": qs,
        "procedure_date": sd.get("procedure_date"),
        "surgery_display": surgery.isoformat() if surgery else None,
    }


@app.post("/api/preop-survey/submit", dependencies=[Depends(rate_limiter("preop_survey_submit", 30, 60))])
async def submit_preop_survey(body: PreOpSurveySubmitBody):
    w = (body.window or "").lower()
    if w not in WINDOW_SURVEY_DAY:
        raise HTTPException(status_code=400, detail="window must be t96, t48, or t24")
    pid = (body.patient_id or "").strip()
    if pid not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    d = _patient_store[pid]
    sd = d.get("structured_data") or {}
    surgery = parse_surgery_datetime(sd.get("procedure_date") or "")
    if not surgery:
        raise HTTPException(status_code=400, detail="Scheduled surgery date required")
    h = hours_until_surgery(surgery, datetime.utcnow())
    if survey_window_state(w, h) != "open":
        raise HTTPException(status_code=400, detail="Survey window is not open for this patient")
    day = WINDOW_SURVEY_DAY[w]
    partial_row = {"answers": body.answers, "answers_json": json.dumps(body.answers)}
    tier_info = compute_window_tier(
        patient_id=pid, window=w, team_store=_team_store, patient_dict=d, survey_row=partial_row
    )
    prev_row = _team_store.get_survey_response(pid, day, survey_type="preop")
    prev_tier = None
    if prev_row:
        prev_tier = compute_window_tier(
            patient_id=pid, window=w, team_store=_team_store, patient_dict=d, survey_row=prev_row
        ).get("tier")
    score_survey = score_preop_survey(w, body.answers, surgery, sd)
    _team_store.save_survey_response(
        patient_id=pid,
        survey_day=day,
        answers=body.answers,
        score=tier_info.get("score"),
        tier=tier_info.get("tier"),
        survey_type="preop",
    )
    _team_store.log_event(
        patient_id=pid,
        event_type="preop_survey_completed",
        payload={
            "window": w,
            "tier": tier_info.get("tier"),
            "score": tier_info.get("score"),
            "survey_score": tier_info.get("survey_score"),
        },
    )
    if tier_info.get("tier") == "red":
        trig = preop_escalation_trigger(w)
        if not _team_store.has_open_escalation(pid, trig):
            esc_tier = 2 if w == "t24" else 3
            snap = [
                {"role": "system", "content": f"Pre-op window {w} red"},
                {"role": "system", "content": json.dumps(tier_info.get("flags") or [])},
            ]
            _team_store.create_escalation(
                patient_id=pid,
                tier=esc_tier,
                trigger_type=trig,
                message=f"Pre-op readiness red ({w}): {', '.join(tier_info.get('flags') or [])}",
                conversation_snapshot=snap,
                health_system_id=d.get("health_system_id"),
            )
    return {
        "ok": True,
        "tier": tier_info.get("tier"),
        "score": tier_info.get("score"),
        "survey_score": score_survey.get("survey_score"),
        "flags": tier_info.get("flags"),
        "previous_tier": prev_tier,
    }


@app.get("/api/patients/{patient_id}/preop-window/{window}")
async def get_preop_window_detail(
    patient_id: str,
    window: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    if not staff and not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    return _build_preop_window_detail(patient_id, window)


@app.post("/api/patients/{patient_id}/preop-window/{window}/action")
async def preop_window_action(
    patient_id: str,
    window: str,
    body: PreOpWindowActionBody,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    if not staff and not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    w = (window or "").lower()
    if w not in WINDOW_SURVEY_DAY:
        raise HTTPException(status_code=400, detail="Invalid window")
    act = (body.action or "").strip().lower()
    d = _patient_store[patient_id]
    if act == "mark_called":
        _team_store.log_event(
            patient_id=patient_id,
            event_type="preop_window_mark_called",
            payload={"window": w},
        )
        return {"ok": True}
    if act == "escalate_surgeon":
        snap = [{"role": "system", "content": f"Manual escalate surgeon ({w}) from doctor portal"}]
        eid = _team_store.create_escalation(
            patient_id=patient_id,
            tier=2,
            trigger_type=f"preop_window_manual:surgeon:{w}",
            message="Pre-op readiness: escalate to surgeon (manual)",
            conversation_snapshot=snap,
            health_system_id=d.get("health_system_id"),
        )
        return {"ok": True, "escalation_id": eid}
    if act == "recommend_cancel":
        snap = [{"role": "system", "content": f"Manual recommend cancel ({w}) from doctor portal"}]
        eid = _team_store.create_escalation(
            patient_id=patient_id,
            tier=2,
            trigger_type=f"preop_window_manual:cancel:{w}",
            message="Pre-op readiness: recommend case cancellation (manual)",
            conversation_snapshot=snap,
            health_system_id=d.get("health_system_id"),
        )
        return {"ok": True, "escalation_id": eid}
    raise HTTPException(status_code=400, detail="Unknown action")


@app.get("/api/doctor/patient/{patient_id}/survey/{day}")
async def get_doctor_survey(
    patient_id: str,
    day: int,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    if not staff and not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if day not in (7, 14, 30):
        raise HTTPException(status_code=400, detail="Day must be 7, 14, or 30")
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    survey = _team_store.get_survey_response(patient_id, day)
    question_set = _question_set_for_day(day)["questions"]
    answers_detailed: List[Dict[str, Any]] = []
    if survey:
        raw_answers = survey.get("answers") or []
        by_index: Dict[int, str] = {}
        for ans in raw_answers:
            idx_raw = ans.get("question_index") or ans.get("id")
            try:
                idx = int(idx_raw)
            except Exception:
                idx = 0
            if idx > 0:
                by_index[idx] = str(ans.get("response") or ans.get("value") or "").strip()
        for i, q in enumerate(question_set, start=1):
            answers_detailed.append(
                {
                    "question_index": i,
                    "question_text": q,
                    "response": by_index.get(i, "No response"),
                }
            )
    return {
        "patient_id": patient_id,
        "day": day,
        "response": survey,
        "question_set": question_set,
        "answers_detailed": answers_detailed,
        "composite_score": _team_store.get_composite_score(patient_id),
        "doctor_only": True,
    }


@app.get("/doctor/patient/{patient_id}", response_class=HTMLResponse)
async def doctor_patient_view(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Doctor's view of a patient dashboard (same as patient view but with back-to-roster nav)."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    # Staff-only: reject patient sessions (this is the clinician surface).
    _assert_clinical_staff_can_access_patient(patient_id, staff)

    d = _patient_store[patient_id]

    def clean_html(html):
        h = (html or "").strip()
        if h.startswith("```"):
            h = h.split("\n", 1)[1] if "\n" in h else h[3:]
            if h.endswith("```"):
                h = h[:-3].strip()
        return h

    resources = d.get("resources") or {}
    resources_json = None
    if resources.get("diagnosis") and resources.get("treatment"):
        resources_json = {
            "diagnosis": {
                "voice_audio_url": resources["diagnosis"].get("voice_audio_url"),
                "battlecard_html": clean_html(resources["diagnosis"].get("battlecard_html", "")),
            },
            "treatment": {
                "voice_audio_url": resources["treatment"].get("voice_audio_url"),
                "battlecard_html": clean_html(resources["treatment"].get("battlecard_html", "")),
            },
        }

    phone_team = d.get("office_phone") or os.getenv("CARE_TEAM_PHONE", "")
    patient_json = json.dumps({
        "id":           patient_id,
        "name":         d["name"],
        "firstName":    d["name"].split()[0],
        "pipelineType": d["pipeline_type"],
        "procedure":    d["structured_data"].get("procedure_name", ""),
        "visitDate":    d["structured_data"].get("procedure_date", ""),
        "audioUrl":     d.get("voice_audio_url") or None,
        "tavusUrl":     d.get("avatar_url") or None,
        "phoneTeam":    phone_team,
        "hasResources": resources_json is not None,
        "resources":    resources_json,
        "doctorView":   True,
    })

    html_path = os.path.join(os.path.dirname(__file__), "../frontend/index.html")
    with open(html_path) as f:
        html = f.read()

    html = _apply_cache_bust(html)
    inject = f"<script>window.__PATIENT__ = {patient_json};</script>"
    html = html.replace("</head>", f"{inject}\n</head>")
    voice_url = f"/patient/{patient_id}/digital-care-companion"
    html = html.replace('id="voiceAvatarBtn" href="#"', f'id="voiceAvatarBtn" href="{voice_url}"')

    return HTMLResponse(content=html)


# ─── PDF Upload ───────────────────────────────────────────────
from fastapi import File, UploadFile, Form

@app.post("/api/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    """Extract text from an uploaded PDF discharge document."""
    try:
        from PyPDF2 import PdfReader
        import io

        content = await file.read()
        reader = PdfReader(io.BytesIO(content))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""

        if not text.strip():
            raise HTTPException(status_code=422, detail="Could not extract text from PDF. The file may be scanned/image-based.")

        return {"text": text.strip(), "pages": len(reader.pages)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF processing failed: {str(e)}")


# ─── Send to Patient ──────────────────────────────────────────
@app.post("/api/send-to-patient/{patient_id}")
async def send_to_patient(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Send the patient dashboard link via SMS (Twilio) and email. Email includes clinic/resource codes and link to code-entry page."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    # Staff-only action: reject patient sessions.
    _assert_clinical_staff_can_access_patient(patient_id, staff)

    d = _patient_store[patient_id]
    name = d.get("name", "Patient")
    first_name = name.split()[0]
    # PRD-4: don't place the patient's name in an email body sent via a transport
    # without a BAA (e.g. SendGrid). Codes/links are not PHI; the name is.
    email_first_name = first_name if email_phi_allowed() else "there"
    phone = d.get("phone", "")
    email = d.get("email", "")
    is_preop = (d.get("pipeline_type") or "").lower() == "pre_op"
    materials_label = "surgery preparation resources" if is_preop else "post-surgery recovery resources"
    plan_label = "preparation plan" if is_preop else "recovery plan"
    email_subject = (
        "Your Surgery Preparation Resources Are Ready - Archangel Health"
        if is_preop else
        "Your Recovery Resources Are Ready - Archangel Health"
    )
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    dashboard_url = f"{base_url}/patient/{patient_id}/pre-op" if is_preop else f"{base_url}/patient/{patient_id}"
    clinic_code = (d.get("clinic_code") or "").strip()
    resource_code = (d.get("resource_code") or "").strip()
    # Always route patients through the code-entry flow (landing recovery form, or
    # the self-contained /recovery page) — never a bare dashboard URL (PRD-1).
    recovery_plan_entry_url = _patient_entry_url()

    results = {"sms": None, "email": None}

    # SMS via Twilio
    if phone:
        try:
            sms_body = (
                f"Hi {first_name}, your {materials_label} from your care team are ready. "
                f"View your personalized {plan_label} here: {recovery_plan_entry_url} "
                f"Use Health System Code: {clinic_code or 'N/A'}, Resource Code: {resource_code or 'N/A'}. "
                f"(Best viewed on a computer)"
            )
            sid = TwilioClient().send(to=phone, body=sms_body)
            results["sms"] = "sent" if sid else "twilio_not_configured"
            if results["sms"] == "sent":
                _team_store.log_event(patient_id=patient_id, event_type="sms_sent", payload={"channel": "sms_initial"})
        except Exception as e:
            print(f"[send] SMS error: {e}")
            results["sms"] = f"error: {str(e)}"

    # Email via shared email_utils (same SendGrid/SMTP path as onboarding OTP)
    if email:
        try:
            html_body = _render_recovery_email_html(
                first_name=email_first_name,
                clinic_code=clinic_code,
                resource_code=resource_code,
                recovery_plan_entry_url=recovery_plan_entry_url,
                is_preop=is_preop,
            )
            if not is_email_transport_configured():
                results["email"] = "sendgrid_not_configured"
                print(f"[send] Email skipped — SENDGRID_API_KEY / SMTP not configured. Would send to: {email}")
            else:
                sent_ok, reason = await _send_html_email_with_reason_impl(
                    email,
                    email_subject,
                    html_body,
                )
                if sent_ok:
                    results["email"] = "sent"
                    _team_store.log_event(patient_id=patient_id, event_type="email_sent", payload={"channel": "email_initial"})
                    print(f"[send] Email sent → {email}")
                else:
                    results["email"] = f"error: {reason}"
                    print(f"[send] Email FAILED → {email}: {reason}")

        except Exception as e:
            print(f"[send] Email error: {e}")
            results["email"] = f"error: {str(e)}"

    if not phone and not email:
        raise HTTPException(status_code=422, detail="No phone number or email on file for this patient")

    return {"patient_id": patient_id, "dashboard_url": dashboard_url, **results}


@app.get("/internal/email-template-preview", response_class=HTMLResponse, include_in_schema=False)
async def email_template_preview(
    first_name: str = "Tej",
    clinic_code: str = "TG85PQXR",
    resource_code: str = "1COGO60I",
):
    """Local browser preview of the exact backend email template."""
    base = _email_asset_base_url()
    return _render_recovery_email_html(
        first_name=first_name,
        clinic_code=clinic_code,
        resource_code=resource_code,
        recovery_plan_entry_url=_patient_entry_url(),
        use_local_preview_assets=True,
    )


# ─── New Two-Resource Pipeline ────────────────────────────────
def _stream_ctx() -> StreamingPipelineContext:
    return StreamingPipelineContext(
        patient_store=_patient_store,
        team_store=_team_store,
        persist_demo=_persist_demo_patient_store,
        base_url=os.getenv("BASE_URL", "http://localhost:8000"),
    )


async def _collect_stream_payload(gen) -> Dict[str, Any]:
    payload: Optional[Dict[str, Any]] = None
    async for ev in gen:
        if ev.get("stage") == "complete":
            maybe_payload = ev.get("payload")
            if isinstance(maybe_payload, dict):
                payload = maybe_payload
    if payload is None:
        raise RuntimeError("stream pipeline did not produce terminal payload")
    return payload


async def _sse(gen):
    try:
        async for ev in gen:
            yield f"data: {json.dumps(ev)}\n\n"
    except Exception as exc:  # noqa: BLE001
        err = {"stage": "error", "status": "error", "message": str(exc), "ts": round(time.time(), 3)}
        yield f"data: {json.dumps(err)}\n\n"


@app.post("/api/process-discharge")
async def process_discharge(
    input_data: DischargeInput,
    user: Optional[UserOut] = Depends(get_current_user_optional),
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """
    Full two-resource pipeline:
      Raw discharge notes → Extract → Generate (Diagnosis + Treatment)
                         → ElevenLabs audio for each → Store

    If called by an authenticated doctor with a profile, assigns clinic_code, resource_code, and office_phone to the patient.
    Returns both resource sets for immediate display.
    """
    import uuid

    patient_id = input_data.patient_id or f"pt_{uuid.uuid4().hex[:8]}"
    clinic_code = None
    resource_code = None
    office_phone = None
    health_system_id: Optional[str] = None
    if user and (user.role or "").lower() in ("surgeon", "doctor"):
        profile = get_doctor_profile(user.email)
        if profile:
            clinic_code = profile["clinic_code"]
            office_phone = profile.get("office_phone") or ""
            resource_code = _generate_resource_code()
            hs_row = _team_store.get_health_system_by_code(profile["clinic_code"] or "")
            if hs_row:
                health_system_id = hs_row["id"]
    if staff and staff.source == "tenant" and staff.tenant_id:
        health_system_id = staff.tenant_id
        if not clinic_code:
            clinic_code = (staff.health_system_code or "").strip().upper() or None
        if not resource_code:
            resource_code = _generate_resource_code()
        hs = _team_store.get_health_system_by_id(staff.tenant_id)
        if hs and not office_phone:
            office_phone = (hs.get("phone") or "").strip() or None
    if not office_phone:
        office_phone = (input_data.doctor_office_phone or "").strip() or None
    if not clinic_code:
        clinic_code = (input_data.doctor_clinic_code or "").strip().upper() or None
    if input_data.resource_code:
        resource_code = (input_data.resource_code or "").strip().upper() or None
    if clinic_code and not resource_code:
        resource_code = _generate_resource_code()

    try:
        return await _collect_stream_payload(
            run_postop_stream(
                input_data,
                patient_id=patient_id,
                clinic_code=clinic_code,
                resource_code=resource_code,
                office_phone=office_phone,
                health_system_id=health_system_id,
                ctx=_stream_ctx(),
            )
        )
    except Exception as exc:
        print(f"[pipeline] ERROR: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {str(exc)}")


@app.post("/api/process-preop")
async def process_preop(
    input_data: PreOpInput,
    user: Optional[UserOut] = Depends(get_current_user_optional),
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """
    Pre-op resource pipeline:
      Surgical prep notes -> Extract -> Generate pre-op voice + battlecard -> ElevenLabs audio -> Store
    """
    import uuid

    patient_id = input_data.patient_id or f"preop_{uuid.uuid4().hex[:8]}"
    clinic_code = None
    resource_code = None
    office_phone = None
    health_system_id: Optional[str] = None
    if user and (user.role or "").lower() in ("surgeon", "doctor"):
        profile = get_doctor_profile(user.email)
        if profile:
            clinic_code = profile["clinic_code"]
            office_phone = profile.get("office_phone") or ""
            resource_code = _generate_resource_code()
            hs_row = _team_store.get_health_system_by_code(profile["clinic_code"] or "")
            if hs_row:
                health_system_id = hs_row["id"]
    if staff and staff.source == "tenant" and staff.tenant_id:
        health_system_id = staff.tenant_id
        if not clinic_code:
            clinic_code = (staff.health_system_code or "").strip().upper() or None
        if not resource_code:
            resource_code = _generate_resource_code()
        hs = _team_store.get_health_system_by_id(staff.tenant_id)
        if hs and not office_phone:
            office_phone = (hs.get("phone") or "").strip() or None
    if not office_phone:
        office_phone = (input_data.doctor_office_phone or "").strip() or None
    if not clinic_code:
        clinic_code = (input_data.doctor_clinic_code or "").strip().upper() or None
    if input_data.resource_code:
        resource_code = (input_data.resource_code or "").strip().upper() or None
    if clinic_code and not resource_code:
        resource_code = _generate_resource_code()

    try:
        return await _collect_stream_payload(
            run_preop_stream(
                input_data,
                patient_id=patient_id,
                clinic_code=clinic_code,
                resource_code=resource_code,
                office_phone=office_phone,
                health_system_id=health_system_id,
                specialty_from_procedure=_specialty_from_procedure,
                ctx=_stream_ctx(),
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pre-op pipeline failed: {exc}")


@app.post("/api/process-discharge/stream")
async def process_discharge_stream(
    input_data: DischargeInput,
    user: Optional[UserOut] = Depends(get_current_user_optional),
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    import uuid

    patient_id = input_data.patient_id or f"pt_{uuid.uuid4().hex[:8]}"
    clinic_code = None
    resource_code = None
    office_phone = None
    health_system_id: Optional[str] = None
    if user and (user.role or "").lower() in ("surgeon", "doctor"):
        profile = get_doctor_profile(user.email)
        if profile:
            clinic_code = profile["clinic_code"]
            office_phone = profile.get("office_phone") or ""
            resource_code = _generate_resource_code()
            hs_row = _team_store.get_health_system_by_code(profile["clinic_code"] or "")
            if hs_row:
                health_system_id = hs_row["id"]
    if staff and staff.source == "tenant" and staff.tenant_id:
        health_system_id = staff.tenant_id
        if not clinic_code:
            clinic_code = (staff.health_system_code or "").strip().upper() or None
        if not resource_code:
            resource_code = _generate_resource_code()
        hs = _team_store.get_health_system_by_id(staff.tenant_id)
        if hs and not office_phone:
            office_phone = (hs.get("phone") or "").strip() or None
    if not office_phone:
        office_phone = (input_data.doctor_office_phone or "").strip() or None
    if not clinic_code:
        clinic_code = (input_data.doctor_clinic_code or "").strip().upper() or None
    if input_data.resource_code:
        resource_code = (input_data.resource_code or "").strip().upper() or None
    if clinic_code and not resource_code:
        resource_code = _generate_resource_code()
    gen = run_postop_stream(
        input_data,
        patient_id=patient_id,
        clinic_code=clinic_code,
        resource_code=resource_code,
        office_phone=office_phone,
        health_system_id=health_system_id,
        ctx=_stream_ctx(),
    )
    return StreamingResponse(
        _sse(gen),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/process-preop/stream")
async def process_preop_stream(
    input_data: PreOpInput,
    user: Optional[UserOut] = Depends(get_current_user_optional),
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    import uuid

    patient_id = input_data.patient_id or f"preop_{uuid.uuid4().hex[:8]}"
    clinic_code = None
    resource_code = None
    office_phone = None
    health_system_id: Optional[str] = None
    if user and (user.role or "").lower() in ("surgeon", "doctor"):
        profile = get_doctor_profile(user.email)
        if profile:
            clinic_code = profile["clinic_code"]
            office_phone = profile.get("office_phone") or ""
            resource_code = _generate_resource_code()
            hs_row = _team_store.get_health_system_by_code(profile["clinic_code"] or "")
            if hs_row:
                health_system_id = hs_row["id"]
    if staff and staff.source == "tenant" and staff.tenant_id:
        health_system_id = staff.tenant_id
        if not clinic_code:
            clinic_code = (staff.health_system_code or "").strip().upper() or None
        if not resource_code:
            resource_code = _generate_resource_code()
        hs = _team_store.get_health_system_by_id(staff.tenant_id)
        if hs and not office_phone:
            office_phone = (hs.get("phone") or "").strip() or None
    if not office_phone:
        office_phone = (input_data.doctor_office_phone or "").strip() or None
    if not clinic_code:
        clinic_code = (input_data.doctor_clinic_code or "").strip().upper() or None
    if input_data.resource_code:
        resource_code = (input_data.resource_code or "").strip().upper() or None
    if clinic_code and not resource_code:
        resource_code = _generate_resource_code()
    gen = run_preop_stream(
        input_data,
        patient_id=patient_id,
        clinic_code=clinic_code,
        resource_code=resource_code,
        office_phone=office_phone,
        health_system_id=health_system_id,
        specialty_from_procedure=_specialty_from_procedure,
        ctx=_stream_ctx(),
    )
    return StreamingResponse(
        _sse(gen),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─── Legacy Process Patient ───────────────────────────────────
@app.post("/api/process-patient", response_model=ProcessResponse)
async def process_patient(
    bundle: EHRBundle,
    background_tasks: BackgroundTasks,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Legacy full pipeline (single resource set)."""
    health_system_id = staff.tenant_id if (staff and staff.source == "tenant" and staff.tenant_id) else None
    resources_opt: Optional[Dict[str, Any]] = None
    _legacy_grounding_gate = None
    grounding_track = "post_op_treatment"
    try:
        raw_package = IngestLayer().process(bundle.model_dump())
        structured_data = await ExtractionLayer().extract(raw_package)
        pipeline_type = ClassificationLayer().classify(structured_data)
        generator = GenerationLayer()
        voice_script, battlecard_html = await generator.generate(structured_data, pipeline_type)

        grounding_track = "pre_op" if pipeline_type == "pre_op" else "post_op_treatment"

        async def _regen_legacy() -> str:
            nonlocal battlecard_html
            v, b = await generator.generate(structured_data, pipeline_type)
            battlecard_html = b
            return v

        legacy_gate, audio_url = await synthesize_script(
            patient_id=bundle.patient_id,
            structured_data=structured_data,
            script=voice_script,
            track=grounding_track,
            team_store=_team_store,
            audio_id=bundle.patient_id,
            regenerate_fn=_regen_legacy,
        )
        voice_script = legacy_gate.script
        _legacy_grounding_gate = legacy_gate
    except Exception as exc:
        prev_blob = _patient_store.get(bundle.patient_id) or {}
        triage_ok = _is_demo_mode() and (prev_blob.get("clinic_code") or "").upper() == "TRIAGEDM"
        if not triage_ok:
            raise HTTPException(status_code=500, detail=f"Pipeline failed: {exc}") from exc
        structured_data = dict(prev_blob.get("structured_data") or {})
        if bundle.clinical_notes:
            structured_data["discharge_notes"] = bundle.clinical_notes
        pipeline_type = "post_op"
        dx_h, tx_h, dx_s, tx_s = spinal_fusion_postop_demo_resources()
        voice_script = dx_s
        battlecard_html = dx_h
        resources_opt = {
            "diagnosis": {"voice_script": dx_s, "battlecard_html": dx_h, "voice_audio_url": None},
            "treatment": {"voice_script": tx_s, "battlecard_html": tx_h, "voice_audio_url": None},
        }
        _legacy_grounding_gate = None

    if _legacy_grounding_gate is None:
        _legacy_grounding_gate, audio_url = await synthesize_script(
            patient_id=bundle.patient_id,
            structured_data=structured_data,
            script=voice_script,
            track=grounding_track,
            team_store=_team_store,
            audio_id=bundle.patient_id,
            regenerate_fn=None,
        )
        voice_script = _legacy_grounding_gate.script
    try:
        avatar = await TavusClient().create_conversation(
            patient_id=bundle.patient_id,
            knowledge_base={
                "voice_script":  voice_script,
                "battlecard":    battlecard_html,
                "ehr_summary":   structured_data,
            },
        )
    except Exception:
        avatar = {"conversation_url": None}
    base_url      = os.getenv("BASE_URL", "http://localhost:8000")
    dashboard_url = f"{base_url}/patient/{bundle.patient_id}"
    clinic_code_m = ""
    prev = _patient_store.get(bundle.patient_id)
    if prev:
        clinic_code_m = (prev.get("clinic_code") or "") or ""
        prev.update({
            "name":                bundle.patient_name,
            "health_system_id":    health_system_id or prev.get("health_system_id"),
            "phone":               bundle.phone_number or prev.get("phone"),
            "pipeline_type":       pipeline_type,
            "voice_audio_url":     audio_url,
            "battlecard_html":     battlecard_html,
            "avatar_url":          avatar.get("conversation_url"),
            "structured_data":     {**(prev.get("structured_data") or {}), **structured_data},
            "voice_script":        voice_script,
        })
        if resources_opt is not None:
            prev["resources"] = resources_opt
        if _legacy_grounding_gate is not None:
            apply_grounding_to_patient(prev, grounding_track, _legacy_grounding_gate)
        _patient_store[bundle.patient_id] = prev
    else:
        blob = {
            "name":                bundle.patient_name,
            "health_system_id":    health_system_id,
            "phone":               bundle.phone_number,
            "pipeline_type":       pipeline_type,
            "voice_audio_url":     audio_url,
            "battlecard_html":     battlecard_html,
            "avatar_url":          avatar.get("conversation_url"),
            "structured_data":     structured_data,
            "voice_script":        voice_script,
            "resources":           resources_opt,
            "clinic_code":         clinic_code_m,
        }
        if _legacy_grounding_gate is not None:
            apply_grounding_to_patient(blob, grounding_track, _legacy_grounding_gate)
        _patient_store[bundle.patient_id] = blob
    _team_store.ensure_episode(
        patient_id=bundle.patient_id,
        procedure_type=structured_data.get("procedure_name", ""),
        clinic_code=(_patient_store[bundle.patient_id] or {}).get("clinic_code") or clinic_code_m or "",
        resource_code=(_patient_store[bundle.patient_id] or {}).get("resource_code") or "",
        health_system_id=(_patient_store[bundle.patient_id] or {}).get("health_system_id") or health_system_id,
    )
    _persist_demo_patient_store()
    _pe = _patient_store.get(bundle.patient_id) or {}
    background_tasks.add_task(
        _send_sms,
        phone=bundle.phone_number,
        name=bundle.patient_name,
        entry_url=_patient_entry_url(),
        clinic_code=(_pe.get("clinic_code") or ""),
        resource_code=(_pe.get("resource_code") or ""),
    )
    return ProcessResponse(
        patient_id=bundle.patient_id, pipeline_type=pipeline_type,
        dashboard_url=dashboard_url, voice_audio_url=audio_url,
        battlecard_html=battlecard_html, avatar_url=avatar.get("conversation_url"),
    )


# ─── Resource Endpoints ───────────────────────────────────────
@app.get("/api/patient/{patient_id}/resources")
async def get_patient_resources(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Return the two-resource sets (diagnosis + treatment) if available."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    resources = _patient_store[patient_id].get("resources")
    if not resources:
        raise HTTPException(status_code=404, detail="No split resources generated for this patient")

    def clean_html(html):
        """Strip markdown code fences if Claude wrapped the HTML."""
        h = (html or "").strip()
        if h.startswith("```"):
            h = h.split("\n", 1)[1] if "\n" in h else h[3:]
            if h.endswith("```"):
                h = h[:-3].strip()
        return h

    for key in ("diagnosis", "treatment"):
        if key in resources and "battlecard_html" in resources[key]:
            resources[key]["battlecard_html"] = clean_html(resources[key]["battlecard_html"])

    _persist_demo_patient_store()
    return resources


async def _ensure_preop_voice_audio(
    store: Dict[str, Any],
    patient_id: str,
    *,
    team_store: Any = None,
) -> Optional[str]:
    """Return a playable pre-op audio URL, synthesizing on demand when needed."""
    from pathlib import Path

    resources = store.get("resources") or {}
    preop = resources.get("preop") if isinstance(resources, dict) else {}
    if not isinstance(preop, dict):
        preop = {}

    for cached in (preop.get("voice_audio_url"), store.get("voice_audio_url")):
        if not cached:
            continue
        filename = cached.split("/audio/")[-1]
        if Path(f"/tmp/{filename}").exists():
            return f"/audio/{filename}"
    if not preop.get("voice_audio_url"):
        store["voice_audio_url"] = None

    voice_script = (preop.get("voice_script") or store.get("voice_script") or "").strip()
    if not voice_script:
        return None

    pending_tracks = store.get("grounding_pending_tracks")
    if isinstance(pending_tracks, list) and "pre_op" in pending_tracks:
        return None

    if team_store is not None:
        gate, audio_url = await synthesize_script(
            patient_id=patient_id,
            structured_data=dict(store.get("structured_data") or {}),
            script=voice_script,
            track="pre_op",
            team_store=team_store,
            audio_id=f"{patient_id}_preop",
            regenerate_fn=None,
            patient_blob=store,
        )
        voice_script = gate.script
        if not gate.synthesize:
            return None
    else:
        return None

    store["voice_script"] = voice_script
    store["voice_audio_url"] = audio_url
    if not isinstance(resources, dict):
        resources = {}
    preop = dict(preop)
    preop["voice_audio_url"] = audio_url
    preop["voice_script"] = voice_script
    resources["preop"] = preop
    store["resources"] = resources
    return audio_url


@app.get("/api/patient/{patient_id}/preop-audio")
async def get_preop_audio(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Lazy pre-op TTS — used when battlecard exists but audio was not cached."""
    _assert_clinical_staff_can_access_patient(patient_id, staff)
    store = _patient_store[patient_id]
    audio_url = await _ensure_preop_voice_audio(store, patient_id, team_store=_team_store)
    if not audio_url:
        resources = store.get("resources") or {}
        preop = resources.get("preop") if isinstance(resources, dict) else {}
        voice_script = ""
        if isinstance(preop, dict):
            voice_script = (preop.get("voice_script") or store.get("voice_script") or "").strip()
        if not voice_script:
            raise HTTPException(status_code=422, detail="No pre-op voice script available for this patient")
        raise HTTPException(status_code=503, detail="Voice synthesis unavailable — check ELEVENLABS_API_KEY")
    _persist_demo_patient_store()
    return {"audio_url": audio_url}


@app.get("/api/patient/{patient_id}/audio")
async def get_patient_audio(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    store = _patient_store[patient_id]
    cached_url = store.get("voice_audio_url")
    if cached_url:
        filename = cached_url.split("/audio/")[-1]
        from pathlib import Path
        if Path(f"/tmp/{filename}").exists():
            return {"audio_url": f"/audio/{filename}"}
    voice_script = store.get("voice_script")
    if not voice_script:
        raise HTTPException(status_code=422, detail="No voice script available for this patient")

    grounding_track = "pre_op" if (store.get("pipeline_type") or "").lower() == "pre_op" else "post_op_treatment"
    gate, audio_url = await synthesize_script(
        patient_id=patient_id,
        structured_data=dict(store.get("structured_data") or {}),
        script=voice_script,
        track=grounding_track,
        team_store=_team_store,
        audio_id=patient_id,
        regenerate_fn=None,
        patient_blob=store,
    )
    store["voice_script"] = gate.script
    if not audio_url:
        raise HTTPException(status_code=503, detail="ElevenLabs not configured — set ELEVENLABS_API_KEY")
    store["voice_audio_url"] = audio_url
    _persist_demo_patient_store()
    return {"audio_url": audio_url}


@app.get("/api/patient/{patient_id}/battlecard")
async def get_battlecard(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    return {"html": _patient_store[patient_id]["battlecard_html"]}


@app.get("/api/patient/{patient_id}/config")
async def get_dashboard_config(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    d = _patient_store[patient_id]
    phone_team = d.get("office_phone") or os.getenv("CARE_TEAM_PHONE", "")
    return {
        "id":            patient_id,
        "name":          d["name"],
        "pipelineType":  d["pipeline_type"],
        "procedure":     d["structured_data"].get("procedure_name", ""),
        "visitDate":     d["structured_data"].get("procedure_date", ""),
        "audioUrl":      d["voice_audio_url"],
        "tavusUrl":      d["avatar_url"],
        "phoneTeam":     phone_team,
        "hasResources":  d.get("resources") is not None,
    }


@app.get("/api/patient/{patient_id}/discharge")
async def get_discharge_instructions(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    d = _patient_store[patient_id]
    return {
        "structured_data": d["structured_data"],
        "voice_script": d.get("voice_script", ""),
    }


@app.get("/patient/{patient_id}/digital-care-companion", response_class=HTMLResponse)
@app.get("/patient/{patient_id}/voice", response_class=HTMLResponse)
async def digital_care_companion_page(
    patient_id: str,
    request: Request,
    k: Optional[str] = None,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Serves the Digital Care Companion conversation interface."""
    _redir = _patient_page_entry(request, patient_id, k, staff)
    if _redir is not None:
        return _redir

    d = _patient_store[patient_id]
    patient_json = json.dumps({
        "id":        patient_id,
        "name":      d["name"],
        "firstName": d["name"].split()[0],
        "procedure": d["structured_data"].get("procedure_name", ""),
    })

    html_path = os.path.join(os.path.dirname(__file__), "../frontend/voice-avatar.html")
    with open(html_path) as f:
        html = f.read()

    inject = f"<script>window.__PATIENT__ = {patient_json};</script>"
    html = html.replace("</head>", f"{inject}\n</head>")
    return HTMLResponse(content=html)


@app.get("/patient/{patient_id}/pre-op", response_class=HTMLResponse)
async def pre_op_page(
    patient_id: str,
    request: Request,
    k: Optional[str] = None,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Serves the pre-operative preparation page."""
    _redir = _patient_page_entry(request, patient_id, k, staff)
    if _redir is not None:
        return _redir
    d = _patient_store[patient_id]
    patient_json = json.dumps(
        {
            "id": patient_id,
            "name": d["name"],
            "firstName": d["name"].split()[0],
            "procedure": d["structured_data"].get("procedure_name", ""),
            "phoneTeam": d.get("office_phone") or os.getenv("CARE_TEAM_PHONE", ""),
            "preop_resource": (d.get("resources") or {}).get("preop"),
        }
    )
    html_path = os.path.join(os.path.dirname(__file__), "../frontend/pre-op.html")
    with open(html_path) as f:
        html = f.read()
    inject = f"<script>window.__PATIENT__ = {patient_json};</script>"
    html = html.replace("</head>", f"{inject}\n</head>")
    return HTMLResponse(content=html)


@app.get("/patient/{patient_id}", response_class=HTMLResponse)
async def patient_dashboard(
    patient_id: str,
    request: Request,
    k: Optional[str] = None,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _redir = _patient_page_entry(request, patient_id, k, staff)
    if _redir is not None:
        return _redir

    d = _patient_store[patient_id]
    if (d.get("pipeline_type") or "").lower() == "pre_op":
        return RedirectResponse(url=f"/patient/{patient_id}/pre-op", status_code=302)

    def clean_html(html):
        h = (html or "").strip()
        if h.startswith("```"):
            h = h.split("\n", 1)[1] if "\n" in h else h[3:]
            if h.endswith("```"):
                h = h[:-3].strip()
        return h

    resources = d.get("resources") or {}
    resources_json = None
    if resources.get("diagnosis") and resources.get("treatment"):
        resources_json = {
            "diagnosis": {
                "voice_audio_url": resources["diagnosis"].get("voice_audio_url"),
                "battlecard_html": clean_html(resources["diagnosis"].get("battlecard_html", "")),
            },
            "treatment": {
                "voice_audio_url": resources["treatment"].get("voice_audio_url"),
                "battlecard_html": clean_html(resources["treatment"].get("battlecard_html", "")),
            },
        }

    phone_team = d.get("office_phone") or os.getenv("CARE_TEAM_PHONE", "")
    patient_json = {
        "id":           patient_id,
        "name":         d["name"],
        "firstName":    d["name"].split()[0],
        "pipelineType": d["pipeline_type"],
        "procedure":    d["structured_data"].get("procedure_name", ""),
        "visitDate":    d["structured_data"].get("procedure_date", ""),
        "audioUrl":     d.get("voice_audio_url") or None,
        "tavusUrl":     d.get("avatar_url") or None,
        "phoneTeam":    phone_team,
        "hasResources": resources_json is not None,
        "resources":    resources_json,
    }

    patient_json_str = json.dumps(patient_json)

    html_path = os.path.join(os.path.dirname(__file__), "../frontend/index.html")
    with open(html_path) as f:
        html = f.read()

    html = _apply_cache_bust(html)
    inject = f"<script>window.__PATIENT__ = {patient_json_str};</script>"
    html = html.replace("</head>", f"{inject}\n</head>")

    voice_url = f"/patient/{patient_id}/digital-care-companion"
    html = html.replace('id="voiceAvatarBtn" href="#"', f'id="voiceAvatarBtn" href="{voice_url}"')

    return HTMLResponse(content=html)


@app.post("/api/digital-care-companion/chat", response_model=ChatResponse)
@app.post("/api/avatar/chat", response_model=ChatResponse)
async def digital_care_companion_chat(
    req: ChatRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if req.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(req.patient_id, staff)

    patient_data = _patient_store[req.patient_id]
    _team_store.log_event(patient_id=req.patient_id, event_type="avatar_chat", payload={"source": "chat"})
    escalation = await _classify_and_create_escalation(
        patient_id=req.patient_id,
        message=req.message,
        conversation_history=req.conversation_history,
        source="chat",
    )
    if escalation:
        return ChatResponse(
            response=escalation["response"],
            patient_id=req.patient_id,
            audio_url=None,
            escalation={
                "tier": escalation["tier"],
                "escalation_id": escalation["escalation_id"],
                "requires_consent": escalation["requires_consent"],
            },
        )

    try:
        from prompts.avatar import build_avatar_system_prompt

        clean_data = {k: v for k, v in patient_data["structured_data"].items()
                      if k != "_raw_clinical"}
        system_prompt = build_avatar_system_prompt(clean_data)
        if patient_data.get("pipeline_type") == "pre_op":
            prep_notes = clean_data.get("pre_op_instructions") or clean_data.get("clinical_notes") or ""
            system_prompt += (
                "\n\n## Pre-Op Scope Override\n"
                "This patient is in a PRE-OP episode. Only discuss surgical preparation, including medication hold/start instructions, dietary restrictions, and activity restrictions.\n"
                "Do not provide post-op recovery advice unless explicitly present in pre-op notes.\n"
                f"Primary preparation notes:\n{prep_notes}\n"
            )

        messages = [{"role": m["role"], "content": m["content"]}
                    for m in req.conversation_history]
        messages.append({"role": "user", "content": req.message})

        print(f"[digital_care_companion_chat] Sending to Claude for patient {req.patient_id}...")
        response, _ = call_llm_sync(
            role="care_companion_chat",
            prompt_id="care_companion_chat",
            patient_id=req.patient_id,
            system=system_prompt,
            messages=messages,
        )

        reply_text = first_text(response)
        print(f"[digital_care_companion_chat] Got response ({len(reply_text)} chars), synthesizing audio...")
        _pn = (patient_data.get("name") or "")  # PRD-4: scrub name if ElevenLabs has no BAA
        _deid = ([_pn] + _pn.split()) if _pn else None
        audio_url = await ElevenLabsClient().synthesize(reply_text, f"{req.patient_id}_chat", deid_terms=_deid)  # gated-synth-exempt: live chat reply, not generated patient-education script

        return ChatResponse(response=reply_text, patient_id=req.patient_id, audio_url=audio_url, escalation=None)

    except Exception as exc:
        import traceback
        print(f"[digital_care_companion_chat] ERROR: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return ChatResponse(
            response="I'm having a brief technical issue. For urgent questions, please call your care team directly.",
            patient_id=req.patient_id, audio_url=None,
        )


def _next_missing_lens(session: Dict[str, Any]) -> Optional[str]:
    for lens in ("pattern", "exposure", "anatomy", "root"):
        if not (session.get("answers", {}).get(lens) or "").strip():
            return lens
    return None


def _lens_question(lens: str, specialty: str) -> str:
    _ = specialty  # retained for API compatibility; specialty prompts are not shown to patients here.
    prompts = {
        "pattern": "When did your symptoms start, how have they changed, and what makes them better or worse?",
        "exposure": "What does your daily activity look like (occupation, exercise, repetitive movement, substance use, prior surgeries)?",
        "anatomy": "Where exactly is the issue, does it radiate anywhere, how severe is it, and what does it stop you from doing?",
        "root": "Please share age, sex, ethnicity, BMI (if known), family history, current medications, allergies, conditions, and prior anesthesia reactions.",
    }
    return (prompts.get(lens, "") or "").strip()


def _with_disclaimer(text: str) -> str:
    return f"{text}\n\n{AI_DISCLAIMER}"


def _resolve_notif_doctor_id(
    doctor_id: str,
    staff: Optional[StaffContext],
    user: Optional[UserOut],
) -> str:
    if doctor_id == "me":
        if staff and staff.email:
            return f"tenant:{staff.email.lower().strip()}"
        if user and user.email:
            return f"doctor:{user.email.lower().strip()}"
        return "doctor:default"
    return doctor_id


@app.post("/api/intake-forms/start-interview")
async def intake_forms_start_interview(
    body: IntakeFormsStartInterviewBody,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if not ENABLE_PREOP_INTAKE_BOT_V2:
        raise HTTPException(status_code=503, detail="Pre-op intake bot v2 is disabled.")
    patient_id = body.patientId
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    existing = _team_store.get_latest_intake_form_for_patient(patient_id)
    if existing and existing.get("status") == "INTERVIEW_IN_PROGRESS":
        return {"intakeFormId": existing["id"], "voiceSessionConfig": None, "status": existing.get("status")}
    form_id = str(uuid.uuid4())
    patient = _patient_store.get(patient_id) or {}
    empty = parseTranscriptToFormData([], patient, _patient_prep_document(patient_id))
    form_data = empty.get("formData") or {}
    apply_health_system_facility_name(form_data, _facility_display_name_for_patient(patient_id))
    created = _team_store.create_intake_form(
        intake_form_id=form_id,
        patient_id=patient_id,
        surgery_id=body.surgeryId,
        status="INTERVIEW_IN_PROGRESS",
        form_data=form_data,
    )
    return {"intakeFormId": created["id"], "voiceSessionConfig": None, "status": created.get("status")}


def _patient_intake_context_summary(patient_id: str) -> str:
    p = _patient_store.get(patient_id) or {}
    sd = p.get("structured_data") or {}
    lines = [
        f"Legal name: {p.get('name', '')}",
        f"Procedure (scheduled): {sd.get('procedure_name', '')}",
        f"Procedure date: {sd.get('procedure_date', '')}",
        f"Surgeon: {sd.get('surgeon_name', '')}",
        f"Laterality: {sd.get('laterality', '')}",
        f"Pre-op diagnosis (record): {sd.get('pre_op_diagnosis', '')}",
        f"Facility (record): {sd.get('facility', '')}",
        f"Phone: {p.get('phone', '')}",
    ]
    return "\n".join(lines)


def _migrate_interview_state(istate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Backfill sectionInterviewComplete / firstPassComplete for older saved interview_state JSON."""
    istate = dict(istate or {})
    sic_raw = istate.get("sectionInterviewComplete")
    sic: Dict[str, Any] = dict(sic_raw) if isinstance(sic_raw, dict) else {}
    cs = {int(x) for x in (istate.get("completedSections") or []) if str(x).isdigit()}
    if not sic and cs:
        for n in range(3, 11):
            if n in cs:
                sic[str(n)] = True
    istate["sectionInterviewComplete"] = sic
    if istate.get("firstPassComplete") is None:
        istate["firstPassComplete"] = bool(cs.issuperset(set(range(1, 12))))
    return istate


def _prior_intake_sections_context(form_data: Dict[str, Any], before_section: int) -> str:
    chunks: List[str] = []
    for sn in range(1, max(1, before_section)):
        key = INTAKE_SECTION_BY_INDEX.get(sn)
        if not key:
            continue
        blob = (form_data or {}).get(key)
        if not blob:
            continue
        chunks.append(f"## Prior section {sn} ({key})\n{json.dumps(blob, default=str)}")
    return "\n\n".join(chunks)[:24000]


def _coalesce_intake_form_response(form: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not form:
        return None
    out = dict(form)
    ist = out.get("interview_state")
    if isinstance(ist, dict):
        out["interview_state"] = _migrate_interview_state(ist)
    return out


def _all_interview_messages_for_flags(interview_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_sec = interview_state.get("messagesBySection") or {}
    out: List[Dict[str, Any]] = []
    for k in sorted(by_sec.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
        for m in by_sec.get(k) or []:
            out.append(m)
    return out


@app.post("/api/intake-forms/{intake_form_id}/interview/section-message")
async def intake_forms_interview_section_message(
    intake_form_id: str,
    body: IntakeSectionMessageBody,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if staff:
        raise HTTPException(status_code=403, detail="Only the patient can send intake interview messages here.")
    if not ENABLE_PREOP_INTAKE_BOT_V2:
        raise HTTPException(status_code=503, detail="Pre-op intake bot v2 is disabled.")
    form = _team_store.get_intake_form(intake_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intake form not found")
    patient_id = form.get("patient_id") or ""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    if (form.get("status") or "") not in ("INTERVIEW_IN_PROGRESS",):
        raise HTTPException(status_code=400, detail="Intake interview is not active for this form.")

    section = int(body.section)
    if section < 3 or section > 10:
        raise HTTPException(status_code=400, detail="This endpoint handles sections 3 through 10 only.")
    if not (body.message or "").strip():
        raise HTTPException(status_code=400, detail="Message is required.")

    istate = _migrate_interview_state(dict(form.get("interview_state") or {}))
    completed = {int(x) for x in (istate.get("completedSections") or []) if str(x).isdigit()}
    need_prior = all(i in completed for i in range(1, section))
    if not need_prior:
        raise HTTPException(
            status_code=400,
            detail=f"Complete sections 1 through {section - 1} before continuing here.",
        )

    sic = dict(istate.get("sectionInterviewComplete") or {})
    if sic.get(str(section)):
        raise HTTPException(
            status_code=400,
            detail="Interview for this section is finished. Review your answers on the form and confirm, or use Redo interview.",
        )

    section_key = INTAKE_SECTION_BY_INDEX[section]
    form_data = dict(form.get("form_data") or {})
    current_section_payload = form_data.get(section_key) or {}

    by_sec = dict(istate.get("messagesBySection") or {})
    sk = str(section)
    thread = list(by_sec.get(sk) or [])
    thread.append(
        {
            "role": "patient",
            "text": body.message.strip(),
            "timestamp": datetime.utcnow().replace(microsecond=0).isoformat(),
        }
    )

    patient = _patient_store.get(patient_id) or {}
    prior_blob = _prior_intake_sections_context(form_data, section)
    reply, updates, section_complete, err = run_intake_section_turn(
        section_num=section,
        patient_name=str(patient.get("name") or "there"),
        patient_context=_patient_intake_context_summary(patient_id),
        prior_sections_text=prior_blob,
        user_message=body.message.strip(),
        conversation_history=body.conversationHistory or [],
        current_form_section=current_section_payload,
    )
    thread.append(
        {
            "role": "assistant",
            "text": reply,
            "timestamp": datetime.utcnow().replace(microsecond=0).isoformat(),
        }
    )
    by_sec[sk] = thread
    istate["messagesBySection"] = by_sec
    istate["activeSection"] = section

    merge_intake_ai_patch(section_key, updates, form_data)

    all_msgs = _all_interview_messages_for_flags(istate)
    red_flags = accumulate_red_flags_from_section_messages(all_msgs)

    if section_complete:
        sic[str(section)] = True
    istate["sectionInterviewComplete"] = sic

    _team_store.update_intake_form_payload(
        intake_form_id,
        form_data=form_data,
        red_flags=red_flags,
        conflicts=list(form.get("conflicts") or []),
        interview_state=istate,
    )

    return {
        "reply": reply,
        "sectionComplete": section_complete,
        "formData": form_data,
        "redFlags": red_flags,
        "interviewState": istate,
        "error": err or None,
    }


@app.post("/api/intake-forms/{intake_form_id}/interview/complete-section")
async def intake_forms_interview_complete_section(
    intake_form_id: str,
    body: IntakeCompleteSectionBody,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if staff:
        raise HTTPException(status_code=403, detail="Only the patient can complete intake sections here.")
    if not ENABLE_PREOP_INTAKE_BOT_V2:
        raise HTTPException(status_code=503, detail="Pre-op intake bot v2 is disabled.")
    form = _team_store.get_intake_form(intake_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intake form not found")
    patient_id = form.get("patient_id") or ""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    if (form.get("status") or "") not in ("INTERVIEW_IN_PROGRESS",):
        raise HTTPException(status_code=400, detail="Intake interview is not active for this form.")

    section = int(body.section)
    if section < 1 or section > 11:
        raise HTTPException(status_code=400, detail="Invalid section.")

    istate = _migrate_interview_state(dict(form.get("interview_state") or {}))
    completed = {int(x) for x in (istate.get("completedSections") or []) if str(x).isdigit()}
    sic = dict(istate.get("sectionInterviewComplete") or {})

    if section > 1 and not all(i in completed for i in range(1, section)):
        raise HTTPException(
            status_code=400,
            detail=f"Complete section {section - 1} before marking section {section} done.",
        )

    form_data = dict(form.get("form_data") or {})

    if section in completed:
        fresh = _team_store.get_intake_form(intake_form_id) or {}
        return {
            "ok": True,
            "interviewState": _migrate_interview_state(dict((fresh.get("interview_state") or {}))),
            "formData": fresh.get("form_data") or form_data,
        }

    if 3 <= section <= 10:
        if body.confirmReview:
            if not sic.get(str(section)):
                raise HTTPException(
                    status_code=400,
                    detail="Please finish the interview for this section before confirming the form.",
                )
            completed.add(section)
            istate["completedSections"] = sorted(completed)
            istate["activeSection"] = min(section + 1, 11)
            cs_full = {int(x) for x in (istate.get("completedSections") or []) if str(x).isdigit()}
            istate["firstPassComplete"] = bool(cs_full.issuperset(set(range(1, 12))))
            _team_store.update_intake_form_payload(
                intake_form_id,
                form_data=form_data,
                red_flags=list(form.get("red_flags") or []),
                conflicts=list(form.get("conflicts") or []),
                interview_state=istate,
            )
            return {"ok": True, "interviewState": istate, "formData": form_data}
        if body.forceComplete:
            msgs = (istate.get("messagesBySection") or {}).get(str(section)) or []
            if len(msgs) < 2 and not sic.get(str(section)):
                raise HTTPException(
                    status_code=400,
                    detail="Have at least one exchange with the assistant before ending this section early.",
                )
            sic[str(section)] = True
            istate["sectionInterviewComplete"] = sic
            istate["activeSection"] = section
            _team_store.update_intake_form_payload(
                intake_form_id,
                form_data=form_data,
                red_flags=list(form.get("red_flags") or []),
                conflicts=list(form.get("conflicts") or []),
                interview_state=istate,
            )
            return {"ok": True, "interviewState": istate, "formData": form_data}
        raise HTTPException(
            status_code=400,
            detail="For sections 3–10 use forceComplete to end the interview early, or confirmReview after reviewing your answers.",
        )

    if section == 11:
        ack = body.acknowledgements or {}
        sec_key = INTAKE_SECTION_BY_INDEX[11]
        for fk, val in ack.items():
            if fk in (form_data.get(sec_key) or {}):
                merge_intake_ai_patch(sec_key, {fk: val}, form_data)
        info = (form_data.get(sec_key) or {}).get("informationAccurate") or {}
        if info.get("value") is not True:
            raise HTTPException(
                status_code=400,
                detail="Please confirm that your information is accurate before finishing this section.",
            )
        cd_node = (form_data.get(sec_key) or {}).get("completionDate") or {}
        if isinstance(cd_node, dict):
            cd_node["value"] = datetime.utcnow().replace(microsecond=0).date().isoformat()
            cd_node["source"] = "system"
            form_data[sec_key]["completionDate"] = cd_node

    if section == 1:
        s1 = form_data.get("section1_demographics") or {}
        required_keys = (
            "fullLegalName",
            "dateOfBirth",
            "phonePrimary",
            "emergencyContactName",
            "emergencyContactPhone",
        )
        for rk in required_keys:
            node = s1.get(rk) or {}
            val = node.get("value") if isinstance(node, dict) else None
            if val is None or (isinstance(val, str) and not val.strip()):
                raise HTTPException(
                    status_code=400,
                    detail=f"Please fill in {rk} before continuing.",
                )

    completed.add(section)
    istate["completedSections"] = sorted(completed)
    istate["activeSection"] = min(section + 1, 11)
    cs_done = {int(x) for x in (istate.get("completedSections") or []) if str(x).isdigit()}
    istate["firstPassComplete"] = bool(cs_done.issuperset(set(range(1, 12))))

    _team_store.update_intake_form_payload(
        intake_form_id,
        form_data=form_data,
        red_flags=list(form.get("red_flags") or []),
        conflicts=list(form.get("conflicts") or []),
        interview_state=istate,
    )
    return {"ok": True, "interviewState": istate, "formData": form_data}


@app.post("/api/intake-forms/{intake_form_id}/interview/reset-section")
async def intake_forms_interview_reset_section(
    intake_form_id: str,
    body: IntakeResetSectionBody,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if staff:
        raise HTTPException(status_code=403, detail="Only the patient can reset an intake section here.")
    if not ENABLE_PREOP_INTAKE_BOT_V2:
        raise HTTPException(status_code=503, detail="Pre-op intake bot v2 is disabled.")
    form = _team_store.get_intake_form(intake_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intake form not found")
    patient_id = form.get("patient_id") or ""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    if (form.get("status") or "") not in ("INTERVIEW_IN_PROGRESS",):
        raise HTTPException(status_code=400, detail="Intake interview is not active for this form.")
    section = int(body.section)
    if section < 3 or section > 10:
        raise HTTPException(status_code=400, detail="Only sections 3 through 10 can be reset for redo.")

    istate = _migrate_interview_state(dict(form.get("interview_state") or {}))
    form_data = dict(form.get("form_data") or {})
    reset_intake_section_for_interview_redo(form_data, section)

    by_sec = dict(istate.get("messagesBySection") or {})
    by_sec[str(section)] = []
    istate["messagesBySection"] = by_sec
    sic = dict(istate.get("sectionInterviewComplete") or {})
    sic.pop(str(section), None)
    istate["sectionInterviewComplete"] = sic
    completed = {int(x) for x in (istate.get("completedSections") or []) if str(x).isdigit()}
    completed.discard(section)
    istate["completedSections"] = sorted(completed)
    istate["activeSection"] = section
    cs_done = {int(x) for x in (istate.get("completedSections") or []) if str(x).isdigit()}
    istate["firstPassComplete"] = bool(cs_done.issuperset(set(range(1, 12))))

    _team_store.update_intake_form_payload(
        intake_form_id,
        form_data=form_data,
        red_flags=list(form.get("red_flags") or []),
        conflicts=list(form.get("conflicts") or []),
        interview_state=istate,
    )
    return {"ok": True, "interviewState": istate, "formData": form_data}


@app.post("/api/intake-forms/{intake_form_id}/complete-interview")
async def intake_forms_complete_interview(
    intake_form_id: str,
    body: IntakeFormsCompleteInterviewBody,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if staff:
        raise HTTPException(status_code=403, detail="Only the patient can finalize the intake interview.")
    if not ENABLE_PREOP_INTAKE_BOT_V2:
        raise HTTPException(status_code=503, detail="Pre-op intake bot v2 is disabled.")
    form = _team_store.get_intake_form(intake_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intake form not found")
    patient_id = form.get("patient_id") or ""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)

    istate = form.get("interview_state") or {}
    done = set(int(s) for s in (istate.get("completedSections") or []) if str(s).isdigit())
    if not set(range(1, 12)).issubset(done):
        raise HTTPException(
            status_code=400,
            detail="Complete all 11 intake sections before finalizing the interview.",
        )

    transcript_id = str(uuid.uuid4())
    parsed = parseTranscriptToFormData(
        body.transcript or [],
        _patient_store.get(patient_id) or {},
        _patient_prep_document(patient_id),
    )
    _team_store.save_interview_transcript(
        transcript_id=transcript_id,
        intake_form_id=intake_form_id,
        full_transcript=body.transcript or [],
        audio_blob_url=body.audioBlobUrl,
        duration=body.duration,
        parsed_data=parsed,
    )
    now = datetime.utcnow().replace(microsecond=0).isoformat()
    fd = form.get("form_data") or {}
    merged_red: List[Dict[str, Any]] = list(form.get("red_flags") or [])
    seen_flags = {str(x.get("flag")) for x in merged_red if x.get("flag")}
    for r in parsed.get("redFlags") or []:
        key = str(r.get("flag") or "")
        if key and key not in seen_flags:
            merged_red.append(r)
            seen_flags.add(key)
    _team_store.update_intake_form_payload(
        intake_form_id,
        form_data=fd,
        red_flags=merged_red,
        conflicts=list(form.get("conflicts") or []),
        status="INTERVIEW_COMPLETE",
        completed_at=now,
        interview_transcript_id=transcript_id,
    )
    _create_intake_notifications(
        patient_id,
        intake_form_id,
        "FORM_COMPLETED",
        f"Intake interview completed for patient {(_patient_store.get(patient_id) or {}).get('name', patient_id)}.",
    )
    prior_red_ct = len(form.get("red_flags") or [])
    if len(merged_red) > prior_red_ct:
        _create_intake_notifications(
            patient_id,
            intake_form_id,
            "RED_FLAG_DETECTED",
            f"Red flag detected in intake for patient {(_patient_store.get(patient_id) or {}).get('name', patient_id)}.",
        )
    _team_store.log_event(
        patient_id=patient_id,
        event_type="preop_intake_submitted",
        payload={"intake_form_id": intake_form_id, "status": "INTERVIEW_COMPLETE"},
    )
    return {
        "intakeFormId": intake_form_id,
        "formData": fd,
        "redFlags": merged_red,
        "conflicts": list(form.get("conflicts") or []),
    }


@app.get("/api/intake-forms/{intake_form_id}")
async def intake_forms_get(
    intake_form_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    form = _team_store.get_intake_form(intake_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intake form not found")
    patient_id = form.get("patient_id") or ""
    if patient_id and patient_id in _patient_store:
        _assert_staff_can_access_patient(patient_id, staff)
    editable = not bool(staff or user)
    return {
        "intakeForm": _coalesce_intake_form_response(form),
        "readOnly": not editable,
        "editable": editable,
    }


@app.get("/api/intake-forms/latest/{patient_id}")
async def intake_forms_latest_for_patient(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    latest = _team_store.get_latest_intake_form_for_patient(patient_id)
    return {"patient_id": patient_id, "intake_form": _coalesce_intake_form_response(latest)}


@app.patch("/api/intake-forms/{intake_form_id}")
async def intake_forms_patch(
    intake_form_id: str,
    body: IntakeFormsPatchBody,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    DOCTOR_SECTION2_FIELDS = frozenset(
        {"procedureCPTCodes", "surgicalSite", "anesthesiologist", "estimatedDuration"},
    )
    if staff:
        if body.section != "section2_surgicalInfo" or body.field not in DOCTOR_SECTION2_FIELDS:
            raise HTTPException(
                status_code=403,
                detail="Staff may only edit surgical info fields reserved for the care team.",
            )
    form = _team_store.get_intake_form(intake_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intake form not found")
    form_data = form.get("form_data") or {}
    section = body.section
    field = body.field
    if section not in form_data or field not in (form_data.get(section) or {}):
        raise HTTPException(status_code=400, detail="Invalid section/field")
    payload = form_data[section][field]
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid field payload")
    prev = payload.get("value")
    payload["value"] = body.value
    if staff:
        # NOTE: "doctor" here is a clinical-source provenance label (i.e. "this
        # field came from a clinician's edit"), NOT a pass-4 role token. Keep
        # the literal so historical intake forms render correctly in the UI.
        payload["source"] = "doctor"
        editor = f"DOCTOR:{staff.email}"
    else:
        payload["source"] = "patient_edited"
        payload["editedAt"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        editor = "PATIENT"
    form_data[section][field] = payload
    status = form.get("status") or "INTERVIEW_COMPLETE"
    if status == "SUBMITTED":
        status = "UPDATED"
    _team_store.update_intake_form_payload(
        intake_form_id,
        form_data=form_data,
        red_flags=form.get("red_flags") or [],
        conflicts=form.get("conflicts") or [],
        status=status,
    )
    _team_store.create_intake_form_edit(
        edit_id=str(uuid.uuid4()),
        intake_form_id=intake_form_id,
        edited_by=editor,
        section_name=section,
        field_key=field,
        previous_value=prev,
        new_value=body.value,
    )
    if status == "UPDATED" and not staff:
        patient_id = form.get("patient_id") or ""
        _create_intake_notifications(
            patient_id,
            intake_form_id,
            "PATIENT_EDITED",
            f"Patient updated intake field {section}.{field} after submission.",
        )
    return {"ok": True, "intakeFormId": intake_form_id, "status": status}


@app.post("/api/intake-forms/{intake_form_id}/submit")
async def intake_forms_submit(
    intake_form_id: str,
    body: IntakeFormsSubmitBody,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _ = body  # keeps request contract explicit for future extension
    if staff:
        raise HTTPException(status_code=403, detail="Doctors cannot submit intake forms.")
    form = _team_store.get_intake_form(intake_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intake form not found")
    now = datetime.utcnow().replace(microsecond=0).isoformat()
    _team_store.update_intake_form_status(
        intake_form_id,
        status="SUBMITTED",
        submitted_at=now,
    )
    patient_id = form.get("patient_id") or ""
    _create_intake_notifications(
        patient_id,
        intake_form_id,
        "FORM_COMPLETED",
        "Patient submitted final intake form.",
    )
    return {"ok": True, "intakeFormId": intake_form_id, "status": "SUBMITTED"}


@app.get("/api/intake-forms/{intake_form_id}/edit-history")
async def intake_forms_edit_history(
    intake_form_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    form = _team_store.get_intake_form(intake_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intake form not found")
    patient_id = form.get("patient_id") or ""
    if patient_id and patient_id in _patient_store:
        _assert_staff_can_access_patient(patient_id, staff)
    rows = _team_store.list_intake_form_edits(intake_form_id)
    return {"intakeFormId": intake_form_id, "edits": rows}


@app.get("/api/doctors/{doctor_id}/notifications")
async def intake_notifications_list(
    doctor_id: str,
    notif_type: Optional[str] = None,
    unread_only: bool = False,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    resolved_doctor_id = _resolve_notif_doctor_id(doctor_id, staff, user)
    rows = _team_store.list_intake_notifications(
        resolved_doctor_id,
        unread_only=unread_only,
        notif_type=notif_type,
    )
    if staff and staff.source == "tenant" and staff.tenant_id:
        unread_patients = _team_store.list_unread_care_team_reply_patients(staff.tenant_id)
        for entry in unread_patients:
            pid = entry.get("patient_id")
            pdata = _patient_store.get(pid) or {}
            pname = pdata.get("name") or pid
            rows.append(
                {
                    "id": f"ctm-{pid}",
                    "doctor_id": resolved_doctor_id,
                    "intake_form_id": pid,
                    "type": "care_team_reply",
                    "message": f"New message from {pname}",
                    "patient_id": pid,
                    "is_read": 0,
                    "read": False,
                    "created_at": entry.get("latest_at") or "",
                }
            )
        if unread_only:
            rows = [r for r in rows if not r.get("read") and not r.get("is_read")]
    return {"doctorId": resolved_doctor_id, "notifications": rows}


@app.patch("/api/doctors/{doctor_id}/notifications/{notif_id}/read")
async def intake_notifications_mark_read(
    doctor_id: str,
    notif_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    resolved_doctor_id = _resolve_notif_doctor_id(doctor_id, staff, user)
    if notif_id.startswith("ctm-"):
        pid = notif_id.removeprefix("ctm-")
        if pid in _patient_store:
            _team_store.mark_care_team_thread_read(pid, by="care_team")
            return {"ok": True}
    ok = _team_store.mark_intake_notification_read(resolved_doctor_id, notif_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"ok": True}


@app.post("/api/pre-op/intake/start")
async def preop_intake_start(
    body: IntakeStartRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if body.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(body.patient_id, staff)
    patient = _patient_store[body.patient_id]
    procedure = (patient.get("structured_data") or {}).get("procedure_name", "")
    specialty = patient.get("specialty") or _specialty_from_procedure(procedure)
    session = {
        "patient_id": body.patient_id,
        "specialty": specialty,
        "answers": {"pattern": "", "exposure": "", "anatomy": "", "root": ""},
        "last_lens": "pattern",
    }
    _team_store.save_preop_intake_session(body.patient_id, session)
    question = _lens_question("pattern", specialty)
    return {
        "patient_id": body.patient_id,
        "response": "Hi, I am your pre-op Digital Care Companion. I will ask one question at a time to complete your surgical intake.\n" + question,
        "interview_complete": False,
    }


@app.post("/api/pre-op/intake/answer")
async def preop_intake_answer(
    body: IntakeAnswerRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if body.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(body.patient_id, staff)
    patient = _patient_store[body.patient_id]
    procedure = (patient.get("structured_data") or {}).get("procedure_name", "")
    specialty = patient.get("specialty") or _specialty_from_procedure(procedure)
    session = _team_store.get_preop_intake_session(body.patient_id) or {
        "patient_id": body.patient_id,
        "specialty": specialty,
        "answers": {"pattern": "", "exposure": "", "anatomy": "", "root": ""},
        "last_lens": "pattern",
    }
    current_lens = session.get("last_lens") or _next_missing_lens(session) or "pattern"
    if body.message.strip() and not (session["answers"].get(current_lens) or "").strip():
        session["answers"][current_lens] = body.message.strip()
    next_lens = _next_missing_lens(session)
    if next_lens:
        session["last_lens"] = next_lens
        _team_store.save_preop_intake_session(body.patient_id, session)
        return {
            "patient_id": body.patient_id,
            "response": _lens_question(next_lens, specialty),
            "interview_complete": False,
        }

    _team_store.save_preop_intake_session(body.patient_id, session)
    return {
        "patient_id": body.patient_id,
        "response": (
            "Thanks for those details. Your main intake interview now runs section-by-section on your Pre-Op page "
            "so answers map cleanly into your surgical intake form."
        ),
        "interview_complete": False,
        "prefill_form": None,
        "specialty": specialty,
        "template_name": "",
    }


@app.post("/api/pre-op/intake/submit")
async def preop_intake_submit(
    body: IntakeSubmitRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if body.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(body.patient_id, staff)
    patient = _patient_store[body.patient_id]
    specialty = patient.get("specialty") or _specialty_from_procedure((patient.get("structured_data") or {}).get("procedure_name", ""))
    library = _load_form_library()
    template = library.get(specialty) or library.get("General Surgery") or {}
    form_data = body.form_data or {}
    submission_id = _team_store.save_preop_intake_submission(
        patient_id=body.patient_id,
        specialty=specialty,
        form_template_name=template.get("template_name", "Pre-Op Intake Form"),
        form_data=form_data,
    )
    _team_store.log_event(
        patient_id=body.patient_id,
        event_type="preop_intake_submitted",
        payload={"submission_id": submission_id, "specialty": specialty},
    )
    # PRD §3 / Triage Suite Pass 2: extract PAM-13 proxy responses and
    # synchronously trigger the pre-op re-tier so the recomputed tier
    # is materialized before the intake-finalize response returns.
    await _wire_intake_to_pam_and_retier(
        patient_id=body.patient_id,
        form_data=form_data,
    )
    _team_store.delete_preop_intake_session(body.patient_id)
    return {"ok": True, "submission_id": submission_id}


async def _wire_intake_to_pam_and_retier(
    *,
    patient_id: str,
    form_data: Dict[str, Any],
) -> None:
    """Score PAM responses found in `form_data`, persist a row to
    `pam_assessments`, mark the intake complete on the patient blob,
    and run the pre-op re-tier inside the per-episode lock."""
    from triage.preop_retier import extract_disclosure_flags, score_pam
    from triage.preop_retier.apply import apply_preop_retier
    from triage.preop_retier.locks import with_episode_lock
    from triage.preop_retier.pam_extract import extract_pam_responses
    from triage.preop_retier.tuning import (
        MODEL_VERSION as _PR_MODEL_VERSION,
        TUNING_VERSION as _PR_TUNING_VERSION,
    )

    patient = _patient_store.get(patient_id)
    if patient is None:
        return

    # Mark intake complete (algorithm reads `intake_status` blob field).
    patient["intake_status"] = "COMPLETE"
    patient["intake_completed_at"] = datetime.utcnow().replace(microsecond=0).isoformat()
    patient["intake_disclosures"] = sorted(extract_disclosure_flags(form_data))
    try:
        _team_store.log_event(
            patient_id=patient_id,
            event_type="intake_completed",
            payload={"source": "preop_intake_submit"},
        )
    except Exception:
        pass

    # Persist a `pam_assessments` row whenever there's at least one
    # parseable PAM response — partial submissions still produce a row
    # with `is_complete=False`, which the algorithm treats as "PAM not
    # yet completed" via the not-completed-by ladder (PRD §4.2).
    responses = extract_pam_responses(form_data)
    if responses:
        result = score_pam(responses)
        try:
            _team_store.save_pam_assessment(
                episode_id=patient_id,
                patient_id=patient_id,
                responses=[r.model_dump() for r in responses],
                raw_sum=int(result.raw_sum),
                items_scored=int(result.items_scored),
                raw_average=float(result.raw_average),
                activation_score=float(result.activation_score),
                level=result.level,
                is_complete=bool(result.is_complete),
                model_version=_PR_MODEL_VERSION,
                tuning_version=int(_PR_TUNING_VERSION),
                completed_at=(
                    datetime.utcnow().replace(microsecond=0).isoformat()
                    if result.is_complete else None
                ),
            )
            _team_store.log_event(
                patient_id=patient_id,
                event_type="PAM_ASSESSMENT_SAVED",
                payload={
                    "level": result.level,
                    "activationScore": result.activation_score,
                    "itemsScored": result.items_scored,
                    "isComplete": bool(result.is_complete),
                    "source": "preop_intake_submit",
                },
            )
        except Exception:
            pass

    # Synchronous re-tier inside the per-episode lock so the response
    # reflects the new tier.
    try:
        async with with_episode_lock(patient_id):
            apply_preop_retier(
                patient_id=patient_id,
                patient_store=_patient_store,
                team_store=_team_store,
                triggered_by="SIGNAL:INTAKE_PAM",
            )
    except Exception:
        # Re-tier failure must not block the intake-finalize response.
        pass

    # Triage Suite Pass 3 §4.2 — once-per-episode snapshot of the tier
    # at the moment intake completed. Distinct from `initial_tier`
    # (immutable assignment from the EHR upload) and `current_tier`
    # (the rolling live value). Only stamped if the snapshot row does
    # not yet carry a `post_intake_tier`; subsequent intake submissions
    # must not overwrite the original snapshot.
    try:
        snap = _team_store.get_episode_snapshot(patient_id) or {}
        if not snap.get("post_intake_tier"):
            new_tier = patient.get("current_tier")
            if new_tier:
                _team_store.upsert_episode_snapshot(
                    patient_id, post_intake_tier=new_tier,
                )
                patient["post_intake_tier"] = new_tier

                top_reasons = []
                latest_events = _team_store.list_preop_retier_events(patient_id)
                if latest_events:
                    top_reasons = [
                        {"code": r.get("code"), "label": r.get("label"), "weight": r.get("weight")}
                        for r in (latest_events[0].get("reasons") or [])[:3]
                    ]
                _team_store.log_event(
                    patient_id=patient_id,
                    event_type="POST_INTAKE_TIER_SNAPSHOTTED",
                    payload={
                        "tier": new_tier,
                        "initialTier": patient.get("initial_tier"),
                        "topReasons": top_reasons,
                    },
                )
    except Exception:
        # Snapshot failure must never block the intake-finalize response.
        pass


@app.get("/api/doctor/patient/{patient_id}/latest-intake")
async def doctor_latest_intake(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    if not staff and not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
    submission = _team_store.get_latest_preop_intake_submission(patient_id)
    intake_form = _team_store.get_latest_intake_form_for_patient(patient_id)
    return {"patient_id": patient_id, "submission": submission, "intake_form": intake_form}


@app.post("/api/pre-op/notify-care-team")
async def preop_notify_care_team(
    body: CareTeamNotificationRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if body.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    # Patient-initiated escalation: require a patient session bound to this id (or
    # scoped staff). Otherwise anyone could spam care-team escalations by id.
    _assert_staff_can_access_patient(body.patient_id, staff)
    _team_store.log_event(
        patient_id=body.patient_id,
        event_type="care_team_notification",
        payload={"source": "preop_notify"},
    )
    escalation = await _classify_and_create_escalation(
        patient_id=body.patient_id,
        message=body.message,
        conversation_history=[],
        source="care_team_notification",
    )
    if escalation:
        return {
            "ok": True,
            "response": escalation["response"],
            "escalation": {
                "tier": escalation["tier"],
                "escalation_id": escalation["escalation_id"],
                "requires_consent": False,
                "origin": "Care Team Notification",
            },
        }
    return {
        "ok": True,
        "response": "Your note was sent to your care team for follow-up.",
        "escalation": None,
    }


# ─── Background Tasks ─────────────────────────────────────────
async def _send_html_email(to_email: str, subject: str, html_body: str) -> bool:
    return await _send_html_email_impl(to_email, subject, html_body)


async def _run_team_daily_jobs() -> None:
    today = date.today().isoformat()
    active = _team_store.list_active_episodes()
    for ep in active:
        pid = ep["patient_id"]
        patient = _patient_store.get(pid)
        if not patient:
            continue
        email = patient.get("email") or ""
        if not email:
            continue

        # Daily reminder, idempotent per patient/day.
        if _team_store.mark_daily_reminder(pid, today):
            reminder_html = (
                "<p>Still having trouble understanding your care instructions? "
                "Feel free to view your discharge educational tools again and talk with your care companion!</p>"
            )
            sent = await _send_html_email(
                email,
                "Daily Recovery Reminder - Archangel Health",
                reminder_html,
            )
            if sent:
                _team_store.log_event(patient_id=pid, event_type="email_sent", payload={"channel": "daily_reminder"})

        # Survey day sends (7,14,30)
        try:
            day_num = (date.today() - date.fromisoformat(ep["open_date"])).days + 1
        except Exception:
            continue
        for survey_day in (7, 14, 30):
            if day_num != survey_day:
                continue
            existing = _team_store.get_survey_response(pid, survey_day)
            if existing:
                continue
            sent_new = _team_store.mark_survey_sent(pid, survey_day)
            if not sent_new:
                continue
            link = _survey_link_for_patient(patient, survey_day)
            html_body = (
                f"<p>Your Day {survey_day} recovery survey is ready.</p>"
                f"<p><a href='{html_lib.escape(link, quote=True)}'>Complete survey</a></p>"
                "<p>This helps your care team support your recovery.</p>"
            )
            sent = await _send_html_email(
                email,
                f"Day {survey_day} Recovery Survey - Archangel Health",
                html_body,
            )
            if sent:
                _team_store.log_event(patient_id=pid, event_type="email_sent", payload={"channel": f"survey_day_{survey_day}"})
                _team_store.log_event(patient_id=pid, event_type="survey_pending", payload={"survey_day": survey_day})


async def _team_scheduler_loop() -> None:
    while True:
        try:
            await _run_team_daily_jobs()
        except Exception as e:
            print(f"[team-scheduler] error: {e}")
        await asyncio.sleep(int(os.getenv("TEAM_SCHEDULER_INTERVAL_SECONDS", "3600")))


async def _preop_outreach_loop() -> None:
    while True:
        try:
            await _run_preop_survey_outreach()
        except Exception as e:
            print(f"[preop-scheduler] error: {e}")
        await asyncio.sleep(int(os.getenv("PREOP_OUTREACH_INTERVAL_SECONDS", "900")))


async def _intraop_overdue_loop() -> None:
    """Cron loop: applies the conservative-default tier bump for any
    intra-op form unlocked >24h after OR end (PRD §7.4)."""
    from triage.intraop.overdue_watcher import run_overdue_pass
    from triage.intraop.tuning import OVERDUE_WATCHER_INTERVAL_SECONDS
    interval = int(os.getenv("INTRAOP_OVERDUE_INTERVAL_SECONDS", str(OVERDUE_WATCHER_INTERVAL_SECONDS)))
    while True:
        try:
            run_overdue_pass(patient_store=_patient_store, team_store=_team_store)
        except Exception as e:
            print(f"[intraop-overdue-watcher] error: {e}")
        await asyncio.sleep(interval)


# ─── Post-Op Scoring cron loops (PRD §10.6) ───────────────────────────────

async def _postop_send_pass_loop() -> None:
    """Combined daily-send loop. Each iteration:
       09:00 — daily check-in send + D7/D14/D30 survey send
       19:00 — med adherence ping send
    The simple wall-clock check matches the PRD's daily cadence; in the
    in-memory CareGuide demo we run this loop every `scheduler_tick_seconds`
    and gate the work by the local hour."""
    from triage.postop.cron import (
        run_daily_checkin_send_pass,
        run_dayx_survey_send_pass,
        run_med_adherence_send_pass,
    )
    from triage.postop.tuning import CRON_CONFIG
    tick = int(os.getenv("POSTOP_SEND_INTERVAL_SECONDS", str(CRON_CONFIG["scheduler_tick_seconds"])))
    last_send_local_day: dict[str, str] = {}
    while True:
        try:
            now = datetime.utcnow()
            today = now.strftime("%Y-%m-%d")
            hour = now.hour
            if hour == int(CRON_CONFIG["daily_checkin_send_local"].split(":")[0]) and last_send_local_day.get("checkin") != today:
                run_daily_checkin_send_pass(patient_store=_patient_store, team_store=_team_store)
                run_dayx_survey_send_pass(patient_store=_patient_store, team_store=_team_store)
                last_send_local_day["checkin"] = today
            if hour == int(CRON_CONFIG["med_ping_local"].split(":")[0]) and last_send_local_day.get("med") != today:
                run_med_adherence_send_pass(patient_store=_patient_store, team_store=_team_store)
                last_send_local_day["med"] = today
        except Exception as e:
            print(f"[postop-send-pass] error: {e}")
        await asyncio.sleep(tick)


async def _postop_med_close_loop() -> None:
    """At 23:00 local, mark every unanswered ping as MISSED_NON_RESPONSE."""
    from triage.postop.cron import run_med_adherence_close_pass
    from triage.postop.tuning import CRON_CONFIG
    tick = int(os.getenv("POSTOP_MED_CLOSE_INTERVAL_SECONDS", str(CRON_CONFIG["scheduler_tick_seconds"])))
    target_hour = int(CRON_CONFIG["med_non_response_close_local"].split(":")[0])
    last_close_day: Optional[str] = None
    while True:
        try:
            now = datetime.utcnow()
            today = now.strftime("%Y-%m-%d")
            if now.hour == target_hour and last_close_day != today:
                run_med_adherence_close_pass(team_store=_team_store)
                last_close_day = today
        except Exception as e:
            print(f"[postop-med-close] error: {e}")
        await asyncio.sleep(tick)


async def _postop_checkin_missed_watcher_loop() -> None:
    """Marks daily check-ins past 36h as missed and bumps the streak."""
    from triage.postop.cron import run_checkin_missed_watcher
    from triage.postop.tuning import CRON_CONFIG
    interval = int(os.getenv("POSTOP_CHECKIN_MISSED_WATCHER_SECONDS",
                             str(CRON_CONFIG["checkin_missed_watcher_minutes"] * 60)))
    while True:
        try:
            run_checkin_missed_watcher(patient_store=_patient_store, team_store=_team_store)
        except Exception as e:
            print(f"[postop-checkin-missed] error: {e}")
        await asyncio.sleep(interval)


async def _postop_dayx_missed_watcher_loop() -> None:
    """Marks D-X surveys past 48h as missed."""
    from triage.postop.cron import run_dayx_missed_watcher
    from triage.postop.tuning import CRON_CONFIG
    interval = int(os.getenv("POSTOP_DAYX_MISSED_WATCHER_SECONDS",
                             str(CRON_CONFIG["survey_missed_watcher_minutes"] * 60)))
    while True:
        try:
            run_dayx_missed_watcher(team_store=_team_store)
        except Exception as e:
            print(f"[postop-dayx-missed] error: {e}")
        await asyncio.sleep(interval)


async def _postop_lost_contact_watcher_loop() -> None:
    """Re-tier every post-op patient periodically so 24h Tier-3 / 72h
    general silence trips are caught between signal commits."""
    from triage.postop.cron import run_lost_contact_watcher_async
    from triage.postop.tuning import CRON_CONFIG
    interval = int(os.getenv("POSTOP_LOST_CONTACT_WATCHER_SECONDS",
                             str(CRON_CONFIG["lost_contact_watcher_minutes"] * 60)))
    while True:
        try:
            await run_lost_contact_watcher_async(
                patient_store=_patient_store, team_store=_team_store,
            )
        except Exception as e:
            print(f"[postop-lost-contact] error: {e}")
        await asyncio.sleep(interval)


async def _postop_nightly_retier_loop() -> None:
    """02:00-local nightly batch — recomputes every post-op episode."""
    from triage.postop.cron import run_nightly_retier_batch_async
    from triage.postop.tuning import CRON_CONFIG
    target_hour = int(CRON_CONFIG["nightly_retier_local"].split(":")[0])
    tick = int(os.getenv("POSTOP_NIGHTLY_INTERVAL_SECONDS", str(CRON_CONFIG["scheduler_tick_seconds"])))
    last_run_day: Optional[str] = None
    while True:
        try:
            now = datetime.utcnow()
            today = now.strftime("%Y-%m-%d")
            if now.hour == target_hour and last_run_day != today:
                await run_nightly_retier_batch_async(
                    patient_store=_patient_store, team_store=_team_store,
                )
                last_run_day = today
        except Exception as e:
            print(f"[postop-nightly-retier] error: {e}")
        await asyncio.sleep(tick)


@app.on_event("startup")
async def startup_team_scheduler():
    # Fail closed in production if secrets are still default/weak (PRD-2 §8).
    assert_production_secrets()
    # PRD-4: warn loudly if PHI email would go through a non-BAA transport.
    try:
        from email_utils import active_email_vendor, email_phi_allowed

        if active_email_vendor() == "sendgrid" and not email_phi_allowed():
            _auth_logger.warning(
                "[compliance] Email transport is SendGrid, which is NOT HIPAA-eligible "
                "(no BAA). Patient names are stripped from email bodies; move PHI email "
                "to a BAA-backed provider, or set SENDGRID_BAA_SIGNED=1 only if you truly "
                "have one. See docs/security/SUBPROCESSORS.md."
            )
    except Exception:
        pass
    # PRD-6: warn if application-layer PHI field encryption isn't configured in prod.
    if is_production() and not field_crypto.is_configured():
        _auth_logger.warning(
            "[compliance] DATA_ENCRYPTION_KEY not set — PHI field encryption at rest is "
            "inactive (relying on volume encryption only). Set a base64 32-byte key to "
            "enable AES-256-GCM field encryption. See docs/security/ENCRYPTION.md."
        )
    if not _disable_public_demo_account():
        _ensure_demo_doctor()
    await _seed_demo_mode_data()
    if _triage_demo_enabled():
        _ensure_triage_demo_tenant_seeded()
    # Demo-tenant login re-checks seed freshness so a long-running process
    # can't serve stale/missing demo data (see routers/tenant_portal.py).
    app.state.refresh_triage_demo_seed = _refresh_triage_demo_seed_if_needed
    if _is_demo_mode():
        _persist_demo_patient_store()
    if _is_demo_mode() and _disable_scheduler_in_demo():
        print("[demo-seed] team scheduler disabled for demo startup stability.")
        app.state.team_scheduler_task = None
        app.state.preop_outreach_task = None
        app.state.intraop_overdue_task = None
        app.state.postop_send_task = None
        app.state.postop_med_close_task = None
        app.state.postop_checkin_missed_task = None
        app.state.postop_dayx_missed_task = None
        app.state.postop_lost_contact_task = None
        app.state.postop_nightly_task = None
        return
    app.state.team_scheduler_task = asyncio.create_task(_team_scheduler_loop())
    app.state.preop_outreach_task = asyncio.create_task(_preop_outreach_loop())
    app.state.intraop_overdue_task = asyncio.create_task(_intraop_overdue_loop())
    app.state.postop_send_task = asyncio.create_task(_postop_send_pass_loop())
    app.state.postop_med_close_task = asyncio.create_task(_postop_med_close_loop())
    app.state.postop_checkin_missed_task = asyncio.create_task(_postop_checkin_missed_watcher_loop())
    app.state.postop_dayx_missed_task = asyncio.create_task(_postop_dayx_missed_watcher_loop())
    app.state.postop_lost_contact_task = asyncio.create_task(_postop_lost_contact_watcher_loop())
    app.state.postop_nightly_task = asyncio.create_task(_postop_nightly_retier_loop())


@app.on_event("shutdown")
async def shutdown_team_scheduler():
    for attr in (
        "team_scheduler_task", "preop_outreach_task", "intraop_overdue_task",
        "postop_send_task", "postop_med_close_task", "postop_checkin_missed_task",
        "postop_dayx_missed_task", "postop_lost_contact_task", "postop_nightly_task",
    ):
        task = getattr(app.state, attr, None)
        if task:
            task.cancel()


@app.post("/internal/team/run-daily-jobs", include_in_schema=False)
async def internal_run_team_daily_jobs(authorization: Optional[str] = Header(None)):
    """Manual trigger for TEAM scheduler jobs (local QA/testing)."""
    _check_internal_auth(authorization)
    await _run_team_daily_jobs()
    return {"ok": True, "ran_at": _utcnow_iso()}


async def _send_sms(
    phone: str,
    name: str,
    entry_url: Optional[str] = None,
    clinic_code: str = "",
    resource_code: str = "",
) -> None:
    first = name.split()[0]
    url = entry_url or _patient_entry_url()
    codes = ""
    if clinic_code or resource_code:
        codes = f"Use Health System Code: {clinic_code or 'N/A'}, Resource Code: {resource_code or 'N/A'}. "
    body = (
        f"Hi {first}, your post-surgery recovery resources from your care team are ready. "
        f"View your personalized recovery plan here: {url} "
        f"{codes}(Best viewed on a computer)"
    )
    TwilioClient().send(to=phone, body=body)


# ─── Internal & Admin Tools ───────────────────────────────────
app.include_router(internal_router)
app.include_router(admin_router)
app.include_router(asclepius_router)
app.include_router(onboarding_router)
app.include_router(tenant_portal_router)
app.include_router(eligibility_router)
app.include_router(intraop_router)
app.include_router(postop_router)
app.include_router(initial_tier_router)
app.include_router(preop_retier_router)
app.include_router(teachback_router)
app.include_router(triage_explain_router)
app.include_router(messaging_router)
app.include_router(telehealth_router)


@app.get("/internal/prompt-lab", response_class=HTMLResponse, include_in_schema=False)
async def prompt_lab_page():
    with open(os.path.join(os.path.dirname(__file__), "../frontend/prompt-lab.html")) as f:
        return f.read()

@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
@app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
async def admin_page():
    with open(os.path.join(os.path.dirname(__file__), "../frontend/admin.html")) as f:
        return f.read()


@app.get("/intraop-form/{patient_id}", response_class=HTMLResponse, include_in_schema=False)
async def intraop_form_page(patient_id: str):
    """Serve the surgeon intra-op form shell. Patient id is read by JS at
    runtime from the URL path; the HTML is the same for every patient."""
    path = os.path.join(os.path.dirname(__file__), "../frontend/intraop-form.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Intra-op form page not deployed")
    with open(path) as f:
        return f.read()


# ─── Static Files ─────────────────────────────────────────────
try:
    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(os.path.dirname(__file__), "../frontend")),
        name="static",
    )
except Exception:
    pass

try:
    app.mount(
        "/email-assets",
        StaticFiles(directory=os.path.join(os.path.dirname(__file__), "assets")),
        name="email_assets",
    )
except Exception:
    pass

try:
    app.mount("/audio", StaticFiles(directory="/tmp"), name="audio")
except Exception:
    pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
