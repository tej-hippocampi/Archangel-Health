"""
CareGuide — Surgical Patient Video Platform
FastAPI backend: EHR → Pipeline → Dashboard → SMS
"""

import asyncio
import os
import re
import json
import html as html_lib
import secrets
import string
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from typing import Optional, List

from fastapi import FastAPI, HTTPException, BackgroundTasks, APIRouter, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from pipeline.ingest   import IngestLayer
from pipeline.extract  import ExtractionLayer
from pipeline.classify import ClassificationLayer
from pipeline.generate import GenerationLayer
from integrations.elevenlabs   import ElevenLabsClient
from integrations.tavus        import TavusClient
from integrations.twilio_client import TwilioClient
from routers.internal import router as internal_router
from routers.admin    import router as admin_router
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
from team_store import TeamStore

# ─── App Setup ────────────────────────────────────────────────
app = FastAPI(
    title="Archangel Health Surgical Patient Platform",
    description="Personalized surgical education videos generated from EHR data.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_patient_store: dict = {}
app.state.patient_store = _patient_store
_team_store = TeamStore()
app.state.team_store = _team_store

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


def _seed_demo_patient_if_empty() -> None:
    """Local demo convenience: ensure doctor portal has one visible patient."""
    if _patient_store:
        return
    if os.getenv("SEED_DEMO_PATIENT", "1").strip() in ("0", "false", "False"):
        return
    patient_id = "demo_spine_001"
    structured_data = {
        "patient_name": "James R.",
        "procedure_name": "L4-L5 Lumbar Fusion",
        "procedure_date": date.today().isoformat(),
    }
    _patient_store[patient_id] = {
        "name": "James R.",
        "phone": "+1 (214) 555-0101",
        "email": "james.demo@example.com",
        "pipeline_type": "post_op",
        "voice_audio_url": None,
        "battlecard_html": "<div><h2>Demo Battlecard</h2><p>Demo content.</p></div>",
        "avatar_url": None,
        "voice_script": "This is a demo Digital Care Companion script.",
        "structured_data": structured_data,
        "clinic_code": "DEMOCLIN",
        "resource_code": "DEMO0001",
        "office_phone": os.getenv("CARE_TEAM_PHONE", ""),
        "resources": {
            "diagnosis": {
                "voice_script": "Diagnosis demo script",
                "battlecard_html": "<div><h3>Diagnosis</h3><p>Demo diagnosis card.</p></div>",
                "voice_audio_url": None,
            },
            "treatment": {
                "voice_script": "Treatment demo script",
                "battlecard_html": "<div><h3>Treatment</h3><p>Demo treatment card.</p></div>",
                "voice_audio_url": None,
            },
        },
    }
    _team_store.ensure_episode(
        patient_id=patient_id,
        procedure_type=structured_data.get("procedure_name", ""),
        clinic_code="DEMOCLIN",
        resource_code="DEMO0001",
    )


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
) -> str:
    """
    Build the branded recovery-resources email body.
    Mirrors the latest design while remaining broadly email-client compatible.
    """
    first_name_safe = html_lib.escape(first_name or "Patient")
    clinic_code_safe = html_lib.escape(clinic_code or "N/A")
    resource_code_safe = html_lib.escape(resource_code or "N/A")
    recovery_url_safe = html_lib.escape(recovery_plan_entry_url or "#", quote=True)
    hero_image_src_safe = html_lib.escape(hero_image_src, quote=True) if hero_image_src else ""
    logo_image_src_safe = html_lib.escape(logo_image_src, quote=True) if logo_image_src else ""
    hero_image_block = ""
    if hero_image_src_safe:
        # Use width-driven sizing (not object-fit crop) for consistent email rendering.
        hero_image_block = f'<img src="{hero_image_src_safe}" alt="Classical medical painting" width="680" style="display:block;width:100%;max-width:680px;height:auto;border:0;" />'
    else:
        hero_image_block = '<div style="display:block;width:100%;height:230px;background:linear-gradient(135deg,#0f172a,#1e293b 45%,#1d4ed8);"></div>'
    logo_block = ""
    if logo_image_src_safe:
        logo_block = f'<img src="{logo_image_src_safe}" alt="Archangel Health logo" width="64" height="64" style="display:block;width:64px;height:64px;margin:14px auto;border:0;" />'
    else:
        logo_block = '<div style="font-size:34px;line-height:1;color:#ffffff;text-align:center;padding-top:26px;">&#9877;</div>'
    footer_logo_block = ""
    if logo_image_src_safe:
        footer_logo_block = f'<img src="{logo_image_src_safe}" alt="Archangel Health logo" width="24" height="24" style="display:block;width:24px;height:24px;margin:0 auto;border:0;" />'
    else:
        footer_logo_block = '<div style="font-size:18px;line-height:1;color:#4b5563;">&#9877;</div>'

    return f"""
    <div style="margin:0;padding:0;background:#EFF4FB;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#EFF4FB;">
        <tr>
          <td align="center" style="padding:24px 16px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="680" style="width:680px;max-width:680px;background:#ffffff;border-radius:14px;overflow:hidden;margin:0 auto;">
              <tr>
                <td style="padding:0;background:#0f172a;">
                  {hero_image_block}
                </td>
              </tr>
              <tr>
                <td align="center" style="padding:16px 24px 20px 24px;background:#0b1228;">
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                    <tr>
                      <td align="center" style="padding:0;">
                        <div style="width:92px;height:92px;border-radius:16px;background:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.24);margin:0 auto 16px auto;">
                          {logo_block}
                        </div>
                        <div style="font-family:Georgia,serif;color:#ffffff;font-size:62px;line-height:1.06;font-weight:700;text-shadow:0 3px 10px rgba(0,0,0,0.45);">
                          Archangel Health
                        </div>
                        <div style="height:1px;width:120px;background:rgba(255,255,255,0.52);margin:14px auto 0 auto;"></div>
                        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="88%" style="max-width:520px;margin:20px auto 0 auto;background:rgba(255,255,255,0.10);border:1px solid rgba(255,255,255,0.32);border-radius:12px;">
                          <tr>
                            <td align="center" style="padding:14px 22px;">
                              <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#bfdbfe;font-size:12px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;">
                                Your Care Package
                              </div>
                              <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#ffffff;font-size:20px;line-height:1.12;font-weight:700;margin-top:5px;">
                                Recovery Resources Ready
                              </div>
                            </td>
                          </tr>
                        </table>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <tr>
                <td style="padding:30px 28px 8px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937;font-size:18px;line-height:1.65;">
                  Hi <span style="font-weight:700;color:#111827;">{first_name_safe}</span>,
                </td>
              </tr>
              <tr>
                <td style="padding:0 28px 20px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#4b5563;font-size:16px;line-height:1.75;">
                  Your care team has prepared personalized recovery resources for you, including voice explanations and quick reference guides.
                </td>
              </tr>
              <tr>
                <td style="padding:0 28px 8px 28px;">
                  <div style="height:1px;background:linear-gradient(90deg,rgba(209,213,219,0),rgba(209,213,219,1),rgba(209,213,219,0));"></div>
                </td>
              </tr>

              <tr>
                <td align="center" style="padding:20px 28px 8px 28px;">
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;font-size:12px;font-weight:700;letter-spacing:0.18em;text-transform:uppercase;">Your Access Codes</div>
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#6b7280;font-size:13px;line-height:1.6;margin-top:6px;">Save these codes to access your personalized recovery plan</div>
                </td>
              </tr>

              <tr>
                <td style="padding:10px 28px 0 28px;">
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#6b7280;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;text-align:center;margin-bottom:10px;">Clinic Code</div>
                  <div style="background:#f8fafc;border:1px solid #d1d5db;border-radius:12px;padding:18px;text-align:center;">
                    <div style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,'Courier New',monospace;color:#111827;font-size:34px;font-weight:800;letter-spacing:0.15em;line-height:1.2;">{clinic_code_safe}</div>
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:16px 28px 0 28px;">
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#6b7280;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;text-align:center;margin-bottom:10px;">Resource Code</div>
                  <div style="background:#f8fafc;border:1px solid #d1d5db;border-radius:12px;padding:18px;text-align:center;">
                    <div style="font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,'Courier New',monospace;color:#111827;font-size:34px;font-weight:800;letter-spacing:0.15em;line-height:1.2;">{resource_code_safe}</div>
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:22px 28px 14px 28px;">
                  <a href="{recovery_url_safe}" style="display:block;text-decoration:none;text-align:center;background:#111827;color:#ffffff;border:1px solid #78350f;border-radius:12px;padding:15px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:17px;font-weight:700;line-height:1.2;">
                    View Your Recovery Plan
                  </a>
                </td>
              </tr>

              <tr>
                <td style="padding:0 28px 22px 28px;">
                  <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:12px;padding:13px 14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#4b5563;font-size:13px;line-height:1.65;text-align:center;">
                    <span style="color:#92400e;">&#10022;</span> <strong style="color:#111827;">Pro tip:</strong> Use your Clinic Code and Resource Code above to access your plan. Best viewed on a computer or tablet for the full experience.
                  </div>
                </td>
              </tr>

              <tr>
                <td style="border-top:1px solid #e5e7eb;padding:8px 28px 6px 28px;text-align:center;">
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:0 auto;">
                    <tr>
                      <td align="center" style="padding:0 0 4px 0;">{footer_logo_block}</td>
                    </tr>
                    <tr>
                      <td align="center" style="font-family:Georgia,serif;color:#374151;font-size:16px;font-weight:700;">Archangel Health</td>
                    </tr>
                    <tr>
                      <td align="center" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#9ca3af;font-size:12px;padding-top:4px;">Your personalized healthcare companion</td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>
            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#6b7280;font-size:12px;line-height:1.5;text-align:center;padding-top:14px;">
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
) -> str:
    configured_hero_url = (os.getenv("EMAIL_HIPPOCRATES_IMAGE_URL") or "").strip()
    configured_logo_url = (os.getenv("EMAIL_LOGO_IMAGE_URL") or "").strip()
    canonical_hero_url = "https://archangelhealth.ai/hippocrates-email-bg.png"
    canonical_logo_url = "https://archangelhealth.ai/medical-guardian-logo-email.png"

    if use_local_preview_assets:
        hero_public_url = "/email-assets/hippocrates-email-bg.png"
        logo_public_url = "/email-assets/medical-guardian-logo-email.png"
    else:
        # Send mode uses stable canonical URLs by default.
        hero_public_url = configured_hero_url if _is_public_http_url(configured_hero_url) else canonical_hero_url
        logo_public_url = configured_logo_url if _is_public_http_url(configured_logo_url) else canonical_logo_url

    html_body = _build_recovery_resources_email_html(
        first_name=first_name,
        clinic_code=clinic_code,
        resource_code=resource_code,
        recovery_plan_entry_url=recovery_plan_entry_url,
        hero_image_src=hero_public_url,
        logo_image_src=logo_public_url,
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
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        eval_prompt = (
            "Classify the patient message for post-op escalation. "
            "Return only compact JSON object: "
            '{"tier": 0|2|3, "reason": "short reason"}. '
            "Use tier 2 for urgent same-day surgeon contact, "
            "tier 3 for navigator follow-up within 24 hours, "
            "tier 0 for no escalation."
        )
        convo = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in (conversation_history or [])]
        convo.append({"role": "user", "content": message})
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=120,
            system=eval_prompt,
            messages=convo,
        )
        text = resp.content[0].text.strip()
        parsed = json.loads(text)
        tier = int(parsed.get("tier", 0))
        if tier in (2, 3):
            return {"tier": tier, "trigger_type": "semantic", "reason": parsed.get("reason", "")}
    except Exception:
        return _heuristic_semantic_escalation(message)
    return None


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


class SurveySubmitRequest(BaseModel):
    clinic_code: str
    resource_code: str
    day: int
    answers: List[dict]


class EscalationResolveRequest(BaseModel):
    resolved: bool


class EscalationConsentRequest(BaseModel):
    escalation_id: int
    consent: str


# ─── Auth (Elysium Health landing) ────────────────────────────
@app.post("/api/auth/register")
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


@app.post("/api/auth/login")
async def auth_login(body: UserLogin):
    """Sign in; returns access token and user."""
    user = authenticate_user(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token(user["email"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserOut(email=user["email"], name=user.get("name"), role=user.get("role")),
    }


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
async def doctor_profile(user: UserOut = Depends(get_current_user)):
    """Return current doctor's profile (requires onboarding to be done)."""
    profile = get_doctor_profile(user.email)
    if not profile:
        raise HTTPException(
            status_code=404,
            detail="Doctor profile not found. Complete onboarding first.",
        )
    return DoctorProfileOut(**profile)


