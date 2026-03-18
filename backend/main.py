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
        patients.append({
            "id": pid,
            "name": d.get("name", "Unknown"),
            "procedure": sd.get("procedure_name", ""),
            "date": sd.get("procedure_date", ""),
            "hasResources": d.get("resources") is not None,
            "pipelineType": d.get("pipeline_type", "post_op"),
            "phone": d.get("phone", ""),
            "email": d.get("email", ""),
        })
    return {"patients": patients}


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
    voice_url = f"/patient/{patient_id}/voice"
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


@app.get("/patient/{patient_id}/voice", response_class=HTMLResponse)
async def voice_avatar_page(patient_id: str):
    """Serves the voice avatar conversation interface."""
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

    voice_url = f"/patient/{patient_id}/voice"
    html = html.replace('id="voiceAvatarBtn" href="#"', f'id="voiceAvatarBtn" href="{voice_url}"')

    return HTMLResponse(content=html)


@app.post("/api/avatar/chat", response_model=ChatResponse)
async def avatar_chat(req: ChatRequest):
    if req.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")

    patient_data = _patient_store[req.patient_id]

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

        print(f"[avatar_chat] Sending to Claude for patient {req.patient_id}...")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
            system=system_prompt,
            messages=messages,
        )

        reply_text = response.content[0].text
        print(f"[avatar_chat] Got response ({len(reply_text)} chars), synthesizing audio...")
        audio_url = await ElevenLabsClient().synthesize(reply_text, f"{req.patient_id}_chat")

        return ChatResponse(response=reply_text, patient_id=req.patient_id, audio_url=audio_url)

    except Exception as exc:
        import traceback
        print(f"[avatar_chat] ERROR: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return ChatResponse(
            response="I'm having a brief technical issue. For urgent questions, please call your care team directly.",
            patient_id=req.patient_id, audio_url=None,
        )


# ─── Background Tasks ─────────────────────────────────────────
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
