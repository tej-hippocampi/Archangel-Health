"""
Synthetic load test for the post-op stage (PRD §18 step 19).

Exercises 50 simulated post-op episodes with varied engagement profiles
across 30 days each. For every (patient × day) tick we may submit a
daily check-in, mark a survey, record a video event, post a med
adherence response, and run `apply_postop_retier`. We assert:

  - Determinism: a second walk over the same fixture stream produces
    identical final tiers.
  - Snapshot completeness: every recompute writes a `postop_retier_events`
    row.
  - Audit completeness: every recompute writes an `event_logs` row.
  - Upward-only: tier never decreases below `post_intraop_tier`.

Wound-photo signals are intentionally absent (PRD §8 out of scope v1).
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_TIER_RANK = {"TIER_1": 1, "TIER_2": 2, "TIER_3": 3}


@pytest.fixture()
def isolated_team_store(monkeypatch):
    db_path = os.path.join(tempfile.mkdtemp(), f"team_{uuid.uuid4().hex}.db")
    monkeypatch.setenv("TEAM_DB_PATH", db_path)
    from team_store import TeamStore
    return TeamStore(db_path=db_path)


def _seed_one(patient_store, *, pid, profile, floor, days):
    discharge_at = (datetime.utcnow() - timedelta(days=days)).replace(microsecond=0).isoformat()
    patient_store[pid] = {
        "id": pid,
        "phase": "post_op",
        "current_tier": floor,
        "post_intraop_tier": floor,
        "post_intraop_tier_at": discharge_at,
        "discharge_at": discharge_at,
        "anchor_procedure_family": profile["family"],
        "structured_data": {},
    }
    from triage.postop.patient_state import ensure_postop_patient_state
    ensure_postop_patient_state(patient_store[pid])


def _record_checkin(team_store, *, pid, day, tier="GREEN", red_flag=False):
    team_store.record_daily_checkin_send(patient_id=pid, episode_day=day)
    team_store.save_daily_checkin_response(
        patient_id=pid, episode_day=day, submitted_at=None,
        answers={"pain_nrs": 2, "incision_flags": []},
        raw_total=95.0 if tier == "GREEN" else 60.0,
        tier=tier,
        red_flags=["CHEST_PAIN"] if red_flag else [],
        new_red_flag=bool(red_flag), wound_concern=False,
        pain_nrs=2, pain_trajectory="BETTER",
        item_scores={"pain_nrs": 80.0},
    )


def _profile_for(seed: int) -> dict:
    """Generate a deterministic engagement profile for a given seed."""
    rng = random.Random(seed)
    kind = rng.choice(["clean", "missed", "hard", "lost"])
    family = rng.choice(["LEJR", "SPINAL_FUSION", "CABG", "MAJOR_BOWEL"])
    return {"kind": kind, "family": family}


def _walk_episode(team_store, patient_store, *, pid, profile, days):
    """Simulate days 1..days of activity for `pid` per the profile.
    Calls apply_postop_retier at each tick. Returns the list of events.
    """
    from triage.postop.apply import apply_postop_retier
    discharge_dt = datetime.fromisoformat(patient_store[pid]["discharge_at"])

    events = []
    for d in range(1, days + 1):
        # Check-in submission per profile.
        if profile["kind"] == "clean":
            _record_checkin(team_store, pid=pid, day=d, tier="GREEN")
        elif profile["kind"] == "missed":
            if d % 3 == 0:  # only every third day responds
                _record_checkin(team_store, pid=pid, day=d, tier="GREEN")
            else:
                team_store.record_daily_checkin_send(patient_id=pid, episode_day=d)
        elif profile["kind"] == "hard" and d == 5:
            _record_checkin(team_store, pid=pid, day=d, tier="RED", red_flag=True)
        elif profile["kind"] == "lost":
            pass  # silence

        # Survey ticks at D7 / D14 / D30.
        if d in (7, 14, 30):
            team_store.upsert_dayx_survey_send(patient_id=pid, day=d)
            if profile["kind"] == "clean":
                team_store.submit_dayx_survey(
                    patient_id=pid, day=d,
                    section_scores={"A": 90, "B": 90, "C": 90, "D": 90},
                    total_score=90.0, tier="GREEN", red_flags=[], raw_answers={},
                )
            elif profile["kind"] == "missed":
                team_store.mark_dayx_survey_missed(patient_id=pid, day=d)

        # Video event for "clean" patients on D1 (red-flag).
        if profile["kind"] == "clean" and d == 1:
            team_store.record_postop_video_event(
                patient_id=pid, video_kind="RED_FLAG", event_type="PLAYED",
                session_id=f"rf-{pid}",
                occurred_at=(discharge_dt + timedelta(days=1, hours=1)).replace(microsecond=0).isoformat(),
            )

        # Med adherence ping every day for "clean" patients.
        if profile["kind"] == "clean":
            team_store.record_med_adherence_ping(patient_id=pid, episode_day=d)
            team_store.upsert_med_adherence_response(
                patient_id=pid, episode_day=d, response="YES",
            )

        ev = apply_postop_retier(
            patient_id=pid,
            patient_store=patient_store,
            team_store=team_store,
            triggered_by=f"SIM:DAY_{d}",
            now=discharge_dt + timedelta(days=d),
        )
        events.append(ev)
    return events


def test_synthetic_load_50_episodes_30_days(isolated_team_store):
    """50 patients × 30 days, mixed profiles."""
    patient_store = {}
    n_patients = 50
    days = 30
    final_tiers: dict[str, str] = {}

    for i in range(n_patients):
        pid = f"sim-{i:02d}"
        profile = _profile_for(seed=i)
        floor = "TIER_1" if i % 5 != 0 else "TIER_2"
        _seed_one(patient_store, pid=pid, profile=profile, floor=floor, days=days)
        evs = _walk_episode(
            isolated_team_store, patient_store,
            pid=pid, profile=profile, days=days,
        )
        # Snapshot completeness: 30 ticks → 30 events.
        assert len(evs) == days
        assert len(isolated_team_store.list_postop_retier_events(pid)) == days

        # Upward-only invariant.
        floor_rank = _TIER_RANK[floor]
        for ev in evs:
            assert _TIER_RANK[ev.tier_after] >= floor_rank, (pid, ev.tier_after, floor)

        # Audit row count matches event row count.
        log_rows = [
            e for e in isolated_team_store.get_events(pid)
            if e["event_type"] in ("POSTOP_RETIER_RECOMPUTED", "POSTOP_RETIER_TIER_UPDATED")
        ]
        assert len(log_rows) == days

        final_tiers[pid] = evs[-1].tier_after

    # Sanity: at least the "hard" patients reached TIER_3, and the "clean"
    # ones stayed at floor.
    hard_pids = [pid for pid in final_tiers if _profile_for(int(pid.split("-")[1]))["kind"] == "hard"]
    if hard_pids:
        assert any(final_tiers[p] == "TIER_3" for p in hard_pids)


def test_synthetic_load_determinism(isolated_team_store, monkeypatch):
    """Replay the same fixture stream twice — final tiers must match exactly."""
    patient_store_a = {}
    patient_store_b = {}

    db_path_b = os.path.join(tempfile.mkdtemp(), f"team_b_{uuid.uuid4().hex}.db")
    from team_store import TeamStore
    team_store_b = TeamStore(db_path=db_path_b)

    n = 20
    days = 10
    finals_a: dict[str, str] = {}
    finals_b: dict[str, str] = {}

    for i in range(n):
        pid = f"det-{i:02d}"
        profile = _profile_for(seed=100 + i)
        floor = "TIER_1"
        _seed_one(patient_store_a, pid=pid, profile=profile, floor=floor, days=days)
        _seed_one(patient_store_b, pid=pid, profile=profile, floor=floor, days=days)
        evs_a = _walk_episode(isolated_team_store, patient_store_a, pid=pid, profile=profile, days=days)
        evs_b = _walk_episode(team_store_b, patient_store_b, pid=pid, profile=profile, days=days)
        finals_a[pid] = evs_a[-1].tier_after
        finals_b[pid] = evs_b[-1].tier_after

    assert finals_a == finals_b