@app.post("/api/doctor/onboard", response_model=DoctorProfileOut)
async def doctor_onboard(body: DoctorOnboard, user: UserOut = Depends(get_current_user)):
    """Set doctor profile and generate clinic code. Email must match current user."""
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
        return DoctorProfileOut(**profile)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Patient access by codes (for landing code-entry form) ───────
@app.get("/api/patient/by-codes")
async def patient_by_codes(clinic_code: str, resource_code: str):
    """Resolve clinic_code + resource_code to patient_id and dashboard URL."""
    clinic_code = (clinic_code or "").strip().upper()
    resource_code = (resource_code or "").strip().upper()
    if not clinic_code or not resource_code:
        raise HTTPException(status_code=400, detail="Clinic code and resource code are required.")
    for pid, d in _patient_store.items():
        if (d.get("clinic_code") or "").upper() == clinic_code and (d.get("resource_code") or "").upper() == resource_code:
            base_url = os.getenv("BASE_URL", "http://localhost:8000")
            _team_store.ensure_episode(
                patient_id=pid,
                procedure_type=(d.get("structured_data") or {}).get("procedure_name", ""),
                clinic_code=d.get("clinic_code") or "",
                resource_code=d.get("resource_code") or "",
            )
            # Explicitly mark successful code-based platform entry.
            _team_store.log_event(patient_id=pid, event_type="platform_opened", payload={"clinic_code": clinic_code})
            return {"patient_id": pid, "dashboard_url": f"{base_url}/patient/{pid}"}
    raise HTTPException(status_code=404, detail="No patient found for these codes. Check and try again.")


