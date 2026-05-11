"""
CareGuide — Surgical Patient Video Platform
FastAPI backend: EHR → Pipeline → Dashboard → SMS
"""

import asyncio
import os
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

from fastapi import FastAPI, HTTPException, BackgroundTasks, APIRouter, Depends, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# Load env from predictable paths (uvicorn cwd is often repo root, not backend/).
_backend_dir = Path(__file__).resolve().parent
_repo_root = _backend_dir.parent
# override=True so values from these files win over stale shell exports during local dev.
load_dotenv(_repo_root / ".env", override=True)
load_dotenv(_backend_dir / ".env", override=True)

from pipeline.ingest   import IngestLayer
from pipeline.extract  import ExtractionLayer
from pipeline.classify import ClassificationLayer
from pipeline.generate import GenerationLayer
from integrations.elevenlabs   import ElevenLabsClient
from integrations.tavus        import TavusClient
from integrations.twilio_client import TwilioClient
from routers.internal import router as internal_router
from routers.admin    import router as admin_router
from routers.onboarding import router as onboarding_router
from routers.tenant_portal import router as tenant_portal_router
from routers.eligibility import router as eligibility_router
from routers.intraop import router as intraop_router
from routers.postop import router as postop_router
from routers.initial_tier import router as initial_tier_router
from routers.preop_retier import router as preop_retier_router
from routers.triage_explain import router as triage_explain_router
from eligibility import store as elig_store
from staff_context import StaffContext, get_staff_context_optional
from tenant_constants import (
    DEMO_HEALTH_SYSTEM_ID,
    DEMO_HEALTH_SYSTEM_SLUG,
    TRIAGEDM_CLINIC_CODE,
)
from triage_demo_seed import (
    ensure_triage_demo_staff,
    merge_triage_patients_into_store,
    seed_triage_demo_sqlite,
    spinal_fusion_postop_demo_resources,
)
from tenant_jwt import decode_tenant_staff_token
from email_utils import is_email_transport_configured, send_html_email as _send_html_email_impl
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


def _assert_staff_can_access_patient(patient_id: str, staff: Optional[StaffContext]) -> None:
    if staff is None or staff.source != "tenant" or not staff.tenant_id:
        return
    d = _patient_store.get(patient_id)
    if not d or (d.get("health_system_id") or "") != staff.tenant_id:
        raise HTTPException(status_code=404, detail="Patient not found")


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
DEMO_DOCTOR_PASSWORD = "ArchangelDemo2024!"
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
            _patient_store[str(pid)] = entry


