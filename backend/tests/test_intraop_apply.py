"""
End-to-end tests for `apply_intraop_reassessment` (PRD §8.1).

Exercises the lock → reassess → tier-write → phase-transition →
audit-log path, plus the conservative-default and admin-reopen-relock
variants. Uses an isolated SQLite db per test so tests stay independent.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def isolated_team_store(monkeypatch):
    """Per-test SQLite db so the tables initialize cleanly each run."""
    db_path = os.path.join(tempfile.mkdtemp(), f"team_{uuid.uuid4().hex}.db")
    monkeypatch.setenv("TEAM_DB_PATH", db_path)
    from team_store import TeamStore
    store = TeamStore(db_path=db_path)
    return store


@pytest.fixture()
def patient_store():
    return {}


def _seed_patient(patient_store, *, patient_id="p1", procedure_name="Total Knee Arthroplasty"):
    patient_store[patient_id] = {
        "id": patient_id,
        "structured_data": {"procedure_name": procedure_name},
    }
    from triage.intraop.patient_state import ensure_intraop_patient_state
    ensure_intraop_patient_state(patient_store[patient_id])
    return patient_store[patient_id]


def _seed_form(team_store, patient_id, fields, *, or_ended_at="2026-05-08T09:00:00"):
    team_store.get_or_create_intraop_form(
        patient_id=patient_id, or_ended_at=or_ended_at,
    )
    team_store.update_intraop_form_fields(
        patient_id=patient_id,
        fields=fields,
        field_origins={k: {"origin": "MANUAL", "populated_at": "2026-05-08T10:00:00"} for k in fields},
        status="LOCKED",
    )


# ─── Surgeon lock — minimal, T1 → T1 (no contributors) ──────────────────────

def test_surgeon_lock_uneventful_keeps_tier(isolated_team_store, patient_store):
    from triage.intraop.apply import apply_intraop_reassessment
    p = _seed_patient(patient_store)
    _seed_form(isolated_team_store, "p1", {
        "documented_complication": False, "ebl": 100, "transfusion_total_units": 0,
        "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False, "or_duration_minutes": 90, "difficult_airway": False,
        "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
    })
    ev = apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SURGEON_LOCK:dr.smith",
    )
    assert ev.final_tier == "TIER_1"
    assert p["current_tier"] == "TIER_1"
    assert p["phase"] == "post_op"


# ─── Surgeon lock — hard upgrade fires tier write ────────────────────────────

def test_surgeon_lock_hard_upgrade_writes_t3(isolated_team_store, patient_store):
    from triage.intraop.apply import apply_intraop_reassessment
    p = _seed_patient(patient_store)
    _seed_form(isolated_team_store, "p1", {
        "documented_complication": True, "complication_types": ["VASCULAR_INJURY"],
        "ebl": 200, "transfusion_total_units": 0, "conversion": "NO",
        "sustained_hypotension": False, "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False, "or_duration_minutes": 90,
        "difficult_airway": False, "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
    })
    ev = apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SURGEON_LOCK:dr.smith",
    )
    assert ev.proposed_tier == "TIER_3"
    assert ev.final_tier == "TIER_3"
    assert p["current_tier"] == "TIER_3"
    assert p["current_tier_was_hard"] is True


# ─── Pre-op TIER_3 + uneventful → TIER_3 stays via resolve ───────────────────

def test_resolve_keeps_higher_current_tier(isolated_team_store, patient_store):
    from triage.intraop.apply import apply_intraop_reassessment
    p = _seed_patient(patient_store)
    p["current_tier"] = "TIER_3"
    p["current_tier_was_hard"] = True
    _seed_form(isolated_team_store, "p1", {
        "documented_complication": False, "ebl": 100, "transfusion_total_units": 0,
        "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False, "or_duration_minutes": 90, "difficult_airway": False,
        "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
    })
    ev = apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SURGEON_LOCK:dr.smith",
    )
    assert ev.proposed_tier == "TIER_3"
    assert ev.final_tier == "TIER_3"
    assert p["current_tier"] == "TIER_3"
    # was_hard should not flip to False on a no-op write
    assert p["current_tier_was_hard"] is True


# ─── Conservative default — no form yet, system-driven ──────────────────────

def test_conservative_default_path(isolated_team_store, patient_store):
    from triage.intraop.apply import apply_intraop_reassessment
    p = _seed_patient(patient_store)
    isolated_team_store.get_or_create_intraop_form(
        patient_id="p1", or_ended_at="2026-05-06T10:00:00",
    )
    ev = apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SYSTEM:CONSERVATIVE_DEFAULT",
        is_conservative_default=True,
    )
    assert ev.is_conservative_default is True
    assert ev.proposed_tier == "TIER_2"
    assert ev.final_tier == "TIER_2"
    assert p["current_tier"] == "TIER_2"
    assert p["current_tier_was_hard"] is False


# ─── Late lock after conservative default — resolve keeps TIER_2 ────────────

def test_late_lock_after_conservative_default_keeps_higher(isolated_team_store, patient_store):
    from triage.intraop.apply import apply_intraop_reassessment
    _seed_patient(patient_store)
    isolated_team_store.get_or_create_intraop_form(
        patient_id="p1", or_ended_at="2026-05-06T10:00:00",
    )

    # cron path elevates to TIER_2
    apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SYSTEM:CONSERVATIVE_DEFAULT",
        is_conservative_default=True,
    )
    assert patient_store["p1"]["current_tier"] == "TIER_2"

    # surgeon later locks an uneventful form → proposed TIER_1 but resolve keeps TIER_2
    isolated_team_store.update_intraop_form_fields(
        patient_id="p1",
        fields={
            "documented_complication": False, "ebl": 0, "transfusion_total_units": 0,
            "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
            "significant_arrhythmia": False, "or_duration_minutes": 60, "difficult_airway": False,
            "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
        },
        field_origins={},
        status="LOCKED",
    )
    ev2 = apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SURGEON_LOCK:dr.smith",
    )
    # PRD §5.5 Example G's stated outcome is `final == TIER_2`. The algorithm
    # echoes the passed-in current tier (TIER_2 post-conservative-default)
    # when no contributors fire, so proposed_tier is TIER_2 here as well.
    assert ev2.proposed_tier == "TIER_2"
    assert ev2.final_tier == "TIER_2"
    assert patient_store["p1"]["current_tier"] == "TIER_2"


# ─── Audit log + reassessment row are written ───────────────────────────────

def test_audit_and_reassessment_row_written(isolated_team_store, patient_store):
    from triage.intraop.apply import apply_intraop_reassessment
    _seed_patient(patient_store)
    _seed_form(isolated_team_store, "p1", {
        "documented_complication": False, "ebl": 600, "transfusion_total_units": 0,
        "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False, "or_duration_minutes": 90, "difficult_airway": False,
        "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
    })
    apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SURGEON_LOCK:dr.smith",
    )
    rows = isolated_team_store.list_intraop_reassessments("p1")
    assert len(rows) == 1
    r = rows[0]
    assert r["pre_or_current_tier"] == "TIER_1"
    assert r["proposed_tier"] == "TIER_2"
    assert r["final_tier"] == "TIER_2"
    assert r["procedure_family"] == "LEJR"
    assert r["model_version"] == "intraop-delta@1.0.0"
    assert r["triggered_by"] == "SURGEON_LOCK:dr.smith"
    assert isinstance(r["reasons"], list) and len(r["reasons"]) >= 1


# ─── Reopen → relock fires a *new* event ─────────────────────────────────────

def test_reopen_relock_appends_history(isolated_team_store, patient_store):
    from triage.intraop.apply import apply_intraop_reassessment
    _seed_patient(patient_store)
    _seed_form(isolated_team_store, "p1", {
        "documented_complication": False, "ebl": 600, "transfusion_total_units": 0,
        "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False, "or_duration_minutes": 90, "difficult_airway": False,
        "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
    })
    apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SURGEON_LOCK:dr.smith",
    )

    # Admin reopens, surgeon edits to add a hard contributor, relocks.
    isolated_team_store.reopen_intraop_form(patient_id="p1")
    isolated_team_store.update_intraop_form_fields(
        patient_id="p1",
        fields={
            "documented_complication": True, "complication_types": ["VASCULAR_INJURY"],
            "ebl": 600, "transfusion_total_units": 0, "conversion": "NO",
            "sustained_hypotension": False, "vasopressor_requirement": "NONE",
            "significant_arrhythmia": False, "or_duration_minutes": 90,
            "difficult_airway": False, "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
        },
        field_origins={},
        status="LOCKED",
    )
    apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="ADMIN_REOPEN_RELOCK:admin",
    )

    history = isolated_team_store.list_intraop_reassessments("p1")
    assert len(history) == 2
    # newest first per the ORDER BY
    assert history[0]["final_tier"] == "TIER_3"
    assert history[1]["final_tier"] == "TIER_2"


# ─── Procedure-family inference ──────────────────────────────────────────────

def test_procedure_family_inferred_from_procedure_name(isolated_team_store, patient_store):
    from triage.intraop.apply import apply_intraop_reassessment
    p = _seed_patient(patient_store, procedure_name="Three-vessel CABG")
    _seed_form(isolated_team_store, "p1", {
        "documented_complication": False, "ebl": 300, "transfusion_total_units": 0,
        "conversion": "NO", "sustained_hypotension": False, "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False, "or_duration_minutes": 200, "difficult_airway": False,
        "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
        "aortic_cross_clamp_minutes": 100,    # CABG-specific soft contributor
    })
    ev = apply_intraop_reassessment(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SURGEON_LOCK:dr.smith",
    )
    assert ev.procedure_family == "CABG"
    assert any(r.code == "CABG_CROSS_CLAMP_OVER_90" for r in ev.reasons)
    assert p["anchor_procedure_family"] == "CABG"