# ─── Doctor Portal ────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def doctor_portal(request: Request):
    """Serves the doctor dashboard, or redirects to /admin for the admin subdomain."""
    host = request.headers.get("host", "")
    if "admin." in host:
        return RedirectResponse(url="/admin", status_code=301)
    html_path = os.path.join(os.path.dirname(__file__), "../frontend/doctor.html")
    with open(html_path) as f:
        return HTMLResponse(content=f.read())


@app.get("/api/patients")
async def list_patients():
    """Return all patients in the store for the doctor roster."""
    patients = []
    for pid, d in _patient_store.items():
        sd = d.get("structured_data") or {}
        episode = _team_store.get_episode(pid) or _team_store.ensure_episode(
            patient_id=pid,
            procedure_type=sd.get("procedure_name", ""),
            clinic_code=d.get("clinic_code") or "",
            resource_code=d.get("resource_code") or "",
        )
        open_dt = date.fromisoformat(episode["open_date"])
        day_in_episode = max(1, (date.today() - open_dt).days + 1)
        day_in_episode = min(day_in_episode, 30)
        patients.append({
            "id": pid,
            "name": d.get("name", "Unknown"),
            "procedure": sd.get("procedure_name", ""),
            "date": sd.get("procedure_date", ""),
            "hasResources": d.get("resources") is not None,
            "pipelineType": d.get("pipeline_type", "post_op"),
            "phone": d.get("phone", ""),
            "email": d.get("email", ""),
            "episode": {
                "openDate": episode["open_date"],
                "closeDate": episode["close_date"],
                "status": episode["status"],
                "currentDay": day_in_episode,
            },
        })
    return {"patients": patients}


