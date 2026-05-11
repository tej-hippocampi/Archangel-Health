"""
End-to-end lifecycle test for the post-op stage (PRD §10).

Models a single episode walking through:
    intra-op lock (snapshot of post_intraop_tier as the floor)
    → D1 daily check-in submitted GREEN
    → D7 survey window missed
    → D14 survey RED with red-flag chip

At every transition we assert that:
  1. The patient blob's `current_tier` reflects the upward-only rule.
  2. A `postop_retier_events` snapshot row was written.
  3. An `event_logs` row exists for the recompute.
  4. An `escalations` row is raised when the tier reaches TIER_3.

Wound-photo signals are intentionally absent (PRD §8 out of scope v1).
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


@pytest.fixture()
def isolated_team_store(monkeypatch):
    db_path = os.path.join(tempfile.mkdtemp(), f"team_{uuid.uuid4().hex}.db")
    monkeypatch.setenv("TEAM_DB_PATH", db_path)
    from team_store import TeamStore
    return TeamStore(db_path=db_path)


@pytest.fixture()
def patient_store():
    return {}


def _seed_postop(patient_store, *, patient_id, floor="TIER_1", days_post_discharge=1, family="LEJR"):
    discharge_at = (datetime.utcnow() - timedelta(days=days_post_discharge)).replace(microsecond=0).isoformat()
    patient_store[patient_id] = {
        "id": patient_id,
        "phase": "post_op",
        "current_tier": floor,
        "post_intraop_tier": floor,
        "post_intraop_tier_at": discharge_at,
        "discharge_at": discharge_at,
        "anchor_procedure_family": family,
        "structured_data": {},
    }
    from triage.postop.patient_state import ensure_postop_patient_state
    ensure_postop_patient_state(patient_store[patient_id])
    return patient_store[patient_id]


def _record_checkin(team_store, *, patient_id, day, tier="GREEN", red_flag=False, wound_concern=False):
    team_store.record_daily_checkin_send(patient_id=patient_id, episode_day=day)
    team_store.save_daily_checkin_response(
        patient_id=patient_id,
        episode_day=day,
        submitted_at=None,
        answers={"pain_nrs": 2, "incision_flags": [] if not wound_concern else ["BAD_SMELL"]},
        raw_total=95.0 if tier == "GREEN" else (75.0 if tier == "ORANGE" else 50.0),
        tier=tier,
        red_flags=["CHEST_PAIN"] if red_flag else [],
        new_red_flag=bool(red_flag),
        wound_concern=wound_concern,
        pain_nrs=2,
        pain_trajectory="BETTER",
        item_scores={"pain_nrs": 80.0},
    )


def test_full_lifecycle_floor_then_d1_green_then_d7_missed_then_d14_red_flag(
    isolated_team_store, patient_store, monkeypatch,
):
    pid = "lifecycle-1"
    # Discharge 14 days ago so all three checkpoints (D1, D7, D14) fall
    # naturally on real wall-clock days. Update `discharge_at` between steps
    # is unnecessary and would invalidate prior video timestamps.
    _seed_postop(patient_store, patient_id=pid, floor="TIER_1", days_post_discharge=14)
    discharge_dt = datetime.fromisoformat(patient_store[pid]["discharge_at"])
    from triage.postop.apply import apply_postop_retier

    def _retier(triggered_by, *, now):
        return apply_postop_retier(
            patient_id=pid,
            patient_store=patient_store,
            team_store=isolated_team_store,
            triggered_by=triggered_by,
            now=now,
        )

    # ── Step A: D1 — submit clean GREEN check-in ────────────────────────────
    d1_now = discharge_dt + timedelta(days=1)
    _record_checkin(isolated_team_store, patient_id=pid, day=1, tier="GREEN")
    # Engaged patient — viewed the red-flag video on day 1 so the
    # RED_FLAG_VIDEO_NOT_VIEWED_BY_D5 contributor doesn't fire later.
    isolated_team_store.record_postop_video_event(
        patient_id=pid, video_kind="RED_FLAG", event_type="PLAYED", session_id="rf1",
        occurred_at=(discharge_dt + timedelta(days=1, hours=2)).replace(microsecond=0).isoformat(),
    )
    ev_a = _retier("SIGNAL:DAILY_CHECKIN", now=d1_now)
    assert ev_a.tier_before == "TIER_1"
    assert ev_a.tier_after == "TIER_1"
    assert ev_a.changed is False
    assert patient_store[pid]["current_tier"] == "TIER_1"
    snapshots = isolated_team_store.list_postop_retier_events(pid)
    assert len(snapshots) == 1
    events = isolated_team_store.get_events(pid)
    assert any(e["event_type"] == "POSTOP_RETIER_RECOMPUTED" for e in events)

    # ── Step B: D7 — survey window missed ──────────────────────────────────
    d7_now = discharge_dt + timedelta(days=7)
    isolated_team_store.upsert_dayx_survey_send(patient_id=pid, day=7)
    isolated_team_store.mark_dayx_survey_missed(patient_id=pid, day=7)

    # Triage Suite Pass 3 §3.3 — log an `avatar_chat` event so the
    # day-7 "never used Care Companion" zero-engagement contributor
    # does not piggy-back on top of the missed-survey weight and tip
    # the patient into TIER_2. Tests stays a clean "missed window only"
    # case at TIER_1.
    isolated_team_store.log_event(
        patient_id=pid, event_type="avatar_chat", payload={"source": "chat"},
    )
    ev_b = _retier("CRON:CHECKPOINT_D7", now=d7_now)
    # SURVEY_DAY_7_MISSED weight is +2 — under 1-step threshold (+3). Stay at TIER_1.
    assert ev_b.tier_after == "TIER_1", [r.code for r in ev_b.reasons]
    snapshots = isolated_team_store.list_postop_retier_events(pid)
    assert len(snapshots) == 2

    # ── Step C: D14 — RED survey with a red-flag chip → hard escalator ─────
    d14_now = discharge_dt + timedelta(days=14)
    isolated_team_store.upsert_dayx_survey_send(patient_id=pid, day=14)
    isolated_team_store.submit_dayx_survey(
        patient_id=pid, day=14,
        section_scores={"A": 50, "B": 60, "C": 60, "D": 60},
        total_score=58.0, tier="RED", red_flags=["CHEST_PAIN"], raw_answers={},
    )
    ev_c = _retier("SIGNAL:SURVEY_14", now=d14_now)
    assert ev_c.tier_after == "TIER_3"
    assert ev_c.changed is True
    codes = [r.code for r in ev_c.reasons]
    assert "DAY_X_SURVEY_RED_AND_RED_FLAG" in codes
    assert patient_store[pid]["current_tier"] == "TIER_3"

    # ── Audit + escalation surface ─────────────────────────────────────────
    snapshots = isolated_team_store.list_postop_retier_events(pid)
    assert len(snapshots) == 3
    events = isolated_team_store.get_events(pid)
    recompute_types = {"POSTOP_RETIER_RECOMPUTED", "POSTOP_RETIER_TIER_UPDATED"}
    assert sum(1 for e in events if e["event_type"] in recompute_types) == 3
    escs = isolated_team_store.list_escalations()
    pid_escs = [e for e in escs if e["patient_id"] == pid]
    # The hard escalator that wins is the first listed reason — in this
    # scenario `NEW_RED_FLAG_SYMPTOM` (chest-pain chip) typically fires first.
    hard_codes = [r.code for r in ev_c.reasons if r.kind == "HARD"]
    assert pid_escs, "expected at least one escalation row"
    assert any(e["trigger_type"] in hard_codes for e in pid_escs), (hard_codes, [e["trigger_type"] for e in pid_escs])

    # ── Idempotent re-run from same state — no new escalation, snapshot row writes ──
    ev_d = _retier("MANUAL:NIGHTLY", now=d14_now)
    assert ev_d.tier_after == "TIER_3"
    pid_escs2 = [e for e in isolated_team_store.list_escalations() if e["patient_id"] == pid]
    assert len(pid_escs2) == len(pid_escs)


def test_floor_immutable_post_op_only_moves_upward(isolated_team_store, patient_store):
    """post_intraop_tier is the floor; the post-op stage never downgrades below it."""
    pid = "floor-immut"
    _seed_postop(patient_store, patient_id=pid, floor="TIER_2", days_post_discharge=2)
    from triage.postop.apply import apply_postop_retier

    ev = apply_postop_retier(
        patient_id=pid,
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SIGNAL:DAILY_CHECKIN",
    )
    assert ev.tier_after == "TIER_2"
    # Floor is preserved on the patient blob.
    assert patient_store[pid]["post_intraop_tier"] == "TIER_2"
    # Direct mutation attempt: manually force current_tier=TIER_1 — the next
    # apply must restore it to the floor (upward-only invariant).
    patient_store[pid]["current_tier"] = "TIER_1"
    ev2 = apply_postop_retier(
        patient_id=pid,
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="MANUAL:RECOMPUTE",
    )
    assert ev2.tier_after == "TIER_2"
    assert patient_store[pid]["current_tier"] == "TIER_2"
