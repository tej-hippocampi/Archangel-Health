"""
End-to-end tests for `apply_postop_retier` (PRD §10).

Each test exercises the gather-state → re-tier → persist → audit →
escalation → denormalize-on-blob path. Uses an isolated SQLite db per
test so the post-op tables initialize cleanly.

Wound-photo-related fixtures are intentionally absent (PRD §8 out of scope v1).
"""

from __future__ import annotations

import asyncio
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


def _seed_patient(
    patient_store,
    *,
    patient_id="p1",
    floor="TIER_1",
    discharge_days_ago=1,
    procedure_family="LEJR",
):
    discharge_at = (datetime.utcnow() - timedelta(days=discharge_days_ago)).replace(microsecond=0).isoformat()
    patient_store[patient_id] = {
        "id": patient_id,
        "phase": "post_op",
        "current_tier": floor,
        "post_intraop_tier": floor,
        "post_intraop_tier_at": discharge_at,
        "discharge_at": discharge_at,
        "anchor_procedure_family": procedure_family,
        "structured_data": {},
    }
    from triage.postop.patient_state import ensure_postop_patient_state
    ensure_postop_patient_state(patient_store[patient_id])
    return patient_store[patient_id]


def _record_checkin(team_store, *, patient_id, episode_day, tier="GREEN", red_flag=False, wound_concern=False):
    team_store.record_daily_checkin_send(patient_id=patient_id, episode_day=episode_day)
    team_store.save_daily_checkin_response(
        patient_id=patient_id,
        episode_day=episode_day,
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


# ─── Clean state ────────────────────────────────────────────────────────────


def test_clean_state_no_change(isolated_team_store, patient_store):
    _seed_patient(patient_store, patient_id="p1", floor="TIER_1")
    from triage.postop.apply import apply_postop_retier

    ev = apply_postop_retier(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="MANUAL:user-1",
    )
    assert ev.tier_before == "TIER_1"
    assert ev.tier_after == "TIER_1"
    assert ev.changed is False
    assert ev.computed_delta == 0
    # event_logs row written
    events = isolated_team_store.get_events("p1")
    assert any(e["event_type"] == "POSTOP_RETIER_RECOMPUTED" for e in events)
    # patient blob denormalized
    assert patient_store["p1"]["postop_retier_last_tier"] == "TIER_1"
    assert patient_store["p1"]["postop_retier_last_run_at"] is not None


# ─── Hard escalator: self-flag ──────────────────────────────────────────────


def test_active_self_flag_forces_tier_3(isolated_team_store, patient_store):
    _seed_patient(patient_store, patient_id="p1", floor="TIER_1")
    isolated_team_store.create_self_flag(patient_id="p1", free_text="Something feels off")
    from triage.postop.apply import apply_postop_retier

    ev = apply_postop_retier(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SIGNAL:SELF_FLAG",
    )
    assert ev.tier_after == "TIER_3"
    assert ev.changed is True
    assert any(r.code == "PATIENT_SELF_FLAG_ACTIVE" for r in ev.reasons)
    # Escalation raised
    escs = isolated_team_store.list_escalations()
    assert any(e["trigger_type"] == "PATIENT_SELF_FLAG_ACTIVE" for e in escs)


# ─── Hard escalator: red-flag symptom on check-in ───────────────────────────


def test_red_flag_chip_on_checkin_forces_tier_3(isolated_team_store, patient_store):
    _seed_patient(patient_store, patient_id="p1", floor="TIER_1")
    _record_checkin(isolated_team_store, patient_id="p1", episode_day=3, tier="RED", red_flag=True)
    from triage.postop.apply import apply_postop_retier

    ev = apply_postop_retier(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="SIGNAL:DAILY_CHECKIN",
    )
    assert ev.tier_after == "TIER_3"
    assert ev.changed is True
    codes = [r.code for r in ev.reasons]
    assert "NEW_RED_FLAG_SYMPTOM" in codes


# ─── Soft delta path: D7 missed + D14 orange + 4 missed check-ins ───────────


def test_missed_engagement_pushes_to_t3(isolated_team_store, patient_store):
    """PRD §10.5 Example B."""
    _seed_patient(patient_store, patient_id="p1", floor="TIER_1", discharge_days_ago=14)
    # D7 missed
    isolated_team_store.upsert_dayx_survey_send(patient_id="p1", day=7)
    isolated_team_store.mark_dayx_survey_missed(patient_id="p1", day=7)
    # D14 ORANGE submission
    isolated_team_store.upsert_dayx_survey_send(patient_id="p1", day=14)
    isolated_team_store.submit_dayx_survey(
        patient_id="p1", day=14,
        section_scores={"A": 75, "B": 75, "C": 75, "D": 75},
        total_score=75.0, tier="ORANGE", red_flags=[], raw_answers={},
    )
    # 4 sends without responses → 4 missed in rolling 7-day window.
    for d in (8, 9, 10, 11):
        isolated_team_store.record_daily_checkin_send(patient_id="p1", episode_day=d)
    # Latest submitted at D14 to anchor the window.
    _record_checkin(isolated_team_store, patient_id="p1", episode_day=14, tier="GREEN")

    from triage.postop.apply import apply_postop_retier
    ev = apply_postop_retier(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="CHECKPOINT:DAY_14",
    )
    assert ev.computed_delta >= 6
    assert ev.tier_after == "TIER_3"


# ─── Repeat call is idempotent ──────────────────────────────────────────────


def test_idempotent_recompute(isolated_team_store, patient_store):
    _seed_patient(patient_store, patient_id="p1", floor="TIER_2")
    from triage.postop.apply import apply_postop_retier

    a = apply_postop_retier(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="MANUAL:t1",
    )
    b = apply_postop_retier(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="MANUAL:t2",
    )
    assert a.tier_after == b.tier_after
    assert a.computed_delta == b.computed_delta
    # Two distinct rows persisted.
    rows = isolated_team_store.list_postop_retier_events("p1")
    assert len(rows) == 2


# ─── Floor enforcement ─────────────────────────────────────────────────────


def test_apply_never_lowers_below_floor(isolated_team_store, patient_store):
    """If the algorithm proposes TIER_1 but the floor is TIER_2 (e.g.
    intra-op upgraded the floor), the apply layer keeps TIER_2."""
    _seed_patient(patient_store, patient_id="p1", floor="TIER_2")
    from triage.postop.apply import apply_postop_retier

    ev = apply_postop_retier(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="MANUAL:user-1",
    )
    assert ev.tier_after == "TIER_2"
    assert ev.computed_tier == "TIER_2"


# ─── Lost-contact path ─────────────────────────────────────────────────────


def test_lost_contact_general_72h_with_no_signals(isolated_team_store, patient_store):
    """No signals at all + episode is 5 days old → 72h general silence;
    hard escalator fires."""
    _seed_patient(patient_store, patient_id="p1", floor="TIER_1", discharge_days_ago=5)
    from triage.postop.apply import apply_postop_retier

    ev = apply_postop_retier(
        patient_id="p1",
        patient_store=patient_store,
        team_store=isolated_team_store,
        triggered_by="CRON:LOST_CONTACT",
    )
    codes = [r.code for r in ev.reasons]
    assert "LOST_CONTACT_GENERAL" in codes
    assert ev.tier_after == "TIER_3"


# ─── Concurrent recompute serialization ────────────────────────────────────


def test_concurrent_recomputes_serialize(isolated_team_store, patient_store):
    """Two coroutines firing apply_postop_retier under
    `with_patient_lock` produce two deterministic rows (no SQLite write
    contention; both rows persisted in order)."""
    _seed_patient(patient_store, patient_id="p1", floor="TIER_1")

    async def runner():
        from triage.postop.apply import apply_postop_retier
        from triage.postop.locks import reset_locks_for_test, with_patient_lock

        reset_locks_for_test()

        async def one(triggered_by):
            async with with_patient_lock("p1"):
                return apply_postop_retier(
                    patient_id="p1",
                    patient_store=patient_store,
                    team_store=isolated_team_store,
                    triggered_by=triggered_by,
                )

        return await asyncio.gather(one("a"), one("b"))

    results = asyncio.run(runner())
    assert len(results) == 2
    rows = isolated_team_store.list_postop_retier_events("p1")
    assert len(rows) == 2
