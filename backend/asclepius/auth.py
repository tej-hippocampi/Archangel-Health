"""Standalone Asclepius auth (PRD §3, §7.1).

Email/password -> Asclepius JWT (HS256, signed with ``ASCLEPIUS_AUTH_SECRET``).
Completely independent of the clinical/landing/tenant auth planes: its own
secret, its own user table (``asclepius.db``), its own FastAPI dependencies.

Reuses ``PyJWT`` + ``passlib`` (already in requirements) — no new auth library.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, Header, HTTPException

from asclepius.store import AsclepiusStore, get_store, verify_password

log = logging.getLogger("asclepius.auth")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
_PLACEHOLDER = "change-me-asclepius"
_MIN_SECRET_LEN = 16

_cached_secret: Optional[str] = None


def _is_production() -> bool:
    return (os.getenv("ENV") or "").strip().lower() == "production"


def get_asclepius_secret() -> str:
    """Resolve the signing secret. In production a strong ``ASCLEPIUS_AUTH_SECRET``
    is required; in dev we fall back to an ephemeral per-process secret so the
    portal works out of the box (mirrors ``auth_secret.get_auth_secret``)."""
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    raw = (os.getenv("ASCLEPIUS_AUTH_SECRET") or "").strip()
    strong = bool(raw) and raw != _PLACEHOLDER and len(raw) >= _MIN_SECRET_LEN
    if strong:
        _cached_secret = raw
        return _cached_secret
    if _is_production():
        raise RuntimeError(
            "ASCLEPIUS_AUTH_SECRET must be set to a strong (>=16 char) value in production."
        )
    if raw:
        _cached_secret = raw
        return _cached_secret
    _cached_secret = secrets.token_urlsafe(48)
    log.warning(
        "ASCLEPIUS_AUTH_SECRET is not set; generated an ephemeral per-process secret. "
        "Tokens will not survive a restart. Set ASCLEPIUS_AUTH_SECRET for stable sessions."
    )
    return _cached_secret


def create_token(user: Dict[str, Any]) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "typ": "asclepius",
        "sub": user["id"],
        "email": user["email"],
        "role": user["role"],
        "jti": uuid.uuid4().hex,
        "exp": expire,
    }
    return jwt.encode(payload, get_asclepius_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, get_asclepius_secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "asclepius":
        return None
    return payload


def authenticate(store: AsclepiusStore, email: str, password: str) -> Optional[Dict[str, Any]]:
    user = store.get_user_by_email(email)
    if not user or not user.get("active"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "specialty": user.get("specialty"),
        "board_cert": user.get("board_cert"),
        "years_experience": user.get("years_experience"),
    }


# ─── FastAPI dependencies ─────────────────────────────────────────────────────
def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()


def get_current_user_optional(
    authorization: Optional[str] = Header(None),
) -> Optional[Dict[str, Any]]:
    token = _bearer(authorization)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user = get_store().get_user_by_id(payload.get("sub", ""))
    if not user or not user.get("active"):
        return None
    return user


def get_current_user(
    user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="Asclepius authentication required")
    return user


def require_admin(
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def require_qa(
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    if user.get("role") not in ("admin", "qa_reviewer"):
        raise HTTPException(status_code=403, detail="QA reviewer or admin role required")
    return user


def seed_default_admin(store: AsclepiusStore) -> Optional[Dict[str, Any]]:
    """Create a bootstrap admin (and, outside production, a demo evaluator) on
    first boot if the user table is empty.

    Production hardening (FIX 4): in ``ENV=production`` we NEVER seed known
    default credentials. The bootstrap admin is created only when BOTH
    ``ASCLEPIUS_ADMIN_EMAIL`` and ``ASCLEPIUS_ADMIN_PASSWORD`` are explicitly set;
    otherwise we skip seeding with a clear warning. The demo evaluator is never
    seeded in production (``ASCLEPIUS_SEED_DEMO_EVALUATOR`` is ignored there)."""
    if store.count_users() > 0:
        return None

    admin_email = (os.getenv("ASCLEPIUS_ADMIN_EMAIL") or "").strip().lower()
    admin_pw = os.getenv("ASCLEPIUS_ADMIN_PASSWORD")

    if _is_production():
        if not admin_email or not admin_pw:
            log.warning(
                "Asclepius: skipping bootstrap admin seed in production because "
                "ASCLEPIUS_ADMIN_EMAIL and/or ASCLEPIUS_ADMIN_PASSWORD are not set. "
                "Create the first admin explicitly (no default credentials are seeded in prod)."
            )
            return None
        admin = store.create_user(email=admin_email, password=admin_pw, role="admin")
        log.warning(
            "Asclepius: seeded bootstrap admin '%s' from explicit env credentials; "
            "rotate the password after first login.",
            admin_email,
        )
        return admin

    # Non-production: dev/demo convenience defaults (logged as a warning).
    admin_email = admin_email or "admin@asclepius.local"
    admin_pw = admin_pw or "asclepius-admin-2026"
    admin = store.create_user(email=admin_email, password=admin_pw, role="admin")
    log.warning(
        "Asclepius: seeded bootstrap admin '%s' (dev default). Set ASCLEPIUS_ADMIN_EMAIL / "
        "ASCLEPIUS_ADMIN_PASSWORD and rotate immediately.",
        admin_email,
    )
    # A demo evaluator makes the eval screen usable immediately in local/demo.
    if os.getenv("ASCLEPIUS_SEED_DEMO_EVALUATOR", "1").strip().lower() in ("1", "true", "yes", "on"):
        try:
            store.create_user(
                email=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_EMAIL") or "evaluator@asclepius.local"),
                password=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_PASSWORD") or "asclepius-eval-2026"),
                role="evaluator",
                specialty=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_SPECIALTY") or "nephrology"),
                board_cert="board_certified_nephrology",
                years_experience=12,
            )
        except Exception:
            log.warning("Asclepius: failed to seed demo evaluator", exc_info=True)
    return admin