@app.get("/api/patient/{patient_id}/discharge-materials")
async def get_discharge_materials(patient_id: str):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    d = _patient_store[patient_id]
    resources = d.get("resources") or {}
    return {
        "patient_id": patient_id,
        "diagnosis": resources.get("diagnosis"),
        "treatment": resources.get("treatment"),
    }


@app.post("/api/patient/{patient_id}/events")
async def track_patient_event(patient_id: str, body: EventTrackRequest):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    allowed = {
        "platform_opened",
        "email_sent",
        "diagnosis_video_watched",
        "treatment_video_watched",
        "avatar_chat",
        "survey_pending",
        "survey_completed",
        "sms_sent",
    }
    event_type = (body.event_type or "").strip()
    if event_type not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported event_type")
    _team_store.log_event(patient_id=patient_id, event_type=event_type, payload=body.payload or {})
    return {"ok": True}


@app.get("/api/patient/{patient_id}/timeline")
async def get_patient_timeline(patient_id: str):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    d = _patient_store[patient_id]
    sd = d.get("structured_data") or {}
    episode = _team_store.get_episode(patient_id) or _team_store.ensure_episode(
        patient_id=patient_id,
        procedure_type=sd.get("procedure_name", ""),
        clinic_code=d.get("clinic_code") or "",
        resource_code=d.get("resource_code") or "",
    )
    open_dt = date.fromisoformat(episode["open_date"])
    close_dt = date.fromisoformat(episode["close_date"])
    current_day = max(1, min((date.today() - open_dt).days + 1, 30))
    events = _team_store.get_events(patient_id)
    markers = {str(i): [] for i in range(1, 31)}
    for ev in events:
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
        day = int(sr["survey_day"])
        markers[str(day)].append(
            {
                "id": sr["id"],
                "type": "survey_completed",
                "timestamp": sr["submitted_at"],
                "payload": {"survey_day": day, "score": sr.get("score"), "tier": sr.get("tier")},
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
async def list_escalations():
    rows = _team_store.list_escalations()
    out = []
    for row in rows:
        patient = _patient_store.get(row["patient_id"], {})
        out.append(
            {
                "id": row["id"],
                "patient_id": row["patient_id"],
                "patient_name": patient.get("name", row["patient_id"]),
                "tier": row["tier"],
                "trigger_type": row["trigger_type"],
                "message": row.get("message", ""),
                "consent": row.get("consent"),
                "consent_at": row.get("consent_at"),
                "resolved": bool(row.get("resolved")),
                "created_at": row["created_at"],
                "conversation_snapshot": row.get("conversation_snapshot", []),
            }
        )
    resolved_count = sum(1 for r in out if r["resolved"])
    return {"escalations": out, "resolved_count": resolved_count, "total_count": len(out)}


@app.patch("/api/escalations/{escalation_id}/resolved")
async def set_escalation_resolved(escalation_id: int, body: EscalationResolveRequest):
    row = _team_store.get_escalation(escalation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Escalation not found")
    _team_store.set_escalation_resolved(escalation_id, body.resolved)
    return {"ok": True, "resolved": body.resolved}


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


@app.post("/api/survey/submit")
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


@app.get("/api/doctor/patient/{patient_id}/survey/{day}")
async def get_doctor_survey(patient_id: str, day: int, user: UserOut = Depends(get_current_user)):
    if day not in (7, 14, 30):
        raise HTTPException(status_code=400, detail="Day must be 7, 14, or 30")
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    survey = _team_store.get_survey_response(patient_id, day)
    return {
        "patient_id": patient_id,
        "day": day,
        "response": survey,
        "composite_score": _team_store.get_composite_score(patient_id),
        "doctor_only": True,
    }


@app.get("/doctor/patient/{patient_id}", response_class=HTMLResponse)
async def doctor_patient_view(patient_id: str):
    """Doctor's view of a patient dashboard (same as patient view but with back-to-roster nav)."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")

    d = _patient_store[patient_id]

    def clean_html(html):
        h = (html or "").strip()
        if h.startswith("```"):
            h = h.split("\n", 1)[1] if "\n" in h else h[3:]
            if h.endswith("```"):
                h = h[:-3].strip()
        return h

    resources = d.get("resources")
    resources_json = None
    if resources:
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
async def send_to_patient(patient_id: str):
    """Send the patient dashboard link via SMS (Twilio) and email. Email includes clinic/resource codes and link to code-entry page."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")

    d = _patient_store[patient_id]
    name = d.get("name", "Patient")
    first_name = name.split()[0]
    phone = d.get("phone", "")
    email = d.get("email", "")
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    landing_url = (os.getenv("LANDING_URL") or "").strip().rstrip("/")
    dashboard_url = f"{base_url}/patient/{patient_id}"
    clinic_code = (d.get("clinic_code") or "").strip()
    resource_code = (d.get("resource_code") or "").strip()
    # Link must go to landing page so patient can enter codes; never use backend root (doctor dashboard)
    recovery_plan_entry_url = f"{landing_url}/#recovery-plan" if landing_url else dashboard_url

    results = {"sms": None, "email": None}

    # SMS via Twilio
    if phone:
        try:
            sms_body = (
                f"Hi {first_name}, your post-surgery recovery resources from your care team are ready. "
                f"View your personalized recovery plan here: {recovery_plan_entry_url} "
                f"Use Clinic Code: {clinic_code or 'N/A'}, Resource Code: {resource_code or 'N/A'}. "
                f"(Best viewed on a computer)"
            )
            sid = TwilioClient().send(to=phone, body=sms_body)
            results["sms"] = "sent" if sid else "twilio_not_configured"
            if results["sms"] == "sent":
                _team_store.log_event(patient_id=patient_id, event_type="sms_sent", payload={"channel": "sms_initial"})
        except Exception as e:
            print(f"[send] SMS error: {e}")
            results["sms"] = f"error: {str(e)}"

    # Email via SendGrid Web API (or SMTP fallback if no API key)
    if email:
        try:
            api_key = os.getenv("SENDGRID_API_KEY")
            from_email = os.getenv("SENDGRID_FROM_EMAIL", "noreply@archangelhealth.ai")
            from_name = os.getenv("SENDGRID_FROM_NAME", "Archangel Health")

            html_body = _render_recovery_email_html(
                first_name=first_name,
                clinic_code=clinic_code,
                resource_code=resource_code,
                recovery_plan_entry_url=recovery_plan_entry_url,
            )

            if api_key:
                from sendgrid import SendGridAPIClient
                from sendgrid.helpers.mail import Mail

                message = Mail(
                    from_email=(from_email, from_name),
                    to_emails=email,
                    subject="Your Recovery Resources Are Ready - Archangel Health",
                    html_content=html_body,
                )
                sg = SendGridAPIClient(api_key)
                response = sg.send(message)
                results["email"] = "sent" if response.status_code in (200, 202) else f"error: {response.status_code}"
                if results["email"] == "sent":
                    _team_store.log_event(patient_id=patient_id, event_type="email_sent", payload={"channel": "email_initial"})
                    print(f"[send] SendGrid email sent → {email}")
            else:
                # Fallback: SMTP
                import smtplib
                from email.mime.text import MIMEText
                from email.mime.multipart import MIMEMultipart

                smtp_host = os.getenv("SMTP_HOST")
                smtp_user = os.getenv("SMTP_USER")
                smtp_pass = os.getenv("SMTP_PASS")
                if smtp_host and smtp_user and smtp_pass:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = "Your Recovery Resources Are Ready - Archangel Health"
                    msg["From"] = smtp_user
                    msg["To"] = email
                    msg.attach(MIMEText(html_body, "html"))
                    with smtplib.SMTP(smtp_host, int(os.getenv("SMTP_PORT", "587"))) as server:
                        server.starttls()
                        server.login(smtp_user, smtp_pass)
                        server.send_message(msg)
                    results["email"] = "sent"
                    _team_store.log_event(patient_id=patient_id, event_type="email_sent", payload={"channel": "email_initial"})
                else:
                    results["email"] = "sendgrid_not_configured"
                    print(f"[send] Email skipped — SENDGRID_API_KEY and SMTP not configured. Would send to: {email}")

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
        recovery_plan_entry_url=f"{base}/#recovery-plan",
        use_local_preview_assets=True,
    )


# ─── New Two-Resource Pipeline ────────────────────────────────
@app.post("/api/process-discharge")
async def process_discharge(
    input_data: DischargeInput,
    user: Optional[UserOut] = Depends(get_current_user_optional),
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
    if user and user.role == "doctor":
        profile = get_doctor_profile(user.email)
        if profile:
            clinic_code = profile["clinic_code"]
            office_phone = profile.get("office_phone") or ""
            resource_code = _generate_resource_code()
    if not office_phone:
        office_phone = (input_data.doctor_office_phone or "").strip() or None
    if not clinic_code:
        clinic_code = (input_data.doctor_clinic_code or "").strip().upper() or None
    if input_data.resource_code:
        resource_code = (input_data.resource_code or "").strip().upper() or None
    if clinic_code and not resource_code:
        resource_code = _generate_resource_code()

    try:
        # 1. Extract structured data from raw discharge notes
        print(f"[pipeline] Starting extraction for {patient_id}...")
        raw_package = {
            "metadata": {
                "patient_id": patient_id,
                "patient_name": input_data.patient_name,
                "phone_number": input_data.phone_number or "",
            },
            "clinical_data": {
                "clinical_notes": input_data.discharge_notes,
                "after_visit_summary": input_data.discharge_notes,
                "pmh": "",
                "procedure_context": "",
                "medication_list": "",
                "allergies": "",
                "problem_list": "",
            },
        }

        structured_data = await ExtractionLayer().extract(raw_package)
        print(f"[pipeline] Extraction complete. Generating resources...")

        # 2. Generate two resource sets (diagnosis + treatment)
        generator = GenerationLayer()
        resources = await generator.generate_two_resources(structured_data)
        print(f"[pipeline] Generation complete. Synthesizing audio...")

        # 3. Synthesize audio for both via ElevenLabs in parallel
        el_client = ElevenLabsClient()
        diag_audio, treat_audio = await asyncio.gather(
            el_client.synthesize(resources["diagnosis"]["voice_script"], f"{patient_id}_diagnosis"),
            el_client.synthesize(resources["treatment"]["voice_script"], f"{patient_id}_treatment"),
        )
        print(f"[pipeline] Audio synthesis complete. Storing results...")

        # 4. Build dashboard URL
        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        dashboard_url = f"{base_url}/patient/{patient_id}"

        # 5. Store everything (include clinic_code, resource_code, office_phone when from authenticated doctor)
        _patient_store[patient_id] = {
            "name": input_data.patient_name,
            "phone": input_data.phone_number or "",
            "email": input_data.email or "",
            "pipeline_type": "post_op",
            "voice_audio_url": diag_audio,
            "battlecard_html": resources["diagnosis"]["battlecard_html"],
            "avatar_url": None,
            "voice_script": resources["diagnosis"]["voice_script"],
            "structured_data": structured_data,
            "clinic_code": clinic_code,
            "resource_code": resource_code,
            "office_phone": office_phone,
            "resources": {
                "diagnosis": {
                    "voice_script": resources["diagnosis"]["voice_script"],
                    "battlecard_html": resources["diagnosis"]["battlecard_html"],
                    "voice_audio_url": diag_audio,
                },
                "treatment": {
                    "voice_script": resources["treatment"]["voice_script"],
                    "battlecard_html": resources["treatment"]["battlecard_html"],
                    "voice_audio_url": treat_audio,
                },
            },
        }
        _team_store.ensure_episode(
            patient_id=patient_id,
            procedure_type=structured_data.get("procedure_name", ""),
            clinic_code=clinic_code or "",
            resource_code=resource_code or "",
        )

        print(f"[pipeline] Done! Dashboard: {dashboard_url}")

        out = {
            "patient_id": patient_id,
            "dashboard_url": dashboard_url,
            "clinic_code": clinic_code,
            "resource_code": resource_code,
            "diagnosis": {
                "voice_script": resources["diagnosis"]["voice_script"],
                "battlecard_html": resources["diagnosis"]["battlecard_html"],
                "voice_audio_url": diag_audio,
            },
            "treatment": {
                "voice_script": resources["treatment"]["voice_script"],
                "battlecard_html": resources["treatment"]["battlecard_html"],
                "voice_audio_url": treat_audio,
            },
            "structured_data": structured_data,
        }
        return out

    except Exception as exc:
        print(f"[pipeline] ERROR: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {str(exc)}")


# ─── Legacy Process Patient ───────────────────────────────────
@app.post("/api/process-patient", response_model=ProcessResponse)
async def process_patient(bundle: EHRBundle, background_tasks: BackgroundTasks):
    """Legacy full pipeline (single resource set)."""
    raw_package = IngestLayer().process(bundle.model_dump())
    structured_data = await ExtractionLayer().extract(raw_package)
    pipeline_type = ClassificationLayer().classify(structured_data)
    generator = GenerationLayer()
    voice_script, battlecard_html = await generator.generate(structured_data, pipeline_type)
    audio_url = await ElevenLabsClient().synthesize(voice_script, bundle.patient_id)
    avatar = await TavusClient().create_conversation(
        patient_id=bundle.patient_id,
        knowledge_base={
            "voice_script":  voice_script,
            "battlecard":    battlecard_html,
            "ehr_summary":   structured_data,
        },
    )
    base_url      = os.getenv("BASE_URL", "http://localhost:8000")
    dashboard_url = f"{base_url}/patient/{bundle.patient_id}"
    _patient_store[bundle.patient_id] = {
        "name":                bundle.patient_name,
        "phone":               bundle.phone_number,
        "pipeline_type":       pipeline_type,
        "voice_audio_url":     audio_url,
        "battlecard_html":     battlecard_html,
        "avatar_url":          avatar.get("conversation_url"),
        "structured_data":     structured_data,
        "voice_script":        voice_script,
        "resources":           None,
    }
    _team_store.ensure_episode(
        patient_id=bundle.patient_id,
        procedure_type=structured_data.get("procedure_name", ""),
        clinic_code="",
        resource_code="",
    )
    background_tasks.add_task(
        _send_sms, phone=bundle.phone_number, name=bundle.patient_name, dashboard_url=dashboard_url,
    )
    return ProcessResponse(
        patient_id=bundle.patient_id, pipeline_type=pipeline_type,
        dashboard_url=dashboard_url, voice_audio_url=audio_url,
        battlecard_html=battlecard_html, avatar_url=avatar.get("conversation_url"),
    )


# ─── Resource Endpoints ───────────────────────────────────────
@app.get("/api/patient/{patient_id}/resources")
async def get_patient_resources(patient_id: str):
    """Return the two-resource sets (diagnosis + treatment) if available."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
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

    return resources


@app.get("/api/patient/{patient_id}/audio")
async def get_patient_audio(patient_id: str):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
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
    audio_url = await ElevenLabsClient().synthesize(voice_script, patient_id)
    if not audio_url:
        raise HTTPException(status_code=503, detail="ElevenLabs not configured — set ELEVENLABS_API_KEY")
    store["voice_audio_url"] = audio_url
    return {"audio_url": audio_url}


@app.get("/api/patient/{patient_id}/battlecard")
async def get_battlecard(patient_id: str):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    return {"html": _patient_store[patient_id]["battlecard_html"]}


@app.get("/api/patient/{patient_id}/config")
async def get_dashboard_config(patient_id: str):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
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
async def get_discharge_instructions(patient_id: str):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    d = _patient_store[patient_id]
    return {
        "structured_data": d["structured_data"],
        "voice_script": d.get("voice_script", ""),
    }


@app.get("/patient/{patient_id}/digital-care-companion", response_class=HTMLResponse)
@app.get("/patient/{patient_id}/voice", response_class=HTMLResponse)
async def digital_care_companion_page(patient_id: str):
    """Serves the Digital Care Companion conversation interface."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")

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


@app.get("/patient/{patient_id}", response_class=HTMLResponse)
async def patient_dashboard(patient_id: str):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")

    d = _patient_store[patient_id]

    def clean_html(html):
        h = (html or "").strip()
        if h.startswith("```"):
            h = h.split("\n", 1)[1] if "\n" in h else h[3:]
            if h.endswith("```"):
                h = h[:-3].strip()
        return h

    resources = d.get("resources")
    resources_json = None
    if resources:
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
async def digital_care_companion_chat(req: ChatRequest):
    if req.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")

    patient_data = _patient_store[req.patient_id]
    _team_store.log_event(patient_id=req.patient_id, event_type="avatar_chat", payload={"source": "chat"})
    conversation_snapshot = list(req.conversation_history or []) + [{"role": "user", "content": req.message}]

    if _detect_hard_tier_1(req.message):
        esc_id = _team_store.create_escalation(
            patient_id=req.patient_id,
            tier=1,
            trigger_type="hard_keyword",
            message=req.message,
            conversation_snapshot=conversation_snapshot,
        )
        return ChatResponse(
            response=TIER_1_RESPONSE,
            patient_id=req.patient_id,
            audio_url=None,
            escalation={"tier": 1, "escalation_id": esc_id, "requires_consent": False},
        )

    semantic = await _evaluate_semantic_escalation_llm(req.message, req.conversation_history)
    if semantic and semantic.get("tier") == 2:
        doctor_phone = patient_data.get("office_phone") or os.getenv("CARE_TEAM_PHONE", "")
        msg = (
            "This is something your surgeon needs to hear about today. "
            f"Please call {doctor_phone or 'your surgeon office'} as soon as possible. "
            "I'm flagging this for your care team now."
        )
        esc_id = _team_store.create_escalation(
            patient_id=req.patient_id,
            tier=2,
            trigger_type=semantic.get("trigger_type", "semantic"),
            message=req.message,
            conversation_snapshot=conversation_snapshot,
        )
        return ChatResponse(
            response=msg,
            patient_id=req.patient_id,
            audio_url=None,
            escalation={"tier": 2, "escalation_id": esc_id, "requires_consent": False},
        )
    if semantic and semantic.get("tier") == 3:
        msg = (
            "I want to make sure you get the right support for this. "
            "I'm going to let your care navigator know so they can follow up with you directly. Is that okay?"
        )
        esc_id = _team_store.create_escalation(
            patient_id=req.patient_id,
            tier=3,
            trigger_type=semantic.get("trigger_type", "semantic"),
            message=req.message,
            conversation_snapshot=conversation_snapshot,
        )
        return ChatResponse(
            response=msg,
            patient_id=req.patient_id,
            audio_url=None,
            escalation={"tier": 3, "escalation_id": esc_id, "requires_consent": True},
        )

    try:
        from prompts.avatar import build_avatar_system_prompt
        from anthropic import Anthropic

        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        clean_data = {k: v for k, v in patient_data["structured_data"].items()
                      if k != "_raw_clinical"}
        system_prompt = build_avatar_system_prompt(clean_data)

        messages = [{"role": m["role"], "content": m["content"]}
                    for m in req.conversation_history]
        messages.append({"role": "user", "content": req.message})

        print(f"[digital_care_companion_chat] Sending to Claude for patient {req.patient_id}...")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
            system=system_prompt,
            messages=messages,
        )

        reply_text = response.content[0].text
        print(f"[digital_care_companion_chat] Got response ({len(reply_text)} chars), synthesizing audio...")
        audio_url = await ElevenLabsClient().synthesize(reply_text, f"{req.patient_id}_chat")

        return ChatResponse(response=reply_text, patient_id=req.patient_id, audio_url=audio_url, escalation=None)

    except Exception as exc:
        import traceback
        print(f"[digital_care_companion_chat] ERROR: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return ChatResponse(
            response="I'm having a brief technical issue. For urgent questions, please call your care team directly.",
            patient_id=req.patient_id, audio_url=None,
        )