def _persist_demo_patient_store() -> None:
    path = _demo_patient_store_snapshot_path()
    if not path:
        return
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        payload = json.dumps(_patient_store, indent=2, default=str, ensure_ascii=False)
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
    merge_triage_patients_into_store(_patient_store, battlecard_fn=_build_demo_battlecard)
    seed_triage_demo_sqlite(_team_store, _patient_store, strategy=_demo_seed_strategy())


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
    Build a clean, image-free recovery resources email body.
    Keeps broad email-client compatibility (Gmail/Outlook/Apple Mail).
    """
    first_name_safe = html_lib.escape(first_name or "Patient")
    clinic_code_safe = html_lib.escape(clinic_code or "N/A")
    resource_code_safe = html_lib.escape(resource_code or "N/A")
    recovery_url_safe = html_lib.escape(recovery_plan_entry_url or "#", quote=True)
    _ = hero_image_src
    _ = logo_image_src

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
                          Your recovery resources are ready
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
                  Your care team has prepared personalized recovery resources for you, including voice explanations and quick reference guides.
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
                  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#64748b;font-size:13px;line-height:1.6;margin-top:6px;">Use these codes to open your personalized recovery dashboard</div>
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
                    View Your Recovery Plan
                  </a>
                </td>
              </tr>

              <tr>
                <td style="padding:0 28px 22px 28px;">
                  <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:12px;padding:13px 14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#334155;font-size:13px;line-height:1.65;text-align:center;">
                    <strong style="color:#0e7490;">Tip:</strong> Save these codes somewhere safe. You can re-open your resources anytime during recovery.
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
) -> str:
    _ = use_local_preview_assets

    html_body = _build_recovery_resources_email_html(
        first_name=first_name,
        clinic_code=clinic_code,
        resource_code=resource_code,
        recovery_plan_entry_url=recovery_plan_entry_url,
        hero_image_src="",
        logo_image_src="",
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
    # Shared public demo account (marketing landing): always authenticate here first so
    # production isn't blocked when DEMO_MODE=0 (no auth user seed) or the same email
    # exists in team_members (tenant SSO path would otherwise return 403 before password check).
    demo_key = DEMO_DOCTOR_EMAIL.lower().strip()
    if body.email.lower().strip() == demo_key:
        user = authenticate_user(body.email, body.password)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = create_access_token(user["email"])
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": UserOut(email=user["email"], name=user.get("name"), role=user.get("role")),
        }
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
@app.get("/api/patient/by-codes")
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
            return {"patient_id": pid, "dashboard_url": f"{base_url}{dashboard_path}"}
    raise HTTPException(status_code=404, detail="No patient found for these codes. Check and try again.")


async def _maybe_trigger_preop_outreach(app: FastAPI) -> None:
    now_m = time.monotonic()
    last = getattr(app.state, "last_preop_outreach_mono", 0.0)
    if now_m - last < 900:
        return
    app.state.last_preop_outreach_mono = now_m
    try:
        await _run_preop_survey_outreach()
    except Exception as e:
        print(f"[preop-outreach] {e}")


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
@app.get("/", response_class=HTMLResponse)
async def doctor_portal(request: Request):
    """Serves the doctor dashboard, or redirects to /admin for the admin subdomain."""
    host = request.headers.get("host", "")
    if "admin." in host:
        return RedirectResponse(url="/admin", status_code=301)
    html_path = os.path.join(os.path.dirname(__file__), "../frontend/doctor.html")
    with open(html_path) as f:
        return HTMLResponse(content=f.read())


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
  location.replace("/");
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
    await _maybe_trigger_preop_outreach(request.app)
    patients = []
    for pid, d in _patient_store.items():
        if d.get("is_draft"):
            continue
        if staff and staff.source == "tenant" and staff.tenant_id:
            if (d.get("health_system_id") or "") != staff.tenant_id:
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
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
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
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
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


@app.get("/api/patient/{patient_id}/timeline")
async def get_patient_timeline(
    patient_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
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
    rows = _team_store.list_escalations()
    out = []
    filter_applied = (
        "surgeon_tier3_only"
        if staff and staff.source == "tenant" and staff.role == "surgeon"
        else None
    )
    for row in rows:
        patient = _patient_store.get(row["patient_id"], {})
        if staff and staff.source == "tenant" and staff.tenant_id:
            if (patient.get("health_system_id") or "") != staff.tenant_id:
                continue
        trigger = row["trigger_type"]
        origin = "Care Team Notification" if str(trigger).startswith("care_team_notification") else "Chat"
        tier_val = row["tier"]
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
    _assert_staff_can_access_patient(row["patient_id"], staff)
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


@app.post("/api/preop-survey/submit")
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
    _assert_staff_can_access_patient(patient_id, staff)

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
    _assert_staff_can_access_patient(patient_id, staff)

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
                first_name=first_name,
                clinic_code=clinic_code,
                resource_code=resource_code,
                recovery_plan_entry_url=recovery_plan_entry_url,
            )
            if not is_email_transport_configured():
                results["email"] = "sendgrid_not_configured"
                print(f"[send] Email skipped — SENDGRID_API_KEY / SMTP not configured. Would send to: {email}")
            else:
                sent_ok = await _send_html_email_impl(
                    email,
                    "Your Recovery Resources Are Ready - Archangel Health",
                    html_body,
                )
                if sent_ok:
                    results["email"] = "sent"
                    _team_store.log_event(patient_id=patient_id, event_type="email_sent", payload={"channel": "email_initial"})
                    print(f"[send] Email sent → {email}")
                else:
                    results["email"] = "error: send_failed"

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
            "health_system_id": health_system_id,
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
            health_system_id=health_system_id,
        )
        _persist_demo_patient_store()

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
        raw_package = {
            "metadata": {
                "patient_id": patient_id,
                "patient_name": input_data.patient_name,
                "phone_number": input_data.phone_number or "",
            },
            "clinical_data": {
                "clinical_notes": input_data.preparation_notes,
                "after_visit_summary": input_data.preparation_notes,
                "pmh": "",
                "procedure_context": input_data.procedure_type or "",
                "medication_list": "",
                "allergies": "",
                "problem_list": "",
            },
        }
        structured_data = await ExtractionLayer().extract(raw_package)
        if input_data.procedure_type and not structured_data.get("procedure_name"):
            structured_data["procedure_name"] = input_data.procedure_type
        if input_data.scheduled_surgery_date:
            structured_data["procedure_date"] = input_data.scheduled_surgery_date
            structured_data["procedure_status"] = "scheduled"
        structured_data["pre_op_instructions"] = (
            structured_data.get("pre_op_instructions")
            or input_data.preparation_notes
        )

        generator = GenerationLayer()
        preop_voice, preop_battlecard = await generator.generate(structured_data, "pre_op")
        preop_audio = await ElevenLabsClient().synthesize(preop_voice, f"{patient_id}_preop")

        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        dashboard_url = f"{base_url}/patient/{patient_id}/pre-op"
        specialty = _specialty_from_procedure(structured_data.get("procedure_name", ""))
        _patient_store[patient_id] = {
            "name": input_data.patient_name,
            "health_system_id": health_system_id,
            "phone": input_data.phone_number or "",
            "email": input_data.email or "",
            "pipeline_type": "pre_op",
            "voice_audio_url": preop_audio,
            "battlecard_html": preop_battlecard,
            "avatar_url": None,
            "voice_script": preop_voice,
            "structured_data": structured_data,
            "clinic_code": clinic_code,
            "resource_code": resource_code,
            "office_phone": office_phone,
            "specialty": specialty,
            "scheduled_surgery_date": structured_data.get("procedure_date", ""),
            "resources": {
                "preop": {
                    "voice_script": preop_voice,
                    "battlecard_html": preop_battlecard,
                    "voice_audio_url": preop_audio,
                }
            },
        }
        _team_store.ensure_episode(
            patient_id=patient_id,
            procedure_type=structured_data.get("procedure_name", ""),
            clinic_code=clinic_code or "",
            resource_code=resource_code or "",
            health_system_id=health_system_id,
        )
        _persist_demo_patient_store()
        return {
            "patient_id": patient_id,
            "dashboard_url": dashboard_url,
            "clinic_code": clinic_code,
            "resource_code": resource_code,
            "preop": {
                "voice_script": preop_voice,
                "battlecard_html": preop_battlecard,
                "voice_audio_url": preop_audio,
            },
            "structured_data": structured_data,
            "specialty": specialty,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pre-op pipeline failed: {exc}")


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
    try:
        raw_package = IngestLayer().process(bundle.model_dump())
        structured_data = await ExtractionLayer().extract(raw_package)
        pipeline_type = ClassificationLayer().classify(structured_data)
        generator = GenerationLayer()
        voice_script, battlecard_html = await generator.generate(structured_data, pipeline_type)
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

    try:
        audio_url = await ElevenLabsClient().synthesize(voice_script, bundle.patient_id)
    except Exception:
        audio_url = None
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
        _patient_store[bundle.patient_id] = prev
    else:
        _patient_store[bundle.patient_id] = {
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
    _team_store.ensure_episode(
        patient_id=bundle.patient_id,
        procedure_type=structured_data.get("procedure_name", ""),
        clinic_code=(_patient_store[bundle.patient_id] or {}).get("clinic_code") or clinic_code_m or "",
        resource_code=(_patient_store[bundle.patient_id] or {}).get("resource_code") or "",
        health_system_id=(_patient_store[bundle.patient_id] or {}).get("health_system_id") or health_system_id,
    )
    _persist_demo_patient_store()
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
    audio_url = await ElevenLabsClient().synthesize(voice_script, patient_id)
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
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Serves the Digital Care Companion conversation interface."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)

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
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Serves the pre-operative preparation page."""
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)
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
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_staff_can_access_patient(patient_id, staff)

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
        from anthropic import Anthropic

        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

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
    return {"doctorId": resolved_doctor_id, "notifications": rows}


