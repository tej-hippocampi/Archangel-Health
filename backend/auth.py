"""
Elysium Health — Landing / marketing auth.
JWT-based sign-in and registration; in-memory user store with optional file persistence.
"""

import os
import json
import secrets
import string
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple

import pyotp
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr

from token_revocation import is_revoked

# ─── Config ──────────────────────────────────────────────────
AUTH_SECRET = os.getenv("AUTH_SECRET", "change-me-in-production-elysium")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
USERS_FILE = Path(__file__).resolve().parent / "auth_users.json"

# Use pbkdf2_sha256 to avoid platform-specific bcrypt issues and 72-byte limits.
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
security = HTTPBearer(auto_error=False)


def _generate_clinic_code() -> str:
    """Generate a random alphanumeric health system code (e.g. 8 chars); stored as clinic_code in legacy JSON."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ─── Models ───────────────────────────────────────────────────
class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    email: str
    name: Optional[str] = None
    role: Optional[str] = None


class DoctorOnboard(BaseModel):
    name: str
    email: EmailStr
    office_phone: str
    doctor_type: str
    hospital_affiliations: str


class DoctorProfileOut(BaseModel):
    name: str
    email: str
    office_phone: str
    doctor_type: str
    hospital_affiliations: str
    clinic_code: str
    health_system_code: str = ""
    tenant_slug: Optional[str] = None
    is_team_director: bool = False
    role: Optional[str] = None


def _hash_password(password: str) -> str:
    return pwd_context.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _create_token(sub: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": sub, "exp": expire, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, AUTH_SECRET, algorithm=ALGORITHM)


def _decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, AUTH_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if is_revoked(payload.get("jti")):
        return None
    return payload.get("sub")


# ─── MFA pending (pre-auth) token — issued after password, before TOTP ───────
def create_mfa_pending_token(email: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=5)
    payload = {"typ": "mfa_pending", "sub": email.lower().strip(), "exp": expire}
    return jwt.encode(payload, AUTH_SECRET, algorithm=ALGORITHM)


def decode_mfa_pending_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, AUTH_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("typ") != "mfa_pending":
        return None
    return payload.get("sub")


# ─── User store (in-memory + optional file) ────────────────────
def _load_users() -> dict:
    """Load users from file if present. Migrate old records to include role/profile keys."""
    data: dict = {}
    if USERS_FILE.exists():
        try:
            data = json.loads(USERS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    for key, u in data.items():
        if isinstance(u, dict):
            u.setdefault("role", "surgeon")
            # Migrate legacy role tokens in-place so persisted records stay current.
            legacy = (u.get("role") or "").strip().lower()
            if legacy == "doctor" or legacy == "director":
                u["role"] = "surgeon"
            elif legacy == "nurse":
                u["role"] = "rn_coordinator"
            u.setdefault("office_phone", None)
            u.setdefault("doctor_type", None)
            u.setdefault("hospital_affiliations", None)
            u.setdefault("clinic_code", None)
            u.setdefault("mfa_secret", None)
            u.setdefault("mfa_pending_secret", None)
            u.setdefault("mfa_enabled", False)
    return data


def _save_users(users: dict) -> None:
    """Persist users to file."""
    try:
        USERS_FILE.write_text(json.dumps(users, indent=2))
    except OSError:
        pass


_users: dict = {}


def _get_users() -> dict:
    global _users
    if not _users:
        _users = _load_users()
    return _users


def _persist_users() -> None:
    _save_users(_get_users())


def register_user(email: str, password: str, name: Optional[str] = None, role: str = "surgeon") -> dict:
    """Create user. Raises ValueError if email exists. Default role is `surgeon` (pass-4 taxonomy)."""
    users = _get_users()
    key = email.lower().strip()
    if key in users:
        raise ValueError("An account with this email already exists.")
    users[key] = {
        "email": key,
        "password_hash": _hash_password(password),
        "name": (name or "").strip() or None,
        "role": role,
        "office_phone": None,
        "doctor_type": None,
        "hospital_affiliations": None,
        "clinic_code": None,
        "mfa_secret": None,
        "mfa_pending_secret": None,
        "mfa_enabled": False,
    }
    _persist_users()
    return {"email": users[key]["email"], "name": users[key]["name"], "role": users[key]["role"]}


# ─── MFA (TOTP) — opt-in second factor for landing/staff accounts (PRD-3) ─────
MFA_ISSUER = "Archangel Health"


def user_mfa_enabled(email: str) -> bool:
    u = _get_users().get(email.lower().strip())
    return bool(u and u.get("mfa_enabled"))


def mfa_begin_enrollment(email: str) -> Tuple[str, str]:
    """Generate (and persist, pending) a TOTP secret. Returns (secret, otpauth_uri).
    Enrollment is not active until confirmed with a valid code."""
    users = _get_users()
    key = email.lower().strip()
    if key not in users:
        raise ValueError("User not found")
    secret = pyotp.random_base32()
    # Stage the new secret separately so re-enrolling never disturbs (or silently
    # disables) an already-active MFA setup until the new secret is confirmed.
    users[key]["mfa_pending_secret"] = secret
    _persist_users()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=key, issuer_name=MFA_ISSUER)
    return secret, uri


def _verify_code(secret: Optional[str], code: str) -> bool:
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)
    except Exception:
        return False


def mfa_confirm_enrollment(email: str, code: str) -> bool:
    users = _get_users()
    key = email.lower().strip()
    u = users.get(key)
    if not u or not u.get("mfa_pending_secret"):
        return False
    if not _verify_code(u.get("mfa_pending_secret"), code):
        return False
    u["mfa_secret"] = u["mfa_pending_secret"]
    u["mfa_pending_secret"] = None
    u["mfa_enabled"] = True
    _persist_users()
    return True


def mfa_verify(email: str, code: str) -> bool:
    """Verify a TOTP code for an MFA-enabled user (login second step)."""
    u = _get_users().get(email.lower().strip())
    if not u or not u.get("mfa_enabled"):
        return False
    return _verify_code(u.get("mfa_secret"), code)


def mfa_disable(email: str, code: str) -> bool:
    users = _get_users()
    key = email.lower().strip()
    u = users.get(key)
    if not u or not u.get("mfa_enabled"):
        return False
    if not _verify_code(u.get("mfa_secret"), code):
        return False
    u["mfa_secret"] = None
    u["mfa_pending_secret"] = None
    u["mfa_enabled"] = False
    _persist_users()
    return True


def authenticate_user(email: str, password: str) -> Optional[dict]:
    """Return user dict if credentials valid."""
    users = _get_users()
    key = email.lower().strip()
    if key not in users:
        return None
    u = users[key]
    if not _verify_password(password, u["password_hash"]):
        return None
    return {"email": u["email"], "name": u.get("name"), "role": u.get("role")}


def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[UserOut]:
    """Dependency: return current user from Bearer token or None."""
    if not credentials or credentials.scheme != "Bearer":
        return None
    sub = _decode_token(credentials.credentials)
    if not sub:
        return None
    users = _get_users()
    if sub not in users:
        return None
    u = users[sub]
    return UserOut(email=u["email"], name=u.get("name"), role=u.get("role"))


def get_current_user(
    user: Optional[UserOut] = Depends(get_current_user_optional),
) -> UserOut:
    """Dependency: require authenticated user or 401."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def create_access_token(email: str) -> str:
    """Return a JWT for the given user email (e.g. after login/register)."""
    return _create_token(email.lower().strip())


