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

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ratelimit import rate_limiter
from demo_credentials import list_demo_credentials
from tenant_constants import DEMO_HEALTH_SYSTEM_ID
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
    "General Surgery": "Ask one question at a time and collect structured surgical intake details."
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
    # Strip so .env CRLF / accidental spaces do not break local logins vs production.
    expected_user = (os.getenv("ADMIN_USERNAME") or "").strip()
    expected_pass = (os.getenv("ADMIN_PASSWORD") or "").strip()
    if not expected_user or not expected_pass:
        raise HTTPException(status_code=503, detail="Admin credentials not configured in .env")
    u = (username or "").strip()
    p = password or ""
    user_ok = secrets.compare_digest(u.encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = secrets.compare_digest(p.encode("utf-8"), expected_pass.encode("utf-8"))
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

@router.post("/auth/login", dependencies=[Depends(rate_limiter("admin_login", 10, 60))])
async def admin_login(body: AdminLoginRequest):
    if not _check_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = _create_token()
    internal_secret = os.getenv("INTERNAL_TOOL_SECRET", "")
    return {"token": token, "internal_tool_secret": internal_secret}


@router.get("/demo-credentials")
async def admin_demo_credentials(authorization: Optional[str] = Header(None)):
    """Read-only demo account reference for ops (passwords included)."""
    _verify_token(authorization)
    from main import DEMO_DOCTOR_PASSWORD  # noqa: PLC0415 — avoid import cycle at module load

    return {"accounts": list_demo_credentials(cedar_password=DEMO_DOCTOR_PASSWORD)}


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
    team_store = getattr(request.app.state, "team_store", None)
    recent_patients = []
    for pid, pdata in list(patient_store.items())[-20:]:
        structured = pdata.get("structured_data") or {}
        # Triage Suite Pass 3 §4.4 — three-tier chain on the admin
        # patient detail panel. Hydrate `post_intake_tier` from
        # `episode_snapshots` if the blob has lost it (cold start).
        post_intake_tier = pdata.get("post_intake_tier")
        if post_intake_tier in (None, "") and team_store is not None:
            try:
                snap = team_store.get_episode_snapshot(pid) or {}
                post_intake_tier = snap.get("post_intake_tier")
                if post_intake_tier:
                    pdata["post_intake_tier"] = post_intake_tier
            except Exception:
                post_intake_tier = None
        recent_patients.append({
            "id":        pid,
            "name":      structured.get("patient_name", pid),
            "procedure": structured.get("procedure_name", "—"),
            "status":    structured.get("procedure_status", "—"),
            "initialTier":     pdata.get("initial_tier"),
            "postIntakeTier":  post_intake_tier,
            "currentTier":     pdata.get("current_tier"),
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


INTAKE_SECTION_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "intake_section_prompts"


@router.get("/intake-section-prompts")
async def admin_get_intake_section_prompts(authorization: Optional[str] = Header(None)):
    """Return sample conversation markdown files for intake interview sections 3-10."""
    _verify_token(authorization)
    prompts = {}
    if INTAKE_SECTION_PROMPTS_DIR.is_dir():
        for md_file in sorted(INTAKE_SECTION_PROMPTS_DIR.glob("*.md")):
            match = re.search(r"Section(\d+)", md_file.name)
            if match:
                section_num = match.group(1)
                label = md_file.stem.replace("Sample_Conversation_", "").replace("_", " ")
                prompts[section_num] = {
                    "filename": md_file.name,
                    "label": label,
                    "content": md_file.read_text(encoding="utf-8", errors="replace"),
                }
    return {"prompts": prompts}


@router.post("/health-systems/invite")
async def admin_create_health_system_invite(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Generate a unique onboarding URL for a new health system (internal admin only)."""
    _verify_token(authorization)
    invite_base = (
        os.getenv("LANDING_URL") or os.getenv("BASE_URL") or "http://localhost:5173"
    ).strip().rstrip("/")
    ts = request.app.state.team_store
    return ts.create_health_system_invite(invite_base_url=invite_base)


@router.get("/health-systems")
async def admin_list_health_systems(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """List all health systems (invites + onboarded) with team rosters for customer management."""
    _verify_token(authorization)
    ts = request.app.state.team_store
    rows = ts.list_health_systems_admin()
    out = []
    for r in rows:
        tid = r["id"]
        team = ts.list_team_members(tid)
        out.append({**r, "team": team, "is_demo": tid == DEMO_HEALTH_SYSTEM_ID})
    return {"health_systems": out}


# ─── Triage Logic ────────────────────────────────────────────────────────────

@router.get("/triage/initial-tier/config")
async def admin_get_initial_tier_config(authorization: Optional[str] = Header(None)):
    """Return the read-only tuning snapshot for the Pre-Op Initial Tier algorithm.

    Powers the admin portal's "Triage Logic" tab. Shape is documented in
    `triage/tuning.py::get_config()`.
    """
    _verify_token(authorization)
    from triage import get_config
    return get_config()


@router.get("/triage/preop-retier/config")
async def admin_get_preop_retier_config(authorization: Optional[str] = Header(None)):
    """Return the read-only tuning snapshot for the Pre-Op Re-Tier algorithm.

    Rendered alongside the initial-tier section under the same admin
    "Triage Logic" tab. Shape is documented in
    `triage/preop_retier/tuning.py::get_config()`.
    """
    _verify_token(authorization)
    from triage.preop_retier import get_config
    return get_config()


@router.get("/triage/intraop/config")
async def admin_get_intraop_config(authorization: Optional[str] = Header(None)):
    """Return the read-only tuning snapshot for the Intra-Op Reassessment.

    Rendered as a third section in the admin "Triage Logic" tab. Shape is
    documented in `triage/intraop/tuning.py::get_config()` and includes
    hard upgrades, soft thresholds, per-family P90 OR-time benchmarks,
    the conservative-default policy, and extraction tuning."""
    _verify_token(authorization)
    from triage.intraop import get_config
    return get_config()


@router.get("/triage/postop/config")
async def admin_get_postop_config(authorization: Optional[str] = Header(None)):
    """Return the read-only tuning snapshot for the Post-Op Scoring &
    Re-Tiering algorithm.

    Rendered as a fourth section in the admin "Triage Logic" tab. Shape
    is documented in `triage/postop/tuning.py::get_config()` and includes
    hard escalators, positive contributor weights, engagement-audit
    flags, the delta cap and thresholds, daily check-in / D-X survey /
    med adherence / video / lost-contact configs, and the cron cadence.
    Wound-photo-related entries are intentionally absent (PRD §8 out of
    scope v1)."""
    _verify_token(authorization)
    from triage.postop import get_config
    return get_config()


# ─── Grounding Check Admin ───────────────────────────────────────────────────

@router.get("/grounding/reports")
async def list_grounding_reports_admin(
    request: Request,
    authorization: Optional[str] = Header(None),
    verdict: Optional[str] = None,
    track: Optional[str] = None,
    prompt_version: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
):
    _verify_token(authorization)
    team_store = getattr(request.app.state, "team_store", None)
    if team_store is None:
        return {"reports": []}
    patient_store = getattr(request.app.state, "patient_store", {}) or {}
    rows = team_store.list_grounding_reports(
        limit=min(limit, 500), verdict=verdict, track=track, prompt_version=prompt_version, since=since
    )
    for row in rows:
        pid = row.get("patient_id")
        pdata = patient_store.get(pid) or {}
        sd = pdata.get("structured_data") or {}
        row["patient_name"] = sd.get("patient_name") or pdata.get("name") or pid
    return {"reports": rows}


@router.get("/grounding/reports/{report_id}")
async def get_grounding_report_admin(
    report_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    _verify_token(authorization)
    team_store = getattr(request.app.state, "team_store", None)
    if team_store is None:
        raise HTTPException(status_code=404, detail="Report not found")
    row = team_store.get_grounding_report(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    patient_store = getattr(request.app.state, "patient_store", {}) or {}
    pdata = patient_store.get(row.get("patient_id")) or {}
    sd = pdata.get("structured_data") or {}
    row["patient_name"] = sd.get("patient_name") or pdata.get("name") or row.get("patient_id")
    return row


@router.get("/grounding/stats")
async def grounding_stats_admin(
    request: Request,
    authorization: Optional[str] = Header(None),
    window_days: int = 30,
):
    _verify_token(authorization)
    team_store = getattr(request.app.state, "team_store", None)
    if team_store is None:
        return {"total": 0, "pass": 0, "review": 0, "block": 0}
    return team_store.grounding_summary_stats(window_days=window_days)


@router.get("/grounding/inspector-recall")
async def grounding_inspector_recall_admin(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    _verify_token(authorization)
    team_store = getattr(request.app.state, "team_store", None)
    if team_store is None:
        return {"available": False}
    snap = team_store.get_latest_inspector_recall()
    if not snap:
        return {"available": False, "message": "No inspector recall snapshot yet — run validation suite"}
    return {"available": True, **snap}


@router.get("/ai-calls/stats")
async def ai_call_stats_admin(
    request: Request,
    authorization: Optional[str] = Header(None),
    window_days: int = 30,
):
    _verify_token(authorization)
    team_store = getattr(request.app.state, "team_store", None)
    if team_store is None:
        return {
            "window_days": window_days,
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "by_role": {},
            "models_in_use": [],
        }
    return team_store.llm_call_stats(window_days=window_days)


@router.get("/ai-calls")
async def ai_calls_admin(
    request: Request,
    authorization: Optional[str] = Header(None),
    role: Optional[str] = None,
    prompt_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 200,
):
    _verify_token(authorization)
    team_store = getattr(request.app.state, "team_store", None)
    if team_store is None:
        return {"calls": []}
    calls = team_store.list_llm_calls(
        limit=min(limit, 500),
        role=role,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        since=since,
    )
    patient_store = getattr(request.app.state, "patient_store", {}) or {}
    for row in calls:
        pid = row.get("patient_id")
        pdata = patient_store.get(pid) or {}
        sd = pdata.get("structured_data") or {}
        row["patient_name"] = sd.get("patient_name") or pdata.get("name") or pid
    return {"calls": calls}


@router.get("/ai-calls/prompts")
async def ai_call_prompts_admin(authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    from prompts.registry import PROMPT_REGISTRY, prompt_meta

    return {
        "prompts": [
            {"prompt_id": pid, "label": entry.get("label", pid), **prompt_meta(pid)}
            for pid, entry in PROMPT_REGISTRY.items()
        ]
    }