@app.patch("/api/doctors/{doctor_id}/notifications/{notif_id}/read")
async def intake_notifications_mark_read(
    doctor_id: str,
    notif_id: str,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
    user: Optional[UserOut] = Depends(get_current_user_optional),
):
    resolved_doctor_id = _resolve_notif_doctor_id(doctor_id, staff, user)
    ok = _team_store.mark_intake_notification_read(resolved_doctor_id, notif_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"ok": True}


@app.post("/api/pre-op/intake/start")
async def preop_intake_start(body: IntakeStartRequest):
    if body.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
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
async def preop_intake_answer(body: IntakeAnswerRequest):
    if body.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
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
async def preop_intake_submit(body: IntakeSubmitRequest):
    if body.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
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
async def preop_notify_care_team(body: CareTeamNotificationRequest):
    if body.patient_id not in _patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
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
    if not _disable_public_demo_account():
        _ensure_demo_doctor()
    await _seed_demo_mode_data()
    _ensure_triage_demo_tenant_seeded()
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
app.include_router(onboarding_router)
app.include_router(tenant_portal_router)
app.include_router(eligibility_router)
app.include_router(intraop_router)
app.include_router(postop_router)
app.include_router(initial_tier_router)
app.include_router(preop_retier_router)
app.include_router(triage_explain_router)


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
