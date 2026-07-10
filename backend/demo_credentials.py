"""Catalog of demo login accounts for admin reference and landing sign-in routing."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from tenant_constants import (
    TRIAGE_DEMO_RN_EMAIL,
    TRIAGE_DEMO_RN_PASSWORD,
    TRIAGE_DEMO_SLUG,
    TRIAGE_DEMO_SURGEON_EMAIL,
    TRIAGE_DEMO_SURGEON_PASSWORD,
    TRIAGEDM_CLINIC_CODE,
)

# Must match DEMO_DOCTOR_EMAIL in main.py (kept here to avoid circular imports).
CEDAR_DEMO_DOCTOR_EMAIL = "manan.vyas@cedarssinai.com"


def _urls() -> Dict[str, str]:
    backend = (os.getenv("BASE_URL") or "http://localhost:8000").rstrip("/")
    landing = (os.getenv("LANDING_URL") or "http://localhost:5173").rstrip("/")
    return {"backend": backend, "landing": landing}


def list_demo_credentials(*, cedar_password: str) -> List[Dict[str, Any]]:
    """Full credential cards for the admin portal (includes passwords)."""
    urls = _urls()
    tenant_sign_in = f"{urls['landing']}/t/{TRIAGE_DEMO_SLUG}/sign-in"
    doctor_sign_in = f"{urls['backend']}/doctor/sign-in"
    doctor_app = f"{urls['backend']}/doctor/app"

    return [
        {
            "id": "triage-director",
            "label": "TRIAGEDM — TEAM Director (Surgeon)",
            "role": "TEAM Director / Surgeon",
            "email": TRIAGE_DEMO_SURGEON_EMAIL,
            "password": TRIAGE_DEMO_SURGEON_PASSWORD,
            "authType": "tenant",
            "tenantSlug": TRIAGE_DEMO_SLUG,
            "healthSystemCode": TRIAGEDM_CLINIC_CODE,
            "signInUrls": {
                "landingTenant": tenant_sign_in,
                "backendDoctor": doctor_sign_in,
                "landingDoctorDialog": f"{urls['landing']}/ (Sign in → Doctor)",
            },
            "redirectAfterLogin": doctor_app,
        },
        {
            "id": "triage-rn",
            "label": "TRIAGEDM — RN Care Coordinator",
            "role": "RN Care Coordinator",
            "email": TRIAGE_DEMO_RN_EMAIL,
            "password": TRIAGE_DEMO_RN_PASSWORD,
            "authType": "tenant",
            "tenantSlug": TRIAGE_DEMO_SLUG,
            "healthSystemCode": TRIAGEDM_CLINIC_CODE,
            "signInUrls": {
                "landingTenant": tenant_sign_in,
                "backendDoctor": doctor_sign_in,
                "landingDoctorDialog": f"{urls['landing']}/ (Sign in → Doctor)",
            },
            "redirectAfterLogin": doctor_app,
        },
        {
            "id": "cedar-public-demo",
            "label": "Cedar Sinai — Public demo doctor",
            "role": "Demo doctor (landing account)",
            "email": CEDAR_DEMO_DOCTOR_EMAIL,
            "password": cedar_password,
            "authType": "landing",
            "tenantSlug": None,
            "healthSystemCode": "CDRSNAI1",
            "signInUrls": {
                "landingDoctorDialog": f"{urls['landing']}/ (Sign in → Doctor)",
            },
            "redirectAfterLogin": doctor_app,
        },
        *_asclepius_mock_account(urls),
    ]


def _asclepius_mock_account(urls: Dict[str, str]) -> List[Dict[str, Any]]:
    """The Asclepius mock/sandbox contributor card (internal demo tool). Lets an
    operator open the LIVE Expert-Evaluation portal and exercise the latest flow
    (V3, multimodal cases) with a real login whose data is HARD-EXCLUDED from
    exports. Returns [] when the sandbox is disabled (ASCLEPIUS_MOCK_ENABLED=0)."""
    try:
        from asclepius.auth import mock_credentials
    except Exception:
        return []
    cfg = mock_credentials()
    if not cfg.get("enabled"):
        return []
    portal = f"{urls['backend']}/asclepius"
    return [
        {
            "id": "asclepius-mock-contributor",
            "label": "Asclepius — Mock Contributor Account",
            "role": "Evaluator · sandbox (data excluded from exports)",
            "email": cfg["email"],
            "password": cfg["password"],
            "authType": "asclepius",
            "tenantSlug": None,
            "healthSystemCode": None,
            # The portal opens as its own page with its own login; type the email +
            # password above. Its submissions never enter a shipped export batch and
            # are labeled "Mock Contributor Account" in the Asclepius admin.
            "signInUrls": {
                "asclepiusPortal": portal,
            },
            "redirectAfterLogin": portal,
        },
    ]


def sign_in_routes(*, cedar_email: str = CEDAR_DEMO_DOCTOR_EMAIL) -> Dict[str, Dict[str, Optional[str]]]:
    """Public email → auth routing hints (no passwords)."""
    routes: Dict[str, Dict[str, Optional[str]]] = {
        TRIAGE_DEMO_SURGEON_EMAIL.lower(): {"type": "tenant", "slug": TRIAGE_DEMO_SLUG},
        TRIAGE_DEMO_RN_EMAIL.lower(): {"type": "tenant", "slug": TRIAGE_DEMO_SLUG},
        cedar_email.lower().strip(): {"type": "landing", "slug": None},
    }
    return {"routes": routes}
