"""
CareGuide — Surgical Patient Video Platform
FastAPI backend: EHR → Pipeline → Dashboard → SMS
"""

import os
import json
from typing import Optional, List

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
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

# ─── App Setup ────────────────────────────────────────────────
app = FastAPI(
    title="CareGuide Surgical Patient Platform",
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

# ─── Demo Seed ────────────────────────────────────────────────
@app.on_event("startup")
async def _seed_demo_patient() -> None:
    """Pre-populate demo patient so the dashboard works without running the full pipeline."""
    _patient_store["maria_001"] = {
        "name": "Maria L.",
        "phone": "",
        "pipeline_type": "post_op",
        "voice_audio_url": None,
        "battlecard_html": "",
        "avatar_url": None,
        "voice_script": (
            "Hi Maria, I'm your personal care guide from the surgical team. "
            "I'm here to make sure you feel confident and supported as you recover at home. "
            "You just had a lumpectomy and sentinel node biopsy, and you're starting an important "
            "medication called olaparib to protect against the cancer returning. "
            "The most important thing right now: take olaparib 300 milligrams every morning and "
            "300 milligrams every evening — about 12 hours apart — every single day. "
            "I recommend setting phone alarms right now so you never miss a dose. "
            "If you do miss one, just skip it and take the next one on schedule. Never double up. "
            "You may feel some fatigue over the coming days — that's completely normal because "
            "your blood count is slightly low, and your body is healing. "
            "There are a few things that need a call to us right away: "
            "a fever of 100.4 degrees or higher means call us immediately. "
            "Shortness of breath or chest pain means call 911 — don't wait. "
            "Vomiting that won't stop, diarrhea that doesn't improve, or new bruising — call us. "
            "Your follow-up appointment is in about 4 weeks at the oncology clinic "
            "for a blood count recheck. We'll see how your body is tolerating the olaparib. "
            "Before that appointment — or any time — call us if something worries you. "
            "You are never bothering us. Calling early is always the right choice. "
            "You are strong, you have a great team behind you, and you've got this, Maria."
        ),
        "structured_data": {
            "patient_name": "Maria L.",
            "procedure_name": "Lumpectomy + Sentinel Node Biopsy",
            "allergies": ["NKDA"],
            "key_diagnoses": [
                "Triple-negative breast cancer",
                "BRCA1 mutation (germline)",
            ],
            "medications": [
                {"name": "Olaparib", "dose": "300mg", "frequency": "twice daily (12 hrs apart)", "route": "oral", "status": "CONTINUE", "notes": "Take for 12 months at the same times every day."},
                {"name": "Ondansetron", "dose": "8mg", "frequency": "every 8 hrs as needed", "route": "oral", "status": "CONTINUE", "notes": "For nausea — take as soon as you feel off."},
                {"name": "Loperamide", "dose": "2mg after first loose stool, then 2mg each (max 8mg/day)", "frequency": "as needed", "route": "oral", "status": "CONTINUE", "notes": "For diarrhea."},
            ],
            "red_flags": [
                "Fever 100.4°F or higher → call doctor immediately",
                "Shortness of breath or chest pain → call 911 / ER",
                "Vomiting that won't stop → call doctor",
                "Diarrhea that won't improve → call doctor",
                "New or worsening bleeding or bruising → call doctor",
                "Severe dizziness, can't keep fluids down → ER",
                "Worsening fatigue, lightheadedness, racing heart → call doctor",
            ],
            "normal_symptoms": [
                "Mild fatigue (blood count slightly low — Hgb 10.6, mild anemia expected)",
                "Mild soreness at incision site",
                "Light bruising",
            ],
            "post_op_instructions": (
                "Take olaparib exactly as prescribed — do NOT double up missed doses. "
                "No new supplements or herbal products without oncology approval. "
                "Use effective contraception during olaparib therapy. "
                "BRCA1 mutation is genetic — not caused by lifestyle."
            ),
            "primary_concern": "Understanding olaparib schedule and when to call the care team",
            "follow_up": {
                "date": "4 weeks from discharge",
                "provider": "Oncology clinic",
                "notes": "Blood count recheck",
            },
        },
        "resources": None,
    }

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


# ─── Upload Page ──────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def upload_page():
    """Serves the discharge notes upload page for user testing."""
    html_path = os.path.join(os.path.dirname(__file__), "../frontend/upload.html")
    with open(html_path) as f:
        return HTMLResponse(content=f.read())


# ─── New Two-Resource Pipeline ────────────────────────────────
@app.post("/api/process-discharge")
async def process_discharge(input_data: DischargeInput):
    """
    Full two-resource pipeline:
      Raw discharge notes → Extract → Generate (Diagnosis + Treatment)
                         → ElevenLabs audio for each → Store

    Returns both resource sets for immediate display.
    """
    import uuid

    patient_id = input_data.patient_id or f"pt_{uuid.uuid4().hex[:8]}"

    try:
        # 1. Extract structured data from raw discharge notes
        print(f"[pipeline] Starting extraction for {patient_id}...")
        raw_package = {
            "metadata": {
                "patient_id": patient_id,
                "patient_name": input_data.patient_name,
                "phone_number": "",
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

        # 3. Synthesize audio for both via ElevenLabs
        el_client = ElevenLabsClient()
        diag_audio = await el_client.synthesize(
            resources["diagnosis"]["voice_script"], f"{patient_id}_diagnosis"
        )
        treat_audio = await el_client.synthesize(
            resources["treatment"]["voice_script"], f"{patient_id}_treatment"
        )
        print(f"[pipeline] Audio synthesis complete. Storing results...")

        # 4. Build dashboard URL
        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        dashboard_url = f"{base_url}/patient/{patient_id}"

        # 5. Store everything
        _patient_store[patient_id] = {
            "name": input_data.patient_name,
            "phone": "",
            "pipeline_type": "post_op",
            "voice_audio_url": diag_audio,
            "battlecard_html": resources["diagnosis"]["battlecard_html"],
            "avatar_url": None,
            "voice_script": resources["diagnosis"]["voice_script"],
            "structured_data": structured_data,
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

        return {
            "patient_id": patient_id,
            "dashboard_url": dashboard_url,
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
    return {
        "id":            patient_id,
        "name":          d["name"],
        "pipelineType":  d["pipeline_type"],
        "procedure":     d["structured_data"].get("procedure_name", ""),
        "visitDate":     d["structured_data"].get("procedure_date", ""),
        "audioUrl":      d["voice_audio_url"],
        "tavusUrl":      d["avatar_url"],
        "phoneTeam":     os.getenv("CARE_TEAM_PHONE", ""),
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


@app.get("/patient/{patient_id}", response_class=HTMLResponse)
async def patient_dashboard(patient_id: str):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")

    d = _patient_store[patient_id]
    patient_json = {
        "id":           patient_id,
        "name":         d["name"],
        "firstName":    d["name"].split()[0],
        "pipelineType": d["pipeline_type"],
        "procedure":    d["structured_data"].get("procedure_name", ""),
        "visitDate":    d["structured_data"].get("procedure_date", ""),
        "audioUrl":     d.get("voice_audio_url") or "null",
        "tavusUrl":     d.get("avatar_url") or "null",
        "phoneTeam":    os.getenv("CARE_TEAM_PHONE", ""),
        "hasResources": d.get("resources") is not None,
    }

    patient_json_str = json.dumps(patient_json)

    html_path = os.path.join(os.path.dirname(__file__), "../frontend/index.html")
    with open(html_path) as f:
        html = f.read()

    inject = f"<script>window.__PATIENT__ = {patient_json_str};</script>"
    html = html.replace("</head>", f"{inject}\n</head>")
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
