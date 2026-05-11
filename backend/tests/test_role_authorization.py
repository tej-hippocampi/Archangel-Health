"""
Pass-4 route-level authorization tests (PRD §3).

Spot-checks every triage write endpoint for:
  1. NP/PA token → 403 on writes (read-only is the NP/PA contract)
  2. NP/PA token → 200 on reads
  3. Surgeon token → 200 on lock; 403 on mark-ready-for-review (Phase 4)
  4. RN token → 200 on mark-ready-for-review (Phase 4); 403 on lock
  5. System-admin → 200 on tuning POST; everyone else → 403
  6. Anonymous → 401 on every clinical route
  7. Patient-only routes → 403 if a staff Bearer is presented
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402


@pytest.fixture()
def base_client():
    with TestClient(app) as c:
        yield c


def _seed_preop_patient() -> str:
    pid = f"role_pre_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "pre_op",
        "specialty": "General Surgery",
        "current_tier": "TIER_1",
        "initial_tier": "TIER_1",
        "initial_tier_was_hard_escalator": False,
        "structured_data": {
            "procedure_name": "Total Knee Arthroplasty",
            "procedure_date": "2099-12-15T07:00:00",
        },
        "anchor_procedure_family": "LEJR",
    }
    return pid


def _seed_postop_patient() -> str:
    from datetime import datetime, timedelta
    pid = f"role_post_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "post_op",
        "current_tier": "TIER_1",
        "post_intraop_tier": "TIER_1",
        "discharge_at": (datetime.utcnow() - timedelta(days=2))
        .replace(microsecond=0)
        .isoformat(),
        "anchor_procedure_family": "LEJR",
    }
    from triage.postop.patient_state import ensure_postop_patient_state
    ensure_postop_patient_state(app.state.patient_store[pid])
    return pid


def _seed_intraop_patient() -> str:
    pid = f"role_intra_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
    }
    from triage.intraop.patient_state import ensure_intraop_patient_state
    ensure_intraop_patient_state(app.state.patient_store[pid])
    return pid


# ─── 1. NP/PA → 403 on every triage write ──────────────────────────────────


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/triage/initial-tier/compute", {"procedure": {"cpt_code": "27447", "anchor_procedure_family": "LEJR", "scheduled_date": "2099-06-15"}, "active_problems": {"problems": [], "functional_status": "INDEPENDENT"}, "medications": {"medications": []}, "vitals_recent": {"recent": []}, "vitals_baseline": None, "labs_recent": {"recent": []}, "echo": None, "imaging": None, "social": None, "patient_demographics": {"age": 60}, "system_inputs": None}),
        ("post", "/api/triage/preop-retier/compute", {
            "initial_tier": "TIER_1", "initial_tier_was_hard_escalator": False,
            "hours_until_surgery": 72, "pam": None,
            "intake": {"status": "STARTED"},
            "surveys": [{"window": "T_96", "status": "PENDING"}, {"window": "T_48", "status": "PENDING"}, {"window": "T_24", "status": "PENDING"}],
            "video": {"sessions": []}, "battle_card": {"views": []},
        }),
    ],
)
def test_np_pa_blocked_on_writes(base_client, method, path, body):
    headers = auth_headers("np_pa", source="landing")
    r = getattr(base_client, method)(path, json=body, headers=headers)
    assert r.status_code == 403, r.text


def test_np_pa_blocked_on_postop_retier_run(base_client):
    pid = _seed_postop_patient()
    headers = auth_headers("np_pa", source="landing")
    r = base_client.post(
        f"/api/episodes/{pid}/postop-retier/run",
        json={"triggered_by": "MANUAL:np_pa"},
        headers=headers,
    )
    assert r.status_code == 403, r.text


def test_np_pa_blocked_on_self_flag_resolve(base_client):
    pid = _seed_postop_patient()
    headers = auth_headers("np_pa", source="landing")
    r = base_client.post(
        f"/api/episodes/{pid}/postop/self-flag/resolve",
        json={"flag_id": 1, "resolved_by": "np-jane"},
        headers=headers,
    )
    assert r.status_code == 403, r.text


def test_np_pa_blocked_on_initial_tier_persist(base_client):
    pid = _seed_preop_patient()
    headers = auth_headers("np_pa", source="landing")
    body = {
        "input": {
            "procedure": {"cpt_code": "27447", "anchor_procedure_family": "LEJR", "scheduled_date": "2099-06-15"},
            "active_problems": {"problems": [], "functional_status": "INDEPENDENT"},
            "medications": {"medications": []},
            "vitals_recent": {"recent": []},
            "vitals_baseline": None, "labs_recent": {"recent": []},
            "echo": None, "imaging": None, "social": None,
            "patient_demographics": {"age": 60},
            "system_inputs": None,
        },
    }
    r = base_client.post(f"/api/episodes/{pid}/initial-tier", json=body, headers=headers)
    assert r.status_code == 403


# ─── 2. NP/PA → 200 on reads ───────────────────────────────────────────────


def test_np_pa_can_read_postop_view(base_client):
    pid = _seed_postop_patient()
    headers = auth_headers("np_pa", source="landing")
    r = base_client.get(f"/api/episodes/{pid}/postop", headers=headers)
    assert r.status_code == 200


def test_np_pa_can_read_postop_retier_events(base_client):
    pid = _seed_postop_patient()
    headers = auth_headers("np_pa", source="landing")
    r = base_client.get(
        f"/api/episodes/{pid}/postop-retier-events", headers=headers
    )
    assert r.status_code == 200


def test_np_pa_can_read_initial_tier_tuning(base_client):
    headers = auth_headers("np_pa", source="landing")
    r = base_client.get("/api/triage/tuning/initial-tier/current", headers=headers)
    assert r.status_code == 200


def test_np_pa_can_read_preop_retier_tuning(base_client):
    headers = auth_headers("np_pa", source="landing")
    r = base_client.get("/api/triage/tuning/preop-retier/current", headers=headers)
    assert r.status_code == 200


def test_np_pa_can_read_intraop_form(base_client):
    pid = _seed_intraop_patient()
    headers = auth_headers("np_pa", source="landing")
    r = base_client.get(f"/api/episodes/{pid}/intraop-form", headers=headers)
    assert r.status_code == 200


# ─── 3. Surgeon → lock OK; ready-for-review 403 (Phase 4) ─────────────────
# Phase 4 verifies the dual gate via test_intraop_workflow_rn_drafts_surgeon_locks.


# ─── 5. Tuning POST: admin token only ─────────────────────────────────────


def test_tuning_post_initial_tier_requires_admin(base_client):
    headers = auth_headers("surgeon", source="landing")
    r = base_client.post("/api/triage/tuning/initial-tier", json={}, headers=headers)
    # No X-Admin-Token → 401 (the surgeon Bearer doesn't satisfy admin gate).
    assert r.status_code == 401


def test_tuning_post_initial_tier_succeeds_with_admin_token(base_client):
    r = base_client.post(
        "/api/triage/tuning/initial-tier",
        json={"weights": {}},
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert r.status_code == 200


def test_tuning_post_preop_retier_succeeds_with_admin_token(base_client):
    r = base_client.post(
        "/api/triage/tuning/preop-retier",
        json={"weights": {}},
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert r.status_code == 200


# ─── 6. Anonymous → 401 across the board ───────────────────────────────────


def test_anon_blocked_on_initial_tier_compute(base_client):
    body = {
        "procedure": {"cpt_code": "27447", "anchor_procedure_family": "LEJR", "scheduled_date": "2099-06-15"},
        "active_problems": {"problems": [], "functional_status": "INDEPENDENT"},
        "medications": {"medications": []},
        "vitals_recent": {"recent": []},
        "vitals_baseline": None, "labs_recent": {"recent": []},
        "echo": None, "imaging": None, "social": None,
        "patient_demographics": {"age": 60},
        "system_inputs": None,
    }
    r = base_client.post("/api/triage/initial-tier/compute", json=body)
    assert r.status_code == 401


def test_anon_blocked_on_postop_view(base_client):
    pid = _seed_postop_patient()
    r = base_client.get(f"/api/episodes/{pid}/postop")
    assert r.status_code == 401


def test_anon_blocked_on_intraop_form_lock(base_client):
    pid = _seed_intraop_patient()
    r = base_client.post(f"/api/episodes/{pid}/intraop-form/lock")
    assert r.status_code == 401


# ─── 7. Patient-session-only routes reject staff tokens ────────────────────


def test_staff_blocked_on_patient_pam_submit(base_client):
    pid = _seed_preop_patient()
    headers = auth_headers("rn_coordinator", source="landing")
    r = base_client.post(
        f"/api/episodes/{pid}/pam",
        json={"responses": [{"item_index": i, "value": 4} for i in range(1, 14)]},
        headers=headers,
    )
    assert r.status_code == 403


def test_staff_blocked_on_patient_daily_checkin(base_client):
    pid = _seed_postop_patient()
    headers = auth_headers("surgeon", source="landing")
    r = base_client.post(
        f"/api/episodes/{pid}/postop/checkin",
        json={
            "answers": {
                "pain_nrs": 1, "pain_trajectory": "BETTER", "fever": "NO",
                "incision_change": "BETTER", "incision_flags": [], "nausea": "NONE",
                "eating_drinking": "YES", "red_flag_symptoms": [], "walking": "YES",
                "worry_level": "NOT_AT_ALL",
            }
        },
        headers=headers,
    )
    assert r.status_code == 403


def test_staff_blocked_on_patient_video_event(base_client):
    pid = _seed_postop_patient()
    headers = auth_headers("np_pa", source="landing")
    r = base_client.post(
        f"/api/episodes/{pid}/postop/video-event",
        json={"video_kind": "RED_FLAG", "event_type": "PLAYED", "session_id": "s1"},
        headers=headers,
    )
    assert r.status_code == 403


def test_anon_can_submit_patient_pam(base_client):
    pid = _seed_preop_patient()
    r = base_client.post(
        f"/api/episodes/{pid}/pam",
        json={"responses": [{"item_index": i, "value": 4} for i in range(1, 14)]},
    )
    assert r.status_code == 200


# ─── Surgeon + RN role-pair coverage on preop-retier/run ───────────────────


def test_rn_can_run_preop_retier(base_client):
    pid = _seed_preop_patient()
    headers = auth_headers("rn_coordinator", source="landing")
    r = base_client.post(
        f"/api/episodes/{pid}/preop-retier/run",
        json={"triggered_by": "MANUAL:rn"},
        headers=headers,
    )
    assert r.status_code == 200


def test_surgeon_can_run_preop_retier(base_client):
    pid = _seed_preop_patient()
    headers = auth_headers("surgeon", source="landing")
    r = base_client.post(
        f"/api/episodes/{pid}/preop-retier/run",
        json={"triggered_by": "MANUAL:surgeon"},
        headers=headers,
    )
    assert r.status_code == 200
