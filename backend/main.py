"""
CareGuide — Surgical Patient Video Platform
FastAPI backend: EHR → Pipeline → Dashboard → SMS
"""

import asyncio
import os
import json
import secrets
import string
from typing import Optional, List

from fastapi import FastAPI, HTTPException, BackgroundTasks, APIRouter, Depends
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
async def doctor_portal():
    """Serves the doctor dashboard — patient roster + add patient."""
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

            codes_block = ""
            if clinic_code or resource_code:
                codes_block = (
                    f'<p style="font-size:15px;color:#374151;line-height:1.6;margin-bottom:12px;">'
                    f'Your access codes (save these):</p>'
                    f'<p style="font-size:14px;color:#111827;margin-bottom:8px;">'
                    f'<strong>Clinic Code:</strong> <b style="font-size:16px;letter-spacing:.05em;">{clinic_code or "—"}</b></p>'
                    f'<p style="font-size:14px;color:#111827;margin-bottom:16px;">'
                    f'<strong>Resource Code:</strong> <b style="font-size:16px;letter-spacing:.05em;">{resource_code or "—"}</b></p>'
                )
            html_body = f"""
            <div style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto;padding:24px;">
                <div style="background:linear-gradient(135deg,#1A3C8F,#2563EB);color:#fff;padding:24px;border-radius:12px;text-align:center;margin-bottom:20px;">
                    <h1 style="font-size:20px;margin-bottom:6px;">Archangel Health</h1>
                    <p style="font-size:14px;opacity:.85;">Your Recovery Resources Are Ready</p>
                </div>
                <p style="font-size:15px;color:#374151;line-height:1.6;margin-bottom:16px;">
                    Hi {first_name}, your care team has prepared personalized recovery resources for you, including voice explanations and quick reference guides.
                </p>
                {codes_block}
                <a href="{recovery_plan_entry_url}" style="display:block;text-align:center;background:#2563EB;color:#fff;padding:14px 24px;border-radius:10px;text-decoration:none;font-weight:600;font-size:15px;margin-bottom:16px;">
                    View Your Recovery Plan
                </a>
                <p style="font-size:13px;color:#6B7280;text-align:center;">Use your Clinic Code and Resource Code above to access your plan. Best viewed on a computer or tablet.</p>
            </div>
            """

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
    app.mount("/audio", StaticFiles(directory="/tmp"), name="audio")
except Exception:
    pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