# ─── Background Tasks ─────────────────────────────────────────
async def _send_html_email(to_email: str, subject: str, html_body: str) -> bool:
    try:
        api_key = os.getenv("SENDGRID_API_KEY")
        from_email = os.getenv("SENDGRID_FROM_EMAIL", "noreply@archangelhealth.ai")
        from_name = os.getenv("SENDGRID_FROM_NAME", "Archangel Health")
        if api_key:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            message = Mail(
                from_email=(from_email, from_name),
                to_emails=to_email,
                subject=subject,
                html_content=html_body,
            )
            sg = SendGridAPIClient(api_key)
            response = sg.send(message)
            return response.status_code in (200, 202)

        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        smtp_host = os.getenv("SMTP_HOST")
        smtp_user = os.getenv("SMTP_USER")
        smtp_pass = os.getenv("SMTP_PASS")
        if smtp_host and smtp_user and smtp_pass:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = smtp_user
            msg["To"] = to_email
            msg.attach(MIMEText(html_body, "html"))
            with smtplib.SMTP(smtp_host, int(os.getenv("SMTP_PORT", "587"))) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            return True
    except Exception as e:
        print(f"[team-email] error: {e}")
    return False


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


@app.on_event("startup")
async def startup_team_scheduler():
    _seed_demo_patient_if_empty()
    app.state.team_scheduler_task = asyncio.create_task(_team_scheduler_loop())


