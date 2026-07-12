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
        # V4 access gate (EHR PRD §9.5): the client uses this to show the
        # "V4 · Real Cases" box unlocked/locked. Serving is enforced server-side
        # regardless — this is display truth, not the gate itself.
        "real_data_approved": bool(user.get("real_data_approved")),
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


def ensure_admin_from_env(store: AsclepiusStore) -> Optional[Dict[str, Any]]:
    """Idempotently provision the operator-specified admin on every boot.

    ``seed_default_admin`` only fires when the user table is EMPTY, so setting
    ``ASCLEPIUS_ADMIN_EMAIL`` / ``ASCLEPIUS_ADMIN_PASSWORD`` after the portal has
    already booted once (and seeded demo/default users) silently has no effect —
    the account is never created and the operator is locked out.

    This closes that gap: whenever BOTH env vars are set, ensure that admin
    account exists with the given password (creating it, or resetting an existing
    account to role='admin', active, matching password). Runs in all
    environments — it is the supported way to (re)gain admin access. No-op when
    either env var is unset (nothing to provision)."""
    admin_email = (os.getenv("ASCLEPIUS_ADMIN_EMAIL") or "").strip().lower()
    admin_pw = os.getenv("ASCLEPIUS_ADMIN_PASSWORD")
    if not admin_email or not admin_pw:
        return None
    admin = store.ensure_admin(email=admin_email, password=admin_pw)
    log.warning(
        "Asclepius: ensured admin account '%s' from ASCLEPIUS_ADMIN_EMAIL/"
        "ASCLEPIUS_ADMIN_PASSWORD. Rotate the password after logging in.",
        admin_email,
    )
    return admin


# ─── Mock / sandbox contributor (internal demo tool) ──────────────────────────
# A stable, credentialed evaluator account an operator can log into on the LIVE
# portal to exercise the latest flow (V3, multimodal cases, …). Its submissions
# are HARD-EXCLUDED from real exports by default and labeled in the admin, so a
# demo never contaminates a shipped training batch. Enabled in all environments
# (it is a safe, isolated sandbox) but can be turned off with
# ASCLEPIUS_MOCK_ENABLED=0. Credentials are env-overridable.
_MOCK_DEFAULT_ID = "mockadmin"
_MOCK_DEFAULT_PASSWORD = "MockContributor-2026"


