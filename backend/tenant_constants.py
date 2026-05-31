"""Stable IDs for demo multi-tenant wiring (SQLite + patient store)."""

import os

# Same logical tenant for all seeded demo patients (matches DEMO_CLINIC_CODE in main).
DEMO_HEALTH_SYSTEM_ID = "00000000-0000-4000-8000-000000000001"
DEMO_HEALTH_SYSTEM_SLUG = "demo"

# Triage Escalation demo tenant (PRD v1) — isolated from CDRSNAI1 / manan.vyas.
ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID = "00000000-0000-4000-8000-000000000002"
TRIAGE_DEMO_SLUG = "archangel-triage-demo"
TRIAGEDM_CLINIC_CODE = "TRIAGEDM"

TRIAGE_DEMO_SURGEON_EMAIL = "dr.thompson@archangeldemo.com"
TRIAGE_DEMO_SURGEON_PASSWORD = (
    os.getenv("TRIAGE_DEMO_SURGEON_PASSWORD") or "ChangeMeTriageDirector!"
)
TRIAGE_DEMO_RN_EMAIL = "rn.castillo@archangeldemo.com"
TRIAGE_DEMO_RN_PASSWORD = (
    os.getenv("TRIAGE_DEMO_RN_PASSWORD") or "ChangeMeTriageRN!"
)
