"""
Tests for the conservative-default overdue watcher (PRD §7.4).
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.intraop.overdue_watcher import run_overdue_pass  # noqa: E402


@pytest.fixture()
def store(monkeypatch):
    db_path = os.path.join(tempfile.mkdtemp(), f"team_{uuid.uuid4().hex}.db")
    monkeypatch.setenv("TEAM_DB_PATH", db_path)
    from team_store import TeamStore
    return TeamStore(db_path=db_path)


@pytest.fixture()
def patient_store():
    return {}


def _seed(patient_store, store, *, patient_id, hours_since_or_end, status="NEW"):
    """Create a patient + form with `or_ended_at = now - hours_since_or_end`."""
    patient_store[patient_id] = {
        "id": patient_id,
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
    }
    from triage.intraop.patient_state import ensure_intraop_patient_state
    ensure_intraop_patient_state(patient_store[patient_id])
    or_ended = (datetime.utcnow() - timedelta(hours=hours_since_or_end)).replace(microsecond=0).isoformat()
    store.get_or_create_intraop_form(patient_id=patient_id, or_ended_at=or_ended)
    if status != "NEW":
        store.update_intraop_form_fields(
            patient_id=patient_id,
            fields={}, field_origins={},
            status=status,
        )


def test_no_overdue_under_24h(store, patient_store):
    _seed(patient_store, store, patient_id="p1", hours_since_or_end=12)
    applied = run_overdue_pass(patient_store=patient_store, team_store=store)
    assert applied == []


def test_overdue_form_applies_conservative_default(store, patient_store):
    _seed(patient_store, store, patient_id="p1", hours_since_or_end=30)
    applied = run_overdue_pass(patient_store=patient_store, team_store=store)
    assert len(applied) == 1
    assert applied[0]["patient_id"] == "p1"
    assert applied[0]["final_tier"] == "TIER_2"
    assert patient_store["p1"]["current_tier"] == "TIER_2"
    assert patient_store["p1"]["phase"] == "post_op"


def test_idempotent_under_repeated_runs(store, patient_store):
    _seed(patient_store, store, patient_id="p1", hours_since_or_end=30)
    run_overdue_pass(patient_store=patient_store, team_store=store)
    again = run_overdue_pass(patient_store=patient_store, team_store=store)
    assert again == []  # CAS prevents the second cycle


def test_locked_form_skipped(store, patient_store):
    _seed(patient_store, store, patient_id="p1", hours_since_or_end=30, status="LOCKED")
    applied = run_overdue_pass(patient_store=patient_store, team_store=store)
    assert applied == []


def test_already_flagged_form_skipped(store, patient_store):
    _seed(patient_store, store, patient_id="p1", hours_since_or_end=30)
    store.mark_intraop_conservative_default_applied(patient_id="p1")
    applied = run_overdue_pass(patient_store=patient_store, team_store=store)
    assert applied == []


def test_late_lock_after_cron_resolves_to_higher_tier(store, patient_store):
    """PRD edge case 6: cron applied TIER_2; surgeon late-locks an
    uneventful form; final must remain TIER_2."""
    _seed(patient_store, store, patient_id="p1", hours_since_or_end=30)
    run_overdue_pass(patient_store=patient_store, team_store=store)
    assert patient_store["p1"]["current_tier"] == "TIER_2"

    # Surgeon late-locks (uneventful form).
    store.update_intraop_form_fields(
        patient_id="p1",
        fields={
            "documented_complication": False, "ebl": 100, "transfusion_total_units": 0,
            "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
            "significant_arrhythmia": False, "or_duration_minutes": 60,
            "difficult_airway": False, "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
        },
        field_origins={},
        status="LOCKED",
    )
    from triage.intraop.apply import apply_intraop_reassessment
    ev = apply_intraop_reassessment(
        patient_id="p1", patient_store=patient_store, team_store=store,
        triggered_by="SURGEON_LOCK:dr.smith",
    )
    assert ev.final_tier == "TIER_2"   # cron's TIER_2 sticks


def test_multiple_overdue_forms_each_handled(store, patient_store):
    for pid, hrs in [("p1", 25), ("p2", 30), ("p3", 26)]:
        _seed(patient_store, store, patient_id=pid, hours_since_or_end=hrs)
    applied = run_overdue_pass(patient_store=patient_store, team_store=store)
    assert sorted(a["patient_id"] for a in applied) == ["p1", "p2", "p3"]
    assert all(patient_store[a["patient_id"]]["current_tier"] == "TIER_2" for a in applied)


def test_threshold_can_be_overridden(store, patient_store):
    _seed(patient_store, store, patient_id="p1", hours_since_or_end=12)
    applied = run_overdue_pass(
        patient_store=patient_store, team_store=store, threshold_hours=6,
    )
    assert len(applied) == 1
