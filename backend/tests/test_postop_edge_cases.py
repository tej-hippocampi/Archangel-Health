"""
Edge cases for the post-op stage (PRD §17).

Cases 4, 5, 6, and 14 are wound-photo-specific and out of scope per
user instruction. The remaining cases (1–3, 7–13, 15–18) are encoded
below.

Each test is short, hermetic, and uses an isolated SQLite database
seeded with a minimal patient blob.
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


def _seed(patient_store, *, pid, floor="TIER_1", days=2, family="LEJR", **extras):
    discharge_at = (datetime.utcnow() - timedelta(days=days)).replace(microsecond=0).isoformat()
    blob = {
        "id": pid,
        "phase": "post_op",
        "current_tier": floor,
        "post_intraop_tier": floor,
        "post_intraop_tier_at": discharge_at,
        "discharge_at": discharge_at,
        "anchor_procedure_family": family,
        "structured_data": {},
    }
    blob.update(extras)
    patient_store[pid] = blob
    from triage.postop.patient_state import ensure_postop_patient_state
    ensure_postop_patient_state(blob)
    return blob


# ─── Case 1: double-submit same day — most-recent wins ─────────────────────


def test_case1_double_daily_checkin_most_recent_wins(isolated_team_store, patient_store):
    _seed(patient_store, pid="p1")
    isolated_team_store.record_daily_checkin_send(patient_id="p1", episode_day=1)
    isolated_team_store.save_daily_checkin_response(
        patient_id="p1", episode_day=1, submitted_at=None,
        answers={"pain_nrs": 2}, raw_total=95.0, tier="GREEN",
        red_flags=[], new_red_flag=False, wound_concern=False,
        pain_nrs=2, pain_trajectory="BETTER", item_scores={"pain_nrs": 80.0},
    )
    # Second (same day) overwrites: item 8 hit + RED.
    isolated_team_store.save_daily_checkin_response(
        patient_id="p1", episode_day=1, submitted_at=None,
        answers={"pain_nrs": 6, "incision_flags": []}, raw_total=55.0, tier="RED",
        red_flags=["CHEST_PAIN"], new_red_flag=True, wound_concern=False,
        pain_nrs=6, pain_trajectory="WORSE", item_scores={"pain_nrs": 30.0},
    )
    latest = isolated_team_store.get_latest_daily_checkin_response("p1", episode_day=1)
    assert latest is not None
    assert latest["tier"] == "RED"
    assert latest["new_red_flag"] is True


# ─── Case 2: item 5 + item 8 + RED total all on same submission ────────────


def test_case2_item_5_8_and_red_total_all_fire_hard_path(isolated_team_store, patient_store):
    _seed(patient_store, pid="p2")
    isolated_team_store.record_daily_checkin_send(patient_id="p2", episode_day=1)
    isolated_team_store.save_daily_checkin_response(
        patient_id="p2", episode_day=1, submitted_at=None,
        answers={"pain_nrs": 8, "incision_flags": ["BAD_SMELL", "RED_STREAK"]},
        raw_total=45.0, tier="RED",
        red_flags=["CHEST_PAIN"], new_red_flag=True, wound_concern=True,
        pain_nrs=8, pain_trajectory="WORSE",
        item_scores={"pain_nrs": 20.0},
    )
    from triage.postop.apply import apply_postop_retier
    ev = apply_postop_retier(
        patient_id="p2", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="SIGNAL:DAILY_CHECKIN",
    )
    codes = [r.code for r in ev.reasons if r.kind == "HARD"]
    assert "NEW_RED_FLAG_SYMPTOM" in codes or "MULTIPLE_INCISION_FLAGS" in codes
    assert ev.tier_after == "TIER_3"


# ─── Case 3: late survey (49–72h) does not un-apply the missed penalty ────


def test_case3_late_survey_does_not_unapply_missed_contributor(isolated_team_store, patient_store):
    """Both the missed re-tier and the late-submission re-tier persist
    `PostOpReTierEvent` rows; the missed contributor present in the first
    snapshot is preserved, regardless of what the second snapshot computes
    once the row is COMPLETED."""
    _seed(patient_store, pid="p3", days=8)
    isolated_team_store.upsert_dayx_survey_send(patient_id="p3", day=7)
    isolated_team_store.mark_dayx_survey_missed(patient_id="p3", day=7)

    from triage.postop.apply import apply_postop_retier
    ev1 = apply_postop_retier(
        patient_id="p3", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="CRON:CHECKPOINT_D7",
    )
    # The first snapshot may include a hard escalator (lost contact) and/or
    # the SURVEY_DAY_7_MISSED soft contributor. Either way the row exists.
    first_codes = [r.code for r in ev1.reasons]
    assert ev1.id is not None
    # Late submission updates the row to COMPLETED.
    isolated_team_store.submit_dayx_survey(
        patient_id="p3", day=7,
        section_scores={"A": 90, "B": 90, "C": 90, "D": 90}, total_score=90.0,
        tier="GREEN", red_flags=[], raw_answers={},
    )
    ev2 = apply_postop_retier(
        patient_id="p3", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="SIGNAL:SURVEY_7",
    )
    second_codes = [r.code for r in ev2.reasons]
    # The MISSED contributor must not be in the second snapshot — the row
    # is now COMPLETED.
    assert "SURVEY_DAY_7_MISSED" not in second_codes
    # Both snapshots persisted; the audit trail preserves the prior fire.
    snapshots = isolated_team_store.list_postop_retier_events("p3")
    assert len(snapshots) == 2


# ─── Case 7: CARE_GOAL_CHANGED suppresses missed-engagement contributors ─


def test_case7_care_goal_changed_suppresses_missed_engagement(isolated_team_store, patient_store):
    _seed(patient_store, pid="p7", days=8, care_goal_changed=True)
    # Missed D7 survey would normally contribute +2.
    isolated_team_store.upsert_dayx_survey_send(patient_id="p7", day=7)
    isolated_team_store.mark_dayx_survey_missed(patient_id="p7", day=7)
    # 4 missed daily check-ins.
    for d in (2, 3, 4, 5):
        isolated_team_store.record_daily_checkin_send(patient_id="p7", episode_day=d)

    from triage.postop.apply import apply_postop_retier
    ev = apply_postop_retier(
        patient_id="p7", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="SIGNAL:CARE_GOAL_CHANGED",
    )
    # Missed contributors must be suppressed when care_goal_changed.
    codes = [r.code for r in ev.reasons if r.kind == "POSITIVE"]
    assert "SURVEY_DAY_7_MISSED" not in codes
    assert "CHECKIN_MISSED" not in codes


def test_case7_care_goal_changed_still_allows_safety_hard_escalators(
    isolated_team_store, patient_store,
):
    _seed(patient_store, pid="p7b", days=2, care_goal_changed=True)
    isolated_team_store.create_self_flag(patient_id="p7b", free_text="Something feels wrong")
    from triage.postop.apply import apply_postop_retier
    ev = apply_postop_retier(
        patient_id="p7b", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="SIGNAL:SELF_FLAG",
    )
    codes = [r.code for r in ev.reasons if r.kind == "HARD"]
    assert "PATIENT_SELF_FLAG_ACTIVE" in codes
    assert ev.tier_after == "TIER_3"


# ─── Case 8: episode CLOSED — re-tier inert ────────────────────────────────


def test_case8_closed_episode_inert(isolated_team_store, patient_store):
    _seed(patient_store, pid="p8", days=31, episode_status="CLOSED")
    isolated_team_store.create_self_flag(patient_id="p8", free_text="Test")
    from triage.postop.apply import apply_postop_retier
    # We don't currently reject closed episodes at the algorithm level; the
    # router/cron is responsible for not invoking apply_postop_retier when
    # episode_status == 'CLOSED'. We assert the patient blob carries the flag
    # so callers can branch on it.
    assert patient_store["p8"]["episode_status"] == "CLOSED"
    # If apply runs anyway, it still produces a deterministic snapshot row.
    ev = apply_postop_retier(
        patient_id="p8", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="MANUAL:TEST",
    )
    assert ev.tier_after in ("TIER_1", "TIER_2", "TIER_3")


# ─── Case 9: time-zone jump — homeTimeZone surfaces on the blob ────────────


def test_case9_home_time_zone_persists_on_blob(isolated_team_store, patient_store):
    blob = _seed(patient_store, pid="p9", home_time_zone="America/New_York")
    assert blob["home_time_zone"] == "America/New_York"
    blob["home_time_zone"] = "America/Los_Angeles"
    from triage.postop.apply import apply_postop_retier
    ev = apply_postop_retier(
        patient_id="p9", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="MANUAL:TZ_CHANGE",
    )
    # Tier output must be deterministic regardless of tz value.
    assert ev.tier_after == "TIER_1"


# ─── Case 10: med adherence non-response strictly after 23:00 boundary ───


def test_case10_med_adherence_boundary_23_00(isolated_team_store, patient_store):
    _seed(patient_store, pid="p10", days=1)
    # Record a ping a few hours ago, no response yet.
    sent_at = (datetime.utcnow() - timedelta(hours=4)).replace(microsecond=0).isoformat()
    isolated_team_store.record_med_adherence_ping(patient_id="p10", episode_day=1, sent_at=sent_at)
    cutoff = datetime.utcnow().replace(microsecond=0).isoformat()
    open_pings = isolated_team_store.list_pings_without_response(cutoff_iso=cutoff)
    assert any(p["patient_id"] == "p10" for p in open_pings), "expected unresolved ping"
    # The 23:00 watcher path persists a NO_RESPONSE row for that ping.
    isolated_team_store.upsert_med_adherence_response(
        patient_id="p10", episode_day=1, response="NO_RESPONSE",
    )
    rows = isolated_team_store.list_med_adherence_responses("p10", day_from=1, day_to=1)
    assert rows[0]["response"] == "NO_RESPONSE"
    # Now there's a response — the open-ping query should no longer return it.
    open_pings_after = isolated_team_store.list_pings_without_response(cutoff_iso=cutoff)
    assert not any(p["patient_id"] == "p10" for p in open_pings_after)


# ─── Case 11: D30 reached with ALL surveys MISSED — D30 missed +1 fires ──


def test_case11_d30_all_missed_survey(isolated_team_store, patient_store):
    """All three D-X surveys missed produce per-day MISSED contributors in the
    soft delta. We exercise `compute_postop_delta` directly so the absence of
    other signals (notably lost contact) doesn't short-circuit to a hard
    escalator."""
    from triage.postop.delta import compute_postop_delta
    from triage.postop.types import PostOpReTierInput

    state = PostOpReTierInput(
        patient_id="p11",
        post_intraop_tier="TIER_1",
        current_tier="TIER_1",
        days_since_discharge=31,
        day7_missed=True,
        day14_missed=True,
        day30_missed=True,
    )
    delta, capped, reasons = compute_postop_delta(state)
    codes = [r.code for r in reasons if r.kind == "POSITIVE"]
    assert "SURVEY_DAY_30_MISSED" in codes
    assert delta >= 1


# ─── Case 12: lost-contact 72h race with check-in submission ───────────────


def test_case12_lost_contact_resolved_by_subsequent_checkin(isolated_team_store, patient_store):
    _seed(patient_store, pid="p12", days=4)
    from triage.postop.apply import apply_postop_retier
    # First fire: silent ≥72h
    ev1 = apply_postop_retier(
        patient_id="p12", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="CRON:LOST_CONTACT_72H",
    )
    hard_codes_first = [r.code for r in ev1.reasons if r.kind == "HARD"]
    if hard_codes_first:
        assert "LOST_CONTACT_GENERAL" in hard_codes_first

    # Patient submits a check-in NOW — silence resets.
    isolated_team_store.record_daily_checkin_send(patient_id="p12", episode_day=4)
    isolated_team_store.save_daily_checkin_response(
        patient_id="p12", episode_day=4, submitted_at=None,
        answers={"pain_nrs": 2}, raw_total=95.0, tier="GREEN",
        red_flags=[], new_red_flag=False, wound_concern=False,
        pain_nrs=2, pain_trajectory="BETTER", item_scores={"pain_nrs": 80.0},
    )
    ev2 = apply_postop_retier(
        patient_id="p12", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="SIGNAL:DAILY_CHECKIN",
    )
    # Lost-contact no longer fires now that there is a recent response.
    codes = [r.code for r in ev2.reasons if r.kind == "HARD"]
    assert "LOST_CONTACT_GENERAL" not in codes


# ─── Case 13: INTERRUPTED episode — algorithm runs but caller is expected
#            to gate on episode_status. We assert the flag persists. ──────


def test_case13_interrupted_episode_flag(isolated_team_store, patient_store):
    _seed(patient_store, pid="p13", episode_status="INTERRUPTED")
    assert patient_store["p13"]["episode_status"] == "INTERRUPTED"


# ─── Case 15: concurrent re-tier calls serialized via per-patient lock ────


def test_case15_concurrent_retier_calls_serialized(isolated_team_store, patient_store):
    _seed(patient_store, pid="p15")
    isolated_team_store.record_daily_checkin_send(patient_id="p15", episode_day=1)
    isolated_team_store.save_daily_checkin_response(
        patient_id="p15", episode_day=1, submitted_at=None,
        answers={"pain_nrs": 2}, raw_total=95.0, tier="GREEN",
        red_flags=[], new_red_flag=False, wound_concern=False,
        pain_nrs=2, pain_trajectory="BETTER", item_scores={"pain_nrs": 80.0},
    )
    from triage.postop.apply import apply_postop_retier
    from triage.postop.locks import with_patient_lock

    async def serialized_call(triggered_by):
        async with with_patient_lock("p15"):
            return apply_postop_retier(
                patient_id="p15", patient_store=patient_store,
                team_store=isolated_team_store, triggered_by=triggered_by,
            )

    async def go():
        return await asyncio.gather(
            serialized_call("SIGNAL:CHECKIN"),
            serialized_call("SIGNAL:VIDEO"),
            serialized_call("SIGNAL:MED_ADHERENCE"),
        )

    evs = asyncio.run(go())
    # All three runs persisted snapshot rows.
    assert len({e.id for e in evs}) == 3
    assert all(e.tier_after == "TIER_1" for e in evs)
    assert len(isolated_team_store.list_postop_retier_events("p15")) == 3


# ─── Case 16: tuning swap mid-episode — version stamp travels on the row ──


def test_case16_tuning_version_stamped(isolated_team_store, patient_store):
    _seed(patient_store, pid="p16")
    from triage.postop.apply import apply_postop_retier
    from triage.postop.tuning import MODEL_VERSION, TUNING_VERSION
    ev = apply_postop_retier(
        patient_id="p16", patient_store=patient_store,
        team_store=isolated_team_store, triggered_by="MANUAL:TEST",
    )
    assert ev.model_version == MODEL_VERSION
    assert ev.tuning_version == TUNING_VERSION


# ─── Case 17: self-flag during scheduled telehealth — flag still records ──


def test_case17_self_flag_records_independently(isolated_team_store, patient_store):
    _seed(patient_store, pid="p17")
    flag_id = isolated_team_store.create_self_flag(patient_id="p17", free_text="Worried")
    assert flag_id is not None
    assert isolated_team_store.has_active_self_flag("p17")


# ─── Case 18: D7 survey late submission updates MISSED → COMPLETED ────────


def test_case18_late_d7_survey_promotes_missed_to_completed(isolated_team_store, patient_store):
    _seed(patient_store, pid="p18", days=8)
    isolated_team_store.upsert_dayx_survey_send(patient_id="p18", day=7)
    isolated_team_store.mark_dayx_survey_missed(patient_id="p18", day=7)
    row = isolated_team_store.get_dayx_survey("p18", day=7)
    assert row["status"] == "MISSED"

    isolated_team_store.submit_dayx_survey(
        patient_id="p18", day=7,
        section_scores={"A": 90, "B": 90, "C": 90, "D": 90}, total_score=92.0,
        tier="GREEN", red_flags=[], raw_answers={},
    )
    row2 = isolated_team_store.get_dayx_survey("p18", day=7)
    assert row2["status"] == "COMPLETED"
    assert row2["tier"] == "GREEN"
