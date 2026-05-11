"""
End-to-end tests for the Intra-Op Reassessment HTTP surface (PRD §8 + Pass-4 §4).

Pass-4 split-role workflow:
  - PATCH on NEW/IN_PROGRESS  → RN coordinator only.
  - mark-ready-for-review     → RN coordinator only.
  - lock                       → surgeon only, requires READY_FOR_SURGEON_REVIEW.
  - reopen                     → admin OR locking surgeon.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("UPLOAD_DIR", "/tmp/elysium-intraop-tests")
os.environ.setdefault("INTRAOP_UPLOAD_DIR", "/tmp/elysium-intraop-tests")
os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402
from triage.intraop.extractor import MockIntraopExtractor  # noqa: E402


@pytest.fixture()
def client():
    """Pass-4: default Bearer is a `surgeon` so read endpoints + lock work.
    Per-call `_rn_headers()` overrides for RN-only writes."""
    app.state.intraop_extractor = MockIntraopExtractor()
    with TestClient(app, headers=auth_headers("surgeon", source="landing")) as c:
        yield c
    app.state.intraop_extractor = None


def _seed_patient(pid: str = None) -> str:
    pid = pid or f"intraop_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
    }
    from triage.intraop.patient_state import ensure_intraop_patient_state
    ensure_intraop_patient_state(app.state.patient_store[pid])
    return pid


# ─── GET / POST / PATCH form ────────────────────────────────────────────────

def test_get_form_returns_null_until_created(client):
    pid = _seed_patient()
    r = client.get(f"/api/episodes/{pid}/intraop-form")
    assert r.status_code == 200
    assert r.json()["form"] is None


def test_post_creates_form_idempotently(client):
    pid = _seed_patient()
    r1 = client.post(
        f"/api/episodes/{pid}/intraop-form",
        json={
            "orStartedAt": "2026-05-08T08:00:00",
            "orEndedAt":   "2026-05-08T09:30:00",
        },
        headers=_rn_headers(),
    )
    assert r1.status_code == 200
    form_id = r1.json()["form"]["id"]
    r2 = client.post(
        f"/api/episodes/{pid}/intraop-form",
        json={},
        headers=_rn_headers(),
    )
    assert r2.json()["form"]["id"] == form_id


def test_patch_autosave_holds_in_progress_until_marked_ready(client):
    """Pass-4: PATCH never auto-jumps to READY_FOR_SURGEON_REVIEW. The RN
    explicitly hands the form over via the new mark-ready-for-review CTA."""
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn_headers(),
    )
    assert r.status_code == 200
    assert r.json()["form"]["status"] == "IN_PROGRESS"
    assert r.json()["missing"] == []


def test_patch_autosave_reports_missing_fields(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"ebl": 100}},
        headers=_rn_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["form"]["status"] == "IN_PROGRESS"
    assert "documented_complication" in body["missing"]


def test_patch_blocked_when_locked(client):
    pid = _seed_patient()
    _post_full_form_and_lock(client, pid)
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"ebl": 200}},
        headers=_rn_headers(),
    )
    assert r.status_code == 409


def test_patch_rejects_surgeon_while_in_progress(client):
    """Pass-4 §4.2: surgeons cannot PATCH the form until RN hands it off."""
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"ebl": 50}},
        # default fixture header is surgeon — explicit 403 expected
    )
    assert r.status_code == 403
    assert "RN coordinator" in r.json()["detail"]


# ─── Mark ready for review / Recall ─────────────────────────────────────────

def test_mark_ready_requires_rn_role(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn_headers(),
    )
    r = client.post(f"/api/episodes/{pid}/intraop-form/mark-ready-for-review")
    assert r.status_code == 403


def test_mark_ready_then_lock_completes_flow(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn_headers(),
    )
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn_headers(),
    )
    assert r.status_code == 200
    assert r.json()["form"]["status"] == "READY_FOR_SURGEON_REVIEW"
    assert r.json()["form"]["draftCompletedBy"]

    r = client.post(f"/api/episodes/{pid}/intraop-form/lock")
    assert r.status_code == 200
    assert r.json()["form"]["status"] == "LOCKED"


def test_mark_ready_validation_blocks_incomplete(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"ebl": 100}},
        headers=_rn_headers(),
    )
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn_headers(),
    )
    assert r.status_code == 422


def test_recall_returns_form_to_in_progress(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn_headers(),
    )
    client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn_headers(),
    )
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/recall",
        headers=_rn_headers(),
    )
    assert r.status_code == 200
    assert r.json()["form"]["status"] == "IN_PROGRESS"
    assert r.json()["form"].get("draftCompletedBy") in (None, "")


# ─── Lock ────────────────────────────────────────────────────────────────────

def test_lock_rejects_unauthenticated(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/lock",
        headers={"Authorization": ""},
    )
    assert r.status_code == 401


def test_lock_rejects_when_form_in_progress(client):
    """Pass-4 §4.5: lock requires READY_FOR_SURGEON_REVIEW."""
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn_headers(),
    )
    r = client.post(f"/api/episodes/{pid}/intraop-form/lock")
    assert r.status_code == 409
    assert "RN coordinator must mark" in r.json()["detail"]


def test_lock_writes_tier_and_reassessment(client):
    pid = _seed_patient()
    _mark_ready_with_hard_upgrade(client, pid)
    r = client.post(f"/api/episodes/{pid}/intraop-form/lock")
    assert r.status_code == 200
    body = r.json()
    assert body["form"]["status"] == "LOCKED"
    assert body["reassessment"]["final_tier"] == "TIER_3"
    assert body["patient"]["currentTier"] == "TIER_3"
    assert body["patient"]["phase"] == "post_op"


def test_lock_idempotent_double_call_returns_409(client):
    pid = _seed_patient()
    _post_full_form_and_lock(client, pid)
    r2 = client.post(f"/api/episodes/{pid}/intraop-form/lock")
    assert r2.status_code == 409


# ─── Admin reopen / surgeon reopen ──────────────────────────────────────────

def test_reopen_requires_admin_token_or_surgeon(client):
    pid = _seed_patient()
    _post_full_form_and_lock(client, pid)
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/reopen",
        headers={"Authorization": ""},
    )
    assert r.status_code == 401


def test_reopen_succeeds_with_admin_token(client):
    pid = _seed_patient()
    _post_full_form_and_lock(client, pid)
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/reopen",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert r.status_code == 200
    assert r.json()["form"]["status"] == "REOPENED"


def test_reopen_succeeds_for_locking_surgeon(client):
    pid = _seed_patient()
    _post_full_form_and_lock(client, pid)
    # The default fixture surgeon (`tester@example.com`) is the locker.
    r = client.post(f"/api/episodes/{pid}/intraop-form/reopen")
    assert r.status_code == 200
    assert r.json()["form"]["status"] == "REOPENED"


def test_reopen_then_relock_appends_history(client):
    pid = _seed_patient()
    _post_full_form_and_lock(client, pid)

    client.post(
        f"/api/episodes/{pid}/intraop-form/reopen",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    # RN edits resume after reopen.
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {
            "documented_complication": True,
            "complication_types": ["VASCULAR_INJURY"],
            "complication_description": "minor bleed",
        }},
        headers=_rn_headers(),
    )
    client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn_headers(),
    )
    r = client.post(f"/api/episodes/{pid}/intraop-form/lock")
    assert r.status_code == 200
    assert r.json()["reassessment"]["final_tier"] == "TIER_3"

    h = client.get(f"/api/episodes/{pid}/intraop-reassessments")
    assert len(h.json()["items"]) == 2


# ─── Live preview ───────────────────────────────────────────────────────────

def test_preview_returns_proposed_and_final_tiers(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    r = client.post(f"/api/episodes/{pid}/intraop-form/preview", json={"fields": {
        "ebl": 600, "transfusion_total_units": 3, "documented_complication": False,
        "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False, "or_duration_minutes": 90,
        "difficult_airway": False, "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
    }})
    assert r.status_code == 200
    body = r.json()
    assert body["currentTier"] == "TIER_1"
    assert body["proposedTier"] == "TIER_3"
    assert body["finalTier"] == "TIER_3"
    assert body["upgradeSteps"] == 2


# ─── Switch to post-op CTA ──────────────────────────────────────────────────

def test_switch_to_postop_creates_form_and_marks_phase(client):
    pid = _seed_patient()
    r = client.post(
        f"/api/episodes/{pid}/switch-to-postop",
        json={"orStartedAt": "2026-05-08T08:00:00", "orEndedAt": "2026-05-08T09:30:00"},
    )
    assert r.status_code == 200
    assert r.json()["patient"]["phase"] == "intra_op"
    assert r.json()["form"]["status"] == "NEW"
    assert r.json()["patient"]["orEndedAt"] == "2026-05-08T09:30:00"


# ─── PDF upload + extraction status ─────────────────────────────────────────

def test_pdf_upload_then_status_polling(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    files = {"file": ("op-note.pdf", b"%PDF-1.4 fake", "application/pdf")}
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/pdf",
        files=files,
        headers=_rn_headers(),
    )
    assert r.status_code == 200
    extraction_id = r.json()["extractionId"]

    import time
    for _ in range(10):
        s = client.get(f"/api/intraop-extractions/{extraction_id}")
        if s.json()["status"] in ("COMPLETE", "FAILED"):
            break
        time.sleep(0.1)
    s = client.get(f"/api/intraop-extractions/{extraction_id}")
    assert s.status_code == 200
    assert s.json()["status"] == "COMPLETE"
    assert "ebl" in s.json()["fields"]


def test_pdf_upload_rejects_oversized(client):
    pid = _seed_patient()
    huge = b"%PDF-1.4 " + b"\x00" * (26 * 1024 * 1024)
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/pdf",
        files={"file": ("op.pdf", huge, "application/pdf")},
        headers=_rn_headers(),
    )
    assert r.status_code == 413


# ─── Reassessment history ───────────────────────────────────────────────────

def test_reassessments_history_returns_locked_event(client):
    pid = _seed_patient()
    _post_full_form_and_lock(client, pid)
    r = client.get(f"/api/episodes/{pid}/intraop-reassessments")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["finalTier"] == "TIER_1"
    assert items[0]["modelVersion"] == "intraop-delta@1.0.0"


# ─── Helpers ────────────────────────────────────────────────────────────────

_FULL_FIELDS = {
    "documented_complication": False, "ebl": 100, "transfusion_total_units": 0,
    "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
    "significant_arrhythmia": False, "or_duration_minutes": 90,
    "difficult_airway": False, "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
}


def _rn_headers():
    return auth_headers("rn_coordinator", source="landing", email="intraop-rn@example.com")


def _post_full_form_and_lock(client, pid: str):
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn_headers(),
    )
    client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn_headers(),
    )
    return client.post(f"/api/episodes/{pid}/intraop-form/lock")


def _mark_ready_with_hard_upgrade(client, pid: str):
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn_headers())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {
            "documented_complication": True,
            "complication_types": ["VASCULAR_INJURY"],
            "complication_description": "minor bleed",
            "ebl": 100, "transfusion_total_units": 0, "conversion": "NO",
            "sustained_hypotension": False, "vasopressor_requirement": "NONE",
            "significant_arrhythmia": False, "or_duration_minutes": 90,
            "difficult_airway": False, "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
        }},
        headers=_rn_headers(),
    )
    client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn_headers(),
    )
