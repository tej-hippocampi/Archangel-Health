"""
End-to-end test for the pre-op survey → pre-op re-tier wiring (Pass 2 §4).

Seeds three `survey_responses` rows (T-96 green / T-48 red / T-24 orange)
and runs `apply_preop_retier`. Asserts that the expected per-window
contributor codes appear in the persisted snapshot.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from team_store import TeamStore  # noqa: E402
from triage.preop_retier.apply import apply_preop_retier  # noqa: E402


@pytest.fixture()
def isolated_team_store(tmp_path):
    return TeamStore(db_path=str(tmp_path / "team-survey-wiring.db"))


@pytest.fixture()
def patient_store():
    return {}


def _seed_preop(patient_store, *, patient_id: str = "p1", initial_tier: str = "TIER_1") -> str:
    patient_store[patient_id] = {
        "id": patient_id,
        "phase": "pre_op",
        "current_tier": initial_tier,
        "initial_tier": initial_tier,
        "initial_tier_was_hard_escalator": False,
        "structured_data": {
            "procedure_name": "Total Knee Arthroplasty",
            "procedure_date": "2099-12-15T07:00:00",
        },
        "anchor_procedure_family": "LEJR",
        "intake_status": "STARTED",
    }
    return patient_id


def test_three_survey_windows_emit_contributors(
    isolated_team_store, patient_store
):
    """Non-critical red — falls through to the soft-delta path so we
    can observe per-window contributor codes."""
    pid = _seed_preop(patient_store)

    isolated_team_store.save_survey_response(
        patient_id=pid, survey_day=-4,  # T-96
        answers=[],
        score=85.0, tier="green", survey_type="preop",
    )
    # Non-critical red — answers payload doesn't contain any
    # red=True / red_flag=True items, so `has_critical_red_flag=False`
    # and the soft path is taken (PRD §5.3 SURVEY_T_48_RED contributor).
    isolated_team_store.save_survey_response(
        patient_id=pid, survey_day=-2,  # T-48
        answers=[{"id": "t48_anxiety_now", "answer": "8"}],
        score=40.0, tier="red", survey_type="preop",
    )
    isolated_team_store.save_survey_response(
        patient_id=pid, survey_day=-1,  # T-24
        answers=[],
        score=70.0, tier="orange", survey_type="preop",
    )

    snapshot = apply_preop_retier(
        patient_id=pid,
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="UNIT_TEST",
    )

    codes = {r["code"] for r in snapshot["reasons"]}
    # T-96 green is weight 0 but still emits a SOFT contributor for audit.
    assert "SURVEY_T_96_GREEN" in codes
    assert "SURVEY_T_48_RED" in codes
    assert "SURVEY_T_24_ORANGE" in codes


def test_t48_critical_red_flag_triggers_hard_escalator(
    isolated_team_store, patient_store
):
    """A T-48 row with a `red=True` answer is interpreted as a
    *critical* red flag and short-circuits to TIER_3 via the
    `SURVEY_RED_FLAG_CRITICAL` hard escalator (PRD §5.2)."""
    pid = _seed_preop(patient_store, patient_id="p4")
    isolated_team_store.save_survey_response(
        patient_id=pid, survey_day=-2,  # T-48
        answers=[{"id": "t48_symptoms", "red": True, "answer": "yes"}],
        score=10.0, tier="red", survey_type="preop",
    )

    snapshot = apply_preop_retier(
        patient_id=pid,
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="UNIT_TEST",
    )

    assert snapshot["computed_tier"] == "TIER_3"
    assert snapshot["tier_after"] == "TIER_3"
    codes = {r["code"] for r in snapshot["reasons"]}
    assert "SURVEY_RED_FLAG_CRITICAL" in codes


def test_missing_survey_rows_do_not_emit_window_contributors(
    isolated_team_store, patient_store
):
    pid = _seed_preop(patient_store, patient_id="p2")

    # Only T-48 row present.
    isolated_team_store.save_survey_response(
        patient_id=pid, survey_day=-2,
        answers=[],
        score=88.0, tier="green", survey_type="preop",
    )

    snapshot = apply_preop_retier(
        patient_id=pid,
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="UNIT_TEST",
    )

    codes = {r["code"] for r in snapshot["reasons"]}
    assert "SURVEY_T_48_GREEN" in codes
    assert "SURVEY_T_96_GREEN" not in codes
    assert "SURVEY_T_96_RED" not in codes
    assert "SURVEY_T_24_GREEN" not in codes


def test_postop_survey_rows_are_ignored_by_preop_reader(
    isolated_team_store, patient_store
):
    """Sanity: if a postop-typed row sits in survey_responses, the
    pre-op reader must not pick it up."""
    pid = _seed_preop(patient_store, patient_id="p3")

    # Postop-typed row at survey_day=-2 (would otherwise look like T-48)
    isolated_team_store.save_survey_response(
        patient_id=pid, survey_day=-2,
        answers=[], score=55.0, tier="red", survey_type="postop",
    )

    snapshot = apply_preop_retier(
        patient_id=pid,
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="UNIT_TEST",
    )

    codes = {r["code"] for r in snapshot["reasons"]}
    assert "SURVEY_T_48_RED" not in codes
