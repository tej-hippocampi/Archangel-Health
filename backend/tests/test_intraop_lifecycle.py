"""
Lifecycle tests for an intra-op form (Pass-4):
NEW → IN_PROGRESS → READY_FOR_SURGEON_REVIEW → LOCKED → REOPENED → READY_FOR_SURGEON_REVIEW → LOCKED.

These run against the live FastAPI app via TestClient and verify the
status transitions are surfaced correctly at every step.
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
os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402
from triage.intraop.extractor import MockIntraopExtractor  # noqa: E402


@pytest.fixture()
def client():
    """Pass-4: per-call headers (`_rn`/`_surgeon`) drive role-aware writes."""
    app.state.intraop_extractor = MockIntraopExtractor()
    with TestClient(app) as c:
        yield c
    app.state.intraop_extractor = None


def _seed_patient() -> str:
    pid = f"intraop_lc_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "structured_data": {"procedure_name": "CABG x3"},
    }
    from triage.intraop.patient_state import ensure_intraop_patient_state
    ensure_intraop_patient_state(app.state.patient_store[pid])
    return pid


def _surgeon():
    return auth_headers("surgeon", source="landing")


def _rn():
    return auth_headers("rn_coordinator", source="landing")


_FULL_FIELDS = {
    "documented_complication": False,
    "transfusion_total_units": 0, "conversion": "NO",
    "sustained_hypotension": False, "vasopressor_requirement": "NONE",
    "significant_arrhythmia": False, "or_duration_minutes": 180,
    "difficult_airway": False, "net_fluid_balance": 0,
    "anesthesia_type": "GENERAL", "ebl": 100,
}


def test_full_lifecycle_status_transitions(client):
    pid = _seed_patient()

    # NEW — surgeon switch-to-postop creates the form.
    r = client.post(
        f"/api/episodes/{pid}/switch-to-postop",
        json={"orStartedAt": "2026-05-08T08:00:00", "orEndedAt": "2026-05-08T11:00:00"},
        headers=_surgeon(),
    )
    assert r.json()["form"]["status"] == "NEW"

    # IN_PROGRESS — RN partial autosave.
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"ebl": 100}},
        headers=_rn(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "IN_PROGRESS"

    # IN_PROGRESS still — full universal payload (no auto-jump in pass-4).
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "IN_PROGRESS"

    # READY_FOR_SURGEON_REVIEW — RN explicitly hands off.
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "READY_FOR_SURGEON_REVIEW"
    assert r.json()["form"]["draftCompletedBy"]

    # LOCKED — surgeon-driven reassessment.
    r = client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_surgeon())
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "LOCKED"
    assert r.json()["patient"]["phase"] == "post_op"

    # REOPENED — admin path.
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/reopen",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert r.json()["form"]["status"] == "REOPENED"

    # IN_PROGRESS — RN edits resume after reopen.
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"net_fluid_balance": 100}},
        headers=_rn(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "IN_PROGRESS"

    # READY_FOR_SURGEON_REVIEW — RN re-marks ready.
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "READY_FOR_SURGEON_REVIEW"

    # LOCKED again — second reassessment row appended.
    r = client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_surgeon())
    assert r.status_code == 200
    h = client.get(f"/api/episodes/{pid}/intraop-reassessments", headers=_surgeon())
    assert len(h.json()["items"]) == 2


def test_locked_form_blocks_autosave_until_reopen(client):
    pid = _seed_patient()
    client.post(
        f"/api/episodes/{pid}/switch-to-postop",
        json={"orStartedAt": "2026-05-08T08:00:00", "orEndedAt": "2026-05-08T11:00:00"},
        headers=_surgeon(),
    )
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn(),
    )
    client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn(),
    )
    client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_surgeon())

    # Patch must 409 (LOCKED).
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"ebl": 200}},
        headers=_rn(),
    )
    assert r.status_code == 409

    # After reopen the patch is allowed again.
    client.post(
        f"/api/episodes/{pid}/intraop-form/reopen",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"ebl": 200}},
        headers=_rn(),
    )
    assert r.status_code == 200
    assert r.json()["form"]["fields"]["ebl"] == 200
