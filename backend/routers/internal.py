"""
Internal Prompt Lab Router
Prefix: /internal
Auth:   Authorization: Bearer {INTERNAL_TOOL_SECRET}
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional
from uuid import uuid4

import anthropic
from anthropic import APIConnectionError as AnthropicConnectionError, APIStatusError as AnthropicStatusError
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from prompts.registry import PROMPT_REGISTRY
from pipeline.generate import GenerationLayer
from integrations.elevenlabs import ElevenLabsClient

router = APIRouter(prefix="/internal", tags=["internal"])

# ─── Auth ────────────────────────────────────────────────────────────────────

def _check_auth(authorization: Optional[str]) -> None:
    secret = os.getenv("INTERNAL_TOOL_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="INTERNAL_TOOL_SECRET not configured")
    expected = f"Bearer {secret}"
    if not authorization or authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─── Sample Patient Fixtures ─────────────────────────────────────────────────

SAMPLE_FIXTURES = {
    "cardiac_post_op": {
        "label": "Cardiac Cath — Post-Op Discharge",
        "procedure": "Cardiac Catheterization",
        "data": {
            "patient_name": "James Harrington",
            "procedure_name": "Cardiac Catheterization with Stent Placement",
            "procedure_date": "2025-03-10",
            "procedure_status": "completed",
            "key_diagnoses": ["Coronary Artery Disease", "Single Vessel Disease — LAD"],
            "medications": [
                {"name": "Aspirin", "dose": "81mg", "frequency": "daily", "route": "oral", "status": "new", "notes": "Do not stop without calling cardiologist"},
                {"name": "Clopidogrel", "dose": "75mg", "frequency": "daily", "route": "oral", "status": "new", "notes": "Critical — dual antiplatelet therapy"},
                {"name": "Atorvastatin", "dose": "40mg", "frequency": "nightly", "route": "oral", "status": "new", "notes": ""},
                {"name": "Metoprolol", "dose": "25mg", "frequency": "twice daily", "route": "oral", "status": "new", "notes": ""},
            ],
            "red_flags": [
                "Chest pain or pressure",
                "Bleeding or large bruise at groin/wrist access site",
                "Leg swelling, redness, or warmth",
                "Shortness of breath at rest",
                "Fever above 101°F",
            ],
            "normal_symptoms": [
                "Small bruise at catheter insertion site",
                "Mild soreness at access site for 2-3 days",
                "Fatigue for 24-48 hours",
            ],
            "pre_op_instructions": "",
            "post_op_instructions": "Keep access site dry for 48 hours. No heavy lifting over 10 lbs for 5 days. No driving for 24 hours. Take all medications as prescribed.",
            "diet_instructions": "Low sodium, heart-healthy diet. Limit saturated fats.",
            "activity_restrictions": "No strenuous activity for 5 days. Short walks encouraged starting day 2.",
            "wound_care": "Keep bandage on for 24 hours. Watch for bleeding, swelling, or warmth.",
            "allergies": ["Penicillin"],
            "follow_up": {"date": "2025-03-17", "provider": "Dr. Patel, Cardiology", "notes": "Bring medication list"},
            "note_type": "discharge_note",
            "missing_critical_data": [],
        },
    },
    "ortho_post_op": {
        "label": "Knee Replacement — Post-Op Discharge",
        "procedure": "Total Knee Replacement",
        "data": {
            "patient_name": "Sandra Okafor",
            "procedure_name": "Right Total Knee Arthroplasty",
            "procedure_date": "2025-03-08",
            "procedure_status": "completed",
            "key_diagnoses": ["Severe Osteoarthritis — Right Knee", "Chronic Knee Pain"],
            "medications": [
                {"name": "Oxycodone", "dose": "5mg", "frequency": "every 6 hours as needed", "route": "oral", "status": "new", "notes": "Take with food. Do not drive."},
                {"name": "Ibuprofen", "dose": "600mg", "frequency": "every 8 hours with food", "route": "oral", "status": "new", "notes": "Take scheduled, not just as needed"},
                {"name": "Aspirin", "dose": "325mg", "frequency": "daily", "route": "oral", "status": "new", "notes": "Blood clot prevention"},
                {"name": "Enoxaparin", "dose": "40mg", "frequency": "daily injection", "route": "subcutaneous", "status": "new", "notes": "Nurse will teach injection technique"},
            ],
            "red_flags": [
                "Sudden severe calf pain or swelling (possible blood clot)",
                "Knee dramatically more swollen than day before",
                "Fever above 101.5°F",
                "Wound opening, drainage, or foul smell",
                "Numbness or tingling in foot",
            ],
            "normal_symptoms": [
                "Swelling around knee and lower leg for up to 3 months",
                "Bruising from mid-thigh to ankle",
                "Clicking or clunking sounds in knee",
                "Warmth around knee",
                "Difficulty sleeping due to discomfort",
            ],
            "pre_op_instructions": "",
            "post_op_instructions": "Use walker at all times when walking. Do PT exercises 3x daily. Elevate leg above heart level when resting. Ice 20 min every 2 hours.",
            "diet_instructions": "High protein diet to support healing. Stay well hydrated.",
            "activity_restrictions": "No driving until cleared by surgeon. No kneeling. Weight bearing as tolerated with walker.",
            "wound_care": "Keep incision dry for 5 days. Staples removed at 2-week visit.",
            "allergies": ["Sulfa drugs", "Latex"],
            "follow_up": {"date": "2025-03-22", "provider": "Dr. Kim, Orthopedic Surgery", "notes": "Bring list of current medications. PT referral will be sent."},
            "note_type": "discharge_note",
            "missing_critical_data": [],
        },
    },
    "general_pre_op": {
        "label": "Appendectomy — Pre-Op Prep",
        "procedure": "Laparoscopic Appendectomy",
        "data": {
            "patient_name": "Marcus Webb",
            "procedure_name": "Laparoscopic Appendectomy",
            "procedure_date": "2025-03-18",
            "procedure_status": "scheduled",
            "key_diagnoses": ["Acute Appendicitis"],
            "medications": [
                {"name": "Metformin", "dose": "500mg", "frequency": "twice daily", "route": "oral", "status": "hold", "notes": "Stop 48 hours before surgery"},
                {"name": "Lisinopril", "dose": "10mg", "frequency": "daily", "route": "oral", "status": "continue", "notes": "Take morning of surgery with sip of water"},
            ],
            "red_flags": [
                "Worsening abdominal pain before surgery date — call ER",
                "Fever above 100.4°F before surgery",
                "Vomiting that prevents taking medications",
                "Any new symptoms overnight",
            ],
            "normal_symptoms": [
                "Mild abdominal discomfort",
                "Anxiety about surgery",
            ],
            "pre_op_instructions": "Nothing to eat after midnight. Clear liquids until 4 hours before surgery. Shower with antibacterial soap the night before and morning of surgery. Arrive 2 hours before scheduled time.",
            "post_op_instructions": "",
            "diet_instructions": "Clear liquids only after midnight. No solid food.",
            "activity_restrictions": "No strenuous activity day before surgery. Arrange driver — you cannot drive yourself home.",
            "wound_care": "",
            "allergies": ["Penicillin", "NSAIDs"],
            "follow_up": {"date": "2025-03-25", "provider": "Dr. Nguyen, General Surgery", "notes": "Post-op wound check"},
            "note_type": "pre_op_note",
            "missing_critical_data": [],
        },
    },
}

VOICE_OPTIONS = [
    {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Bella — Soft, female (Default)"},
    {"id": "21m00Tcm4TlvDq8ikWAM", "label": "Rachel — Calm, female"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "label": "Domi — Strong, female"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Bella — Soft, female"},
    {"id": "ErXwobaYiN019PkySvjV", "label": "Antoni — Well-rounded, male"},
    {"id": "VR6AewLTigWG4xSOukaG", "label": "Arnold — Crisp, male"},
    {"id": "pNInz6obpgDQGcFmaJgB", "label": "Adam — Deep, male"},
    {"id": "yoZ06aMxZJJ28mfd3POQ", "label": "Sam — Raspy, male"},
]


# ─── Request Models ───────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    prompt_id: str
    system_prompt: str
    discharge_notes: str
    preview_words: int = 75
    voice_id: Optional[str] = None
    test_message: str = "What medications should I be taking, and when?"


class SavePromptRequest(BaseModel):
    content: str


class GitPushRequest(BaseModel):
    prompt_id: str
    commit_message: str


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/prompts")
async def list_prompts(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return [
        {"id": pid, "label": meta["label"], "content": meta["content"], "type": meta["type"]}
        for pid, meta in PROMPT_REGISTRY.items()
    ]


@router.get("/samples")
async def list_samples(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return [
        {"key": key, "label": fix["label"], "procedure": fix["procedure"]}
        for key, fix in SAMPLE_FIXTURES.items()
    ]


@router.get("/voices")
async def list_voices(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return VOICE_OPTIONS + [{"id": "custom", "label": "Custom Voice ID..."}]


@router.post("/run")
async def run_prompt(body: RunRequest, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)

    if body.prompt_id not in PROMPT_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown prompt_id: {body.prompt_id}")
    if not body.discharge_notes.strip():
        raise HTTPException(status_code=422, detail="Discharge notes cannot be empty")

    meta = PROMPT_REGISTRY[body.prompt_id]
    prompt_type = meta["type"]

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    client = anthropic.AsyncAnthropic(api_key=api_key)

    # Avatar chat — combine behavior template + discharge notes as patient context, then test a message
    if prompt_type == "avatar":
        system = (
            body.system_prompt
            .replace("[PATIENT_NAME]", "the patient")
            .replace("[PROCEDURE]", "their recent procedure")
            .replace("[PATIENT_RECORDS]", f"## Patient Clinical Notes\n\n{body.discharge_notes}")
        )
        try:
            message = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=150,
                system=system,
                messages=[{"role": "user", "content": body.test_message}],
            )
        except AnthropicConnectionError as e:
            raise HTTPException(status_code=503, detail=f"Anthropic connection error — check network/API key: {e}")
        except AnthropicStatusError as e:
            raise HTTPException(status_code=502, detail=f"Anthropic API error {e.status_code}: {e.message}")

        response_text = message.content[0].text

        # Synthesize the avatar response as audio
        eleven = ElevenLabsClient()
        voice_id = body.voice_id if body.voice_id and body.voice_id != "custom" else None
        audio_url = await eleven.synthesize_preview(
            script=response_text,
            patient_id=f"avatar_{uuid4().hex[:8]}",
            max_words=100,
            voice_id=voice_id,
        )

        return {
            "type": "avatar",
            "response": response_text,
            "audio_url": audio_url,
            "test_message": body.test_message,
        }

    max_tokens = 1500 if prompt_type == "voice" else 8000

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=body.system_prompt,
            messages=[{"role": "user", "content": body.discharge_notes}],
        )
    except AnthropicConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Anthropic connection error — check network/API key: {e}")
    except AnthropicStatusError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error {e.status_code}: {e.message}")
    full_text = message.content[0].text

    # Battlecard prompt — return rendered HTML only, no audio
    if prompt_type == "battlecard":
        return {
            "type": "battlecard",
            "battlecard_html": full_text,
        }

    # Voice prompt — return script + 30-second audio preview only
    words = full_text.split()
    preview_words = min(body.preview_words, len(words))
    was_truncated = len(words) > preview_words
    preview_text = " ".join(words[:preview_words])
    if was_truncated:
        preview_text += "..."

    eleven = ElevenLabsClient()
    preview_id = f"preview_{uuid4().hex[:8]}"
    voice_id = body.voice_id if body.voice_id and body.voice_id != "custom" else None
    audio_url = await eleven.synthesize_preview(
        script=preview_text,
        patient_id=preview_id,
        max_words=preview_words,
        voice_id=voice_id,
    )

    words_per_second = 2.5
    estimated_seconds = round(preview_words / words_per_second)

    return {
        "type": "voice",
        "voice_script_full": full_text,
        "voice_script_preview": preview_text,
        "audio_url": audio_url,
        "word_count": preview_words,
        "estimated_seconds": estimated_seconds,
        "was_truncated": was_truncated,
    }


@router.patch("/prompts/{prompt_id}")
async def save_prompt(
    prompt_id: str,
    body: SavePromptRequest,
    authorization: Optional[str] = Header(None),
):
    _check_auth(authorization)

    if prompt_id not in PROMPT_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown prompt_id: {prompt_id}")

    meta = PROMPT_REGISTRY[prompt_id]
    variable = meta["variable"]
    file_path = meta["file"]

    # Resolve path relative to repo root (two levels up from backend/routers/)
    repo_root = Path(__file__).resolve().parent.parent.parent
    abs_path = repo_root / file_path

    if not abs_path.exists():
        raise HTTPException(status_code=500, detail=f"Prompt file not found: {file_path}")

    content = abs_path.read_text(encoding="utf-8")

    # Match triple-quoted string (""" or ''') assigned to the variable
    pattern = re.compile(
        r'(' + re.escape(variable) + r'\s*=\s*)("""|\'\'\')(.*?)(\2)',
        re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        raise HTTPException(
            status_code=500,
            detail=f"Could not locate {variable} triple-quoted string in {file_path}",
        )

    quote_style = match.group(2)
    new_content = (
        content[: match.start()]
        + match.group(1)
        + quote_style
        + body.content
        + quote_style
        + content[match.end():]
    )
    abs_path.write_text(new_content, encoding="utf-8")

    # Update in-memory registry immediately
    PROMPT_REGISTRY[prompt_id]["content"] = body.content

    return {"success": True, "prompt_id": prompt_id, "file_written": file_path}


@router.post("/git/push")
async def git_push(body: GitPushRequest, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)

    repo_root = str(Path(__file__).resolve().parent.parent.parent)
    combined_output = ""
    combined_error = ""

    steps = [
        ["git", "add", "backend/prompts/"],
        ["git", "commit", "-m", body.commit_message],
        ["git", "push", "origin", "HEAD"],
    ]

    for cmd in steps:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        combined_output += result.stdout + "\n"
        combined_error += result.stderr + "\n"
        if result.returncode != 0:
            return {
                "success": False,
                "output": combined_output.strip(),
                "error": combined_error.strip(),
            }

    return {
        "success": True,
        "output": combined_output.strip(),
        "error": combined_error.strip(),
    }