def mock_enabled() -> bool:
    return (os.getenv("ASCLEPIUS_MOCK_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def mock_credentials() -> Dict[str, Any]:
    """Resolve the mock contributor's login + display profile (env-overridable).

    The login is a plain USERNAME/ID (default ``mockadmin``), not an email —
    ``ASCLEPIUS_MOCK_ID`` sets it (``ASCLEPIUS_MOCK_EMAIL`` still honored for
    back-compat). The portal login accepts a username or an email, so you sign in
    with just the id + password. Stored in the identity column like any login."""
    login_id = (os.getenv("ASCLEPIUS_MOCK_ID")
                or os.getenv("ASCLEPIUS_MOCK_EMAIL")
                or _MOCK_DEFAULT_ID).strip().lower()
    return {
        "enabled": mock_enabled(),
        "email": login_id,   # the login identifier (username or email)
        "password": os.getenv("ASCLEPIUS_MOCK_PASSWORD") or _MOCK_DEFAULT_PASSWORD,
        "specialty": (os.getenv("ASCLEPIUS_MOCK_SPECIALTY") or "nephrology").strip().lower(),
        "board_cert": os.getenv("ASCLEPIUS_MOCK_BOARD_CERT") or "board_certified_nephrology",
        "years_experience": _mock_years(),
        "organization": os.getenv("ASCLEPIUS_MOCK_ORG") or "Archangel Health (Sandbox)",
    }


def _mock_years() -> int:
    try:
        return int(os.getenv("ASCLEPIUS_MOCK_YEARS", "12"))
    except (ValueError, TypeError):
        return 12


def ensure_mock_contributor(store: AsclepiusStore) -> Optional[Dict[str, Any]]:
    """Idempotently provision the mock/sandbox contributor on every boot (no-op
    when ASCLEPIUS_MOCK_ENABLED=0). Safe in production: the account is isolated
    (is_mock=1) and its data never ships in a default export."""
    cfg = mock_credentials()
    if not cfg["enabled"]:
        return None
    user = store.ensure_mock_user(
        email=cfg["email"], password=cfg["password"], specialty=cfg["specialty"],
        board_cert=cfg["board_cert"], years_experience=cfg["years_experience"],
        organization=cfg["organization"],
    )
    log.warning(
        "Asclepius: ensured MOCK contributor '%s' (sandbox; data hard-excluded from "
        "exports). Disable with ASCLEPIUS_MOCK_ENABLED=0.", cfg["email"],
    )
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
            demo = store.create_user(
                email=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_EMAIL") or "evaluator@asclepius.local"),
                password=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_PASSWORD") or "asclepius-eval-2026"),
                role="evaluator",
                specialty=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_SPECIALTY") or "nephrology"),
                board_cert="board_certified_nephrology",
                years_experience=12,
                organization="Riverside Nephrology Associates",
            )
            _seed_demo_contributors(store, demo)
        except Exception:
            log.warning("Asclepius: failed to seed demo evaluator", exc_info=True)
    return admin


def _seed_demo_contributors(store: AsclepiusStore, demo_evaluator: Dict[str, Any]) -> None:
    """Populate the Contributors view in dev/demo: credential profiles (Tier A
    ship + Tier B vault) across two organizations so the org → contributor →
    profile drill-down and the tiered export are demonstrable out of the box.
    Never runs in production (only called from the dev branch above)."""
    # The demo evaluator becomes a fully credentialed nephrologist.
    store.upsert_contributor_credentials(
        id_hashed=demo_evaluator["id_hashed"],
        user_id=demo_evaluator["id"],
        organization="Riverside Nephrology Associates",
        role_title="Physician (MD)",
        credentials_verified=True,
        ship={
            "degree": "MD",
            "board_certifications": "ABIM — Internal Medicine; Nephrology (active)",
            "primary_specialty": "nephrology",
            "subspecialties": ["dialysis", "transplant", "CKD"],
            "years_in_active_practice": 17,
            "active_practice": True,
            "practice_setting_type": "private_practice",
            "languages": ["English", "Spanish"],
            "fellowship_trained": True,
            "fellowship_summary": "fellowship-trained in nephrology at a major US academic medical center",
            "credentials_verified": True,
        },
        verify={
            "full_legal_name": "Jane A. Doe, MD",
            "npi": "1234567893",
            "medical_license_number": "A-104872",
            "license_state": "CA",
            "medical_school": "University of California, San Francisco",
            "medical_school_year": "2004",
            "residency": "Stanford University Medical Center",
            "residency_year": "2007",
            "fellowship": "UCLA Medical Center — Nephrology",
            "fellowship_year": "2009",
            "practice_name": "Riverside Nephrology Associates",
            "practice_address": "1200 Riverside Dr, Suite 300, Sacramento, CA 95814",
            "practice_contact": "jdoe@riversidenephrology.example",
        },
    )

    extra = [
        {
            "email": "npaul.np@asclepius.local",
            "specialty": "nephrology",
            "organization": "Riverside Nephrology Associates",
            "role_title": "Nurse Practitioner",
            "verified": True,
            "ship": {
                "degree": "DNP",
                "board_certifications": "AANP — Adult-Gerontology Acute Care NP (active)",
                "primary_specialty": "nephrology",
                "subspecialties": ["dialysis", "CKD"],
                "years_in_active_practice": 9,
                "active_practice": True,
                "practice_setting_type": "dialysis_unit",
                "languages": ["English"],
                "fellowship_trained": False,
                "credentials_verified": True,
            },
            "verify": {
                "full_legal_name": "Nadia Paul, DNP, AGACNP-BC",
                "npi": "1982736450",
                "medical_license_number": "NP-55821",
                "license_state": "CA",
                "medical_school": "Johns Hopkins School of Nursing",
                "medical_school_year": "2015",
                "practice_name": "Riverside Nephrology Associates",
                "practice_address": "1200 Riverside Dr, Suite 300, Sacramento, CA 95814",
            },
        },
        {
            "email": "rkhan.do@asclepius.local",
            "specialty": "nephrology",
            "organization": "Lakeside Kidney Institute",
            "role_title": "Physician (DO)",
            "verified": True,
            "ship": {
                "degree": "DO",
                "board_certifications": "AOBIM — Nephrology (active)",
                "primary_specialty": "nephrology",
                "subspecialties": ["transplant", "glomerular disease"],
                "years_in_active_practice": 22,
                "active_practice": True,
                "practice_setting_type": "academic",
                "languages": ["English", "Urdu"],
                "fellowship_trained": True,
                "fellowship_summary": "fellowship-trained in transplant nephrology at a major US academic medical center",
                "credentials_verified": True,
            },
            "verify": {
                "full_legal_name": "Rashid Khan, DO",
                "npi": "1457893021",
                "medical_license_number": "D-220194",
                "license_state": "IL",
                "medical_school": "Chicago College of Osteopathic Medicine",
                "medical_school_year": "1999",
                "residency": "Rush University Medical Center",
                "residency_year": "2002",
                "fellowship": "Northwestern Memorial Hospital — Transplant Nephrology",
                "fellowship_year": "2004",
                "practice_name": "Lakeside Kidney Institute",
                "practice_address": "55 Lakeshore Ave, Chicago, IL 60611",
            },
        },
    ]
    for c in extra:
        try:
            u = store.create_user(
                email=c["email"],
                password=secrets.token_urlsafe(24),
                role="evaluator",
                specialty=c["specialty"],
                organization=c["organization"],
            )
            store.upsert_contributor_credentials(
                id_hashed=u["id_hashed"],
                user_id=u["id"],
                organization=c["organization"],
                role_title=c["role_title"],
                credentials_verified=c["verified"],
                ship=c["ship"],
                verify=c["verify"],
            )
        except Exception:
            log.warning("Asclepius: failed to seed demo contributor %s", c["email"], exc_info=True)
