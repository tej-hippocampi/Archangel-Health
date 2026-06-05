"""
End-to-end test for the intake → PAM → pre-op re-tier wiring (Pass 2 §3).

Submits a synthetic intake form including 13 PAM-style Likert responses
to `/api/pre-op/intake/submit`, then asserts:

1. A `pam_assessments` row exists for the patient.
2. A `preop_retier_events` row exists with triggered_by="SIGNAL:INTAKE_PAM".
3. An `event_logs` row of type
   `PREOP_RETIER_RECOMPUTED_NO_CHANGE` or `PREOP_RETIER_TIER_UPDATED`
   exists for the same patient.
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
from patient_session import create_patient_session  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed_preop_patient(client, *, surgery_iso: str = "2099-12-15T07:00:00") -> str:
    """Seed an in-memory pre-op patient with an initial tier already set, and
    authenticate the client as that patient (PRD-1 patient session)."""
    pid = f"intake_pam_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "pre_op",
        "specialty": "General Surgery",
        "current_tier": "TIER_1",
        "initial_tier": "TIER_1",
        "initial_tier_was_hard_escalator": False,
        "structured_data": {"procedure_name": "Total Knee Arthroplasty",
                             "procedure_date": surgery_iso},
        "anchor_procedure_family": "LEJR",
    }
    client.cookies.set("pt_session", create_patient_session(pid, None))
    return pid


def _pam_responses_high() -> dict:
    """13 strong-agreement responses → PAM HIGH (activation score >> 67)."""
    return {f"pam_{i}": 4 for i in range(1, 14)}


def test_intake_submit_persists_pam_and_triggers_retier(client):
    pid = _seed_preop_patient(client)
    form_data = {
        **_pam_responses_high(),
        "lives_alone": False,
        "housing_status": "STABLE",
    }
    r = client.post(
        "/api/pre-op/intake/submit",
        json={"patient_id": pid, "form_data": form_data},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    ts = app.state.team_store

    # 1. `pam_assessments` row exists
    pam = ts.get_latest_pam_assessment(pid)
    assert pam is not None
    assert pam["patient_id"] == pid
    assert pam["items_scored"] == 13
    assert pam["is_complete"] is True
    assert pam["level"] == "HIGH"

    # 2. `preop_retier_events` row exists with the right trigger
    events = ts.list_preop_retier_events(pid)
    assert events, "expected at least one preop_retier_events row"
    assert any(e["triggered_by"] == "SIGNAL:INTAKE_PAM" for e in events)

    # 3. event_logs has a recompute audit row
    logs = ts.get_events(pid)
    log_types = {e.get("event_type") for e in logs}
    assert "PREOP_RETIER_TIER_UPDATED" in log_types or "PREOP_RETIER_RECOMPUTED_NO_CHANGE" in log_types
    # And the per-PAM audit row
    assert "PAM_ASSESSMENT_SAVED" in log_types
    assert "intake_completed" in log_types


def test_intake_submit_partial_pam_still_triggers_retier(client):
    """Partial PAM submission (5 items) still saves a row with is_complete=False
    and triggers a re-tier; the algorithm treats it as 'not completed'."""
    pid = _seed_preop_patient(client)
    form_data = {f"pam_{i}": 3 for i in range(1, 6)}
    r = client.post(
        "/api/pre-op/intake/submit",
        json={"patient_id": pid, "form_data": form_data},
    )
    assert r.status_code == 200

    ts = app.state.team_store
    pam = ts.get_latest_pam_assessment(pid)
    assert pam is not None
    assert pam["items_scored"] == 5
    assert pam["is_complete"] is False

    events = ts.list_preop_retier_events(pid)
    assert events
    assert any(e["triggered_by"] == "SIGNAL:INTAKE_PAM" for e in events)


def test_intake_submit_without_pam_still_triggers_retier(client):
    """Even with no PAM data in the form, the re-tier still runs (partial
    intake submit) and the patient is marked intake-complete."""
    pid = _seed_preop_patient(client)
    r = client.post(
        "/api/pre-op/intake/submit",
        json={"patient_id": pid, "form_data": {"lives_alone": False}},
    )
    assert r.status_code == 200

    ts = app.state.team_store
    # No PAM rows because no PAM responses extracted
    pam = ts.get_latest_pam_assessment(pid)
    assert pam is None

    events = ts.list_preop_retier_events(pid)
    assert events, "re-tier should still run even without PAM responses"
    assert any(e["triggered_by"] == "SIGNAL:INTAKE_PAM" for e in events)


# ─── Triage Suite Pass 3 §2 — PAM-13 in Section 10 ──────────────────────────


def _section10_pam(value: str = "4") -> dict:
    """Canonical Section 10 schema shape: each pam_<i> is `{value, source}`."""
    return {
        f"pam_{i}": {"value": value, "source": "interview"}
        for i in range(1, 14)
    }


def test_intake_submit_pam_in_section10_schema_completes_pam(client):
    """Pass 3 §2.5 — the PAM-13 proxy is now surfaced inside Section 10,
    so submissions arriving via the canonical
    `section10_dayOfSurgeryReadiness.pam_<i>.value` shape must round-trip
    to a complete `pam_assessments` row and must NOT trigger the
    `PAM_NOT_COMPLETED_BY_T_72` penalty.
    """
    pid = _seed_preop_patient(client)
    form_data = {
        "section10_dayOfSurgeryReadiness": _section10_pam("4"),
    }
    r = client.post(
        "/api/pre-op/intake/submit",
        json={"patient_id": pid, "form_data": form_data},
    )
    assert r.status_code == 200, r.text

    ts = app.state.team_store
    pam = ts.get_latest_pam_assessment(pid)
    assert pam is not None, "Section 10 PAM responses must persist a pam_assessments row"
    assert pam["items_scored"] == 13
    assert pam["is_complete"] is True
    assert pam["level"] == "HIGH"

    events = ts.list_preop_retier_events(pid)
    assert events
    latest = events[0]
    reason_codes = {r.get("code") for r in latest.get("reasons", [])}
    assert "PAM_NOT_COMPLETED_BY_T_72" not in reason_codes, (
        "Completing PAM via Section 10 must not fire the not-completed penalty."
    )


def test_intake_submit_no_pam_fires_not_completed_penalty_exactly_once(client):
    """Pass 3 §2.5 — defensive: zero PAM responses must fire the penalty
    once. We seed the patient so the algorithm reaches the `T-72` window
    where the not-completed contributor evaluates."""
    from datetime import datetime, timedelta

    pid = _seed_preop_patient(client)
    # Place surgery 71 hours out so we're past the T-72 deadline for PAM
    # completion (PRD §4.2 — `PAM_NOT_COMPLETED_BY_T_72` at hours <= 72).
    soon = (datetime.utcnow() + timedelta(hours=71)).isoformat()
    app.state.patient_store[pid]["structured_data"]["procedure_date"] = soon

    r = client.post(
        "/api/pre-op/intake/submit",
        json={"patient_id": pid, "form_data": {"lives_alone": False}},
    )
    assert r.status_code == 200

    ts = app.state.team_store
    events = ts.list_preop_retier_events(pid)
    # The submit triggers exactly one re-tier with triggered_by=
    # SIGNAL:INTAKE_PAM. Ensure the penalty fires inside that single
    # run's reasons block (not zero; not duplicated).
    intake_runs = [e for e in events if e["triggered_by"] == "SIGNAL:INTAKE_PAM"]
    assert len(intake_runs) >= 1
    latest = intake_runs[0]
    codes = [r.get("code") for r in latest.get("reasons", [])]
    not_completed = [c for c in codes if c == "PAM_NOT_COMPLETED_BY_T_72"]
    assert len(not_completed) == 1, (
        f"PAM_NOT_COMPLETED_BY_T_72 should fire exactly once, got {len(not_completed)}; reasons={codes}"
    )