def get_doctor_profile(email: str) -> Optional[dict]:
    """Return surgeon profile for email, or None if not a surgeon or not onboarded.

    The legacy `doctor` token is treated as `surgeon` so users created before
    the pass-4 role migration retain access without re-registering.
    """
    users = _get_users()
    key = email.lower().strip()
    if key not in users:
        return None
    role = (users[key].get("role") or "").strip().lower()
    if role not in ("surgeon", "doctor"):
        return None
    u = users[key]
    clinic_code = u.get("clinic_code")
    if not clinic_code:
        return None
    return {
        "name": u.get("name") or "",
        "email": u["email"],
        "office_phone": u.get("office_phone") or "",
        "doctor_type": u.get("doctor_type") or "",
        "hospital_affiliations": u.get("hospital_affiliations") or "",
        "clinic_code": clinic_code,
        "health_system_code": clinic_code,
    }


def set_doctor_profile(
    email: str,
    name: str,
    office_phone: str,
    doctor_type: str,
    hospital_affiliations: str,
) -> dict:
    """Set doctor profile; generates clinic_code if not set. Returns profile."""
    users = _get_users()
    key = email.lower().strip()
    if key not in users:
        raise ValueError("User not found")
    role = (users[key].get("role") or "").strip().lower()
    if role not in ("surgeon", "doctor"):
        raise ValueError("User is not a surgeon")
    u = users[key]
    u["name"] = (name or "").strip() or None
    u["office_phone"] = (office_phone or "").strip() or None
    u["doctor_type"] = (doctor_type or "").strip() or None
    u["hospital_affiliations"] = (hospital_affiliations or "").strip() or None
    if not u.get("clinic_code"):
        # Generate unique health system code (clinic_code in persisted profile)
        existing = {usr.get("clinic_code") for usr in users.values() if usr.get("clinic_code")}
        for _ in range(20):
            code = _generate_clinic_code()
            if code not in existing:
                u["clinic_code"] = code
                break
        else:
            u["clinic_code"] = _generate_clinic_code()
    _persist_users()
    prof = get_doctor_profile(key) or {}
    if prof.get("clinic_code") and not prof.get("health_system_code"):
        prof["health_system_code"] = prof["clinic_code"]
    return prof