@app.on_event("shutdown")
async def shutdown_team_scheduler():
    task = getattr(app.state, "team_scheduler_task", None)
    if task:
        task.cancel()


@app.post("/internal/team/run-daily-jobs", include_in_schema=False)
async def internal_run_team_daily_jobs():
    """Manual trigger for TEAM scheduler jobs (local QA/testing)."""
    await _run_team_daily_jobs()
    return {"ok": True, "ran_at": _utcnow_iso()}


async def _send_sms(phone: str, name: str, dashboard_url: str) -> None:
    first = name.split()[0]
    body  = (
        f"Hi {first}, your post-surgery recovery resources from your care team are ready. "
        f"View your personalized recovery plan here: {dashboard_url} "
        f"(Best viewed on a computer)"
    )
    TwilioClient().send(to=phone, body=body)


# ─── Internal & Admin Tools ───────────────────────────────────
app.include_router(internal_router)
app.include_router(admin_router)


@app.get("/internal/prompt-lab", response_class=HTMLResponse, include_in_schema=False)
async def prompt_lab_page():
    with open(os.path.join(os.path.dirname(__file__), "../frontend/prompt-lab.html")) as f:
        return f.read()

@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
@app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
async def admin_page():
    with open(os.path.join(os.path.dirname(__file__), "../frontend/admin.html")) as f:
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
