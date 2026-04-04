"""
Admin Portal Router
Prefix: /admin
Auth:   Separate admin credentials (ADMIN_USERNAME + ADMIN_PASSWORD in .env)
        Signed JWT with role=admin claim, 24-hour sessions
"""

import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

router = APIRouter(prefix="/admin", tags=["admin"])

ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24
USERS_FILE = Path(__file__).resolve().parent.parent / "auth_users.json"
FORM_LIBRARY_FILE = Path(__file__).resolve().parent.parent / "intake_form_library.json"
FRAMEWORKS_FILE = Path(__file__).resolve().parent.parent / "intake_frameworks.json"
DEFAULT_FORM_LIBRARY_FALLBACK = {
    "General Surgery": {
        "template_name": "General Surgery Intake Form",
        "header_fields": ["Patient Name", "DOB", "MRN", "Procedure", "Surgeon", "Surgery Date"],
        "sections": {"Pre-Op Testing Acknowledgment": [], "Medication Instructions Acknowledged": [], "Day-of-Surgery Prep": [], "Home Preparation Confirmed": [], "Consent Forms": []},
        "final_review_fields": ["Questions from patient / items needing follow-up", "Additional instructions given", "Reviewed by (staff name / role)", "Date", "Patient initials confirming review", "Patient signature"],
    }
}
DEFAULT_FRAMEWORKS_FALLBACK = {
    "General Surgery": "Ask one question at a time and collect PEAR data for surgical intake."
}

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _secret() -> str:
    s = os.getenv("AUTH_SECRET", "change-me")
    return f"admin-{s}"


def _create_token() -> str:
    payload = {
        "role": "admin",
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def _verify_token(authorization: Optional[str]) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Admin token required")
    token = authorization.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Not an admin token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")


def _check_credentials(username: str, password: str) -> bool:
    expected_user = os.getenv("ADMIN_USERNAME", "")
    expected_pass = os.getenv("ADMIN_PASSWORD", "")
    if not expected_user or not expected_pass:
        raise HTTPException(status_code=503, detail="Admin credentials not configured in .env")
    user_ok = secrets.compare_digest(username.encode(), expected_user.encode())
    pass_ok  = secrets.compare_digest(password.encode(), expected_pass.encode())
    return user_ok and pass_ok


def _load_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            return {}
    return {}


# ─── Request Models ───────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    username: str
    password: str


class PromptUpdateRequest(BaseModel):
    content: str


class IntakeTemplateUpdateRequest(BaseModel):
    specialty: str
    template_name: str
    header_fields: list[str]
    sections: dict
    final_review_fields: list[str]


class IntakeFrameworkUpdateRequest(BaseModel):
    specialty: str
    prompt: str


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/auth/login")
async def admin_login(body: AdminLoginRequest):
    if not _check_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = _create_token()
    internal_secret = os.getenv("INTERNAL_TOOL_SECRET", "")
    return {"token": token, "internal_tool_secret": internal_secret}


@router.get("/stats")
async def admin_stats(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    _verify_token(authorization)

    users = _load_users()
    user_list = [
        {
            "email": u.get("email", ""),
            "name":  u.get("name", ""),
            "role":  u.get("role", ""),
            "clinic_code": u.get("clinic_code"),
            "doctor_type": u.get("doctor_type"),
        }
        for u in users.values()
    ]

    # Pull patient store from app state
    patient_store: dict = request.app.state.patient_store if hasattr(request.app.state, "patient_store") else {}
    recent_patients = []
    for pid, pdata in list(patient_store.items())[-20:]:
        structured = pdata.get("structured_data") or {}
        recent_patients.append({
            "id":        pid,
            "name":      structured.get("patient_name", pid),
            "procedure": structured.get("procedure_name", "—"),
            "status":    structured.get("procedure_status", "—"),
        })

    return {
        "registered_users":    len(user_list),
        "active_patients":     len(patient_store),
        "users":               user_list,
        "recent_patients":     list(reversed(recent_patients)),
    }


def _read_json(path: Path, fallback: dict) -> dict:
    if path.exists():
        try:
            val = json.loads(path.read_text())
            if isinstance(val, dict):
                return val
        except Exception:
            pass
    return fallback


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


@router.get("/preop-prompts")
async def admin_get_preop_prompts(authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    from prompts.registry import PROMPT_REGISTRY
    keys = ("preop_voice", "preop_battlecard")
    out = {}
    for key in keys:
        meta = PROMPT_REGISTRY.get(key) or {}
        out[key] = {
            "id": key,
            "label": meta.get("label", key),
            "content": meta.get("content", ""),
            "file": meta.get("file", ""),
            "variable": meta.get("variable", ""),
        }
    return out


@router.patch("/preop-prompts/{prompt_id}")
async def admin_update_preop_prompt(
    prompt_id: str,
    body: PromptUpdateRequest,
    authorization: Optional[str] = Header(None),
):
    _verify_token(authorization)
    if prompt_id not in ("preop_voice", "preop_battlecard"):
        raise HTTPException(status_code=400, detail="Only pre-op prompts are editable here.")
    from prompts.registry import PROMPT_REGISTRY
    meta = PROMPT_REGISTRY.get(prompt_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Prompt not found")
    repo_root = Path(__file__).resolve().parent.parent.parent
    file_path = repo_root / meta["file"]
    if not file_path.exists():
        raise HTTPException(status_code=500, detail=f"Prompt file not found: {meta['file']}")
    variable = meta["variable"]
    content = file_path.read_text()
    pattern = re.compile(
        r"(" + re.escape(variable) + r"\s*=\s*)(\"\"\"|''')(.*?)(\2)",
        re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        raise HTTPException(status_code=500, detail=f"Could not find {variable} in prompt file")
    quote = match.group(2)
    next_content = content[: match.start()] + match.group(1) + quote + body.content + quote + content[match.end():]
    file_path.write_text(next_content)
    PROMPT_REGISTRY[prompt_id]["content"] = body.content
    return {"ok": True, "prompt_id": prompt_id}


@router.get("/intake-form-library")
async def admin_get_intake_form_library(authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    return {"library": _read_json(FORM_LIBRARY_FILE, DEFAULT_FORM_LIBRARY_FALLBACK)}


@router.put("/intake-form-library/{specialty}")
async def admin_update_intake_form_library(
    specialty: str,
    body: IntakeTemplateUpdateRequest,
    authorization: Optional[str] = Header(None),
):
    _verify_token(authorization)
    library = _read_json(FORM_LIBRARY_FILE, DEFAULT_FORM_LIBRARY_FALLBACK)
    library[specialty] = {
        "template_name": body.template_name,
        "header_fields": body.header_fields,
        "sections": body.sections,
        "final_review_fields": body.final_review_fields,
    }
    _write_json(FORM_LIBRARY_FILE, library)
    return {"ok": True, "specialty": specialty}


@router.get("/intake-frameworks")
async def admin_get_intake_frameworks(authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    return {"frameworks": _read_json(FRAMEWORKS_FILE, DEFAULT_FRAMEWORKS_FALLBACK)}


@router.put("/intake-frameworks/{specialty}")
async def admin_update_intake_frameworks(
    specialty: str,
    body: IntakeFrameworkUpdateRequest,
    authorization: Optional[str] = Header(None),
):
    _verify_token(authorization)
    frameworks = _read_json(FRAMEWORKS_FILE, DEFAULT_FRAMEWORKS_FALLBACK)
    frameworks[specialty] = body.prompt
    _write_json(FRAMEWORKS_FILE, frameworks)
    return {"ok": True, "specialty": specialty}
