"""
Admin Portal Router
Prefix: /admin
Auth:   Separate admin credentials (ADMIN_USERNAME + ADMIN_PASSWORD in .env)
        Signed JWT with role=admin claim, 24-hour sessions
"""

import json
import os
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
