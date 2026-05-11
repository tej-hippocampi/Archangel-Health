"""
End-to-end tests for the Post-Op cron passes (PRD §10.6).

Covers:
  - daily check-in send / D-X send / med-adherence ping send (idempotent)
  - daily check-in missed watcher (36h cutoff + streak bump)
  - D-X survey missed watcher (48h cutoff)
  - med-adherence non-response close pass (23:00)
  - lost-contact watcher async wrapper
  - nightly re-tier batch
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


def _seed_post_op(patient_store, *, patient_id="p1", days_post=2):
    discharge_at = (datetime.utcnow() - timedelta(days=days_post)).replace(microsecond=0).isoformat()
    patient_store[patient_id] = {
        "id": patient_id,
        "phase": "post_op",
        "current_tier": "TIER_1",
        "post_intraop_tier": "TIER_1",
        "discharge_at": discharge_at,
        "anchor_procedure_family": "LEJR",
        "structured_data": {},
    }
    from triage.postop.patient_state import ensure_postop_patient_state
    ensure_postop_patient_state(patient_store[patient_id])
    return patient_store[patient_id]


# ─── Send passes ────────────────────────────────────────────────────────────


def test_daily_checkin_send_pass_creates_one_per_day(isolated_team_store, patient_store):
    _seed_post_op(patient_store, patient_id="p1", days_post=1)
    from triage.postop.cron import run_daily_checkin_send_pass

    sent_first = run_daily_checkin_send_pass(patient_store=patient_store, team_store=isolated_team_store)
    sent_again = run_daily_checkin_send_pass(patient_store=patient_store, team_store=isolated_team_store)
    assert sent_first == 1
    assert sent_again == 0  # idempotent within the same day
    assert isolated_team_store.has_daily_checkin_send("p1", 2)


def test_dayx_send_pass_only_on_d7_d14_d30(isolated_team_store, patient_store):
    _seed_post_op(patient_store, patient_id="p1", days_post=6)  # episode-day 7
    from triage.postop.cron import run_dayx_survey_send_pass

    sent = run_dayx_survey_send_pass(patient_store=patient_store, team_store=isolated_team_store)
    assert sent == 1
    survey = isolated_team_store.get_dayx_survey("p1", 7)
    assert survey is not None
    assert survey["status"] == "PENDING"

    # Repeat send is a no-op.
    again = run_dayx_survey_send_pass(patient_store=patient_store, team_store=isolated_team_store)
    assert again == 0


def test_med_adherence_send_pass(isolated_team_store, patient_store):
    _seed_post_op(patient_store, patient_id="p1", days_post=1)
    from triage.postop.cron import run_med_adherence_send_pass

    sent = run_med_adherence_send_pass(patient_store=patient_store, team_store=isolated_team_store)
    assert sent == 1
    again = run_med_adherence_send_pass(patient_store=patient_store, team_store=isolated_team_store)
    assert again == 0


# ─── Watchers ──────────────────────────────────────────────────────────────


def test_checkin_missed_watcher_marks_and_bumps_streak(isolated_team_store, patient_store):
    _seed_post_op(patient_store, patient_id="p1", days_post=1)
    # Insert a send 40h ago (past the 36h window).
    sent_at = (datetime.utcnow() - timedelta(hours=40)).replace(microsecond=0).isoformat()
    isolated_team_store.record_daily_checkin_send(
        patient_id="p1", episode_day=2, sent_at=sent_at,
    )

    from triage.postop.cron import run_checkin_missed_watcher
    marked = run_checkin_missed_watcher(
        patient_store=patient_store, team_store=isolated_team_store,
    )
    assert marked == 1
    assert patient_store["p1"]["daily_checkin_missed_streak"] == 1


def test_checkin_missed_watcher_skips_responses(isolated_team_store, patient_store):
    _seed_post_op(patient_store, patient_id="p1", days_post=1)
    sent_at = (datetime.utcnow() - timedelta(hours=40)).replace(microsecond=0).isoformat()
    isolated_team_store.record_daily_checkin_send(patient_id="p1", episode_day=2, sent_at=sent_at)
    isolated_team_store.save_daily_checkin_response(
        patient_id="p1", episode_day=2, submitted_at=None,
        answers={}, raw_total=95.0, tier="GREEN",
        red_flags=[], new_red_flag=False, wound_concern=False,
        pain_nrs=2, pain_trajectory="BETTER", item_scores={},
    )

    from triage.postop.cron import run_checkin_missed_watcher
    marked = run_checkin_missed_watcher(
        patient_store=patient_store, team_store=isolated_team_store,
    )
    assert marked == 0


def test_dayx_missed_watcher_marks_pending_past_48h(isolated_team_store, patient_store):
    _seed_post_op(patient_store, patient_id="p1", days_post=10)
    sent_at = (datetime.utcnow() - timedelta(hours=50)).replace(microsecond=0).isoformat()
    isolated_team_store.upsert_dayx_survey_send(patient_id="p1", day=7, sent_at=sent_at)

    from triage.postop.cron import run_dayx_missed_watcher
    marked = run_dayx_missed_watcher(team_store=isolated_team_store)
    assert marked == 1
    survey = isolated_team_store.get_dayx_survey("p1", 7)
    assert survey["status"] == "MISSED"


def test_med_adherence_close_pass_marks_non_response(isolated_team_store, patient_store):
    _seed_post_op(patient_store, patient_id="p1", days_post=1)
    sent_at = (datetime.utcnow() - timedelta(hours=5)).replace(microsecond=0).isoformat()
    isolated_team_store.record_med_adherence_ping(
        patient_id="p1", episode_day=2, sent_at=sent_at,
    )
    from triage.postop.cron import run_med_adherence_close_pass
    closed = run_med_adherence_close_pass(team_store=isolated_team_store)
    assert closed == 1
    rows = isolated_team_store.list_med_adherence_responses("p1", day_from=2, day_to=2)
    assert rows[0]["response"] == "MISSED_NON_RESPONSE"


# ─── Async wrappers ────────────────────────────────────────────────────────


def test_lost_contact_watcher_async_fires_on_silent_t3(isolated_team_store, patient_store):
    """Tier-3 patient discharged 30h ago with no signals → async watcher
    flips them to TIER_3 (already T3) and writes a hard event."""
    _seed_post_op(patient_store, patient_id="p1", days_post=2)
    patient_store["p1"]["current_tier"] = "TIER_3"
    patient_store["p1"]["post_intraop_tier"] = "TIER_3"
    # Override discharge to 30h ago.
    patient_store["p1"]["discharge_at"] = (datetime.utcnow() - timedelta(hours=30)).replace(microsecond=0).isoformat()

    async def runner():
        from triage.postop.cron import run_lost_contact_watcher_async
        from triage.postop.locks import reset_locks_for_test
        reset_locks_for_test()
        return await run_lost_contact_watcher_async(
            patient_store=patient_store, team_store=isolated_team_store,
        )

    moved = asyncio.run(runner())
    # T3 already; tier didn't change, but a re-tier event was written.
    rows = isolated_team_store.list_postop_retier_events("p1")
    assert len(rows) >= 1
    codes = [r["code"] for r in rows[0]["reasons"]]
    assert "LOST_CONTACT_TIER3" in codes
    assert isinstance(moved, int)


def test_nightly_retier_batch_writes_event_per_patient(isolated_team_store, patient_store):
    _seed_post_op(patient_store, patient_id="p1", days_post=1)
    _seed_post_op(patient_store, patient_id="p2", days_post=1)

    async def runner():
        from triage.postop.cron import run_nightly_retier_batch_async
        from triage.postop.locks import reset_locks_for_test
        reset_locks_for_test()
        return await run_nightly_retier_batch_async(
            patient_store=patient_store, team_store=isolated_team_store,
        )

    asyncio.run(runner())
    assert len(isolated_team_store.list_postop_retier_events("p1")) == 1
    assert len(isolated_team_store.list_postop_retier_events("p2")) == 1


def test_send_passes_skip_pre_op_patients(isolated_team_store, patient_store):
    """Pre-op patients are not in scope for any post-op send."""
    patient_store["p_preop"] = {
        "id": "p_preop", "phase": "pre_op", "current_tier": "TIER_1",
        "anchor_procedure_family": "LEJR", "structured_data": {},
    }
    from triage.postop.cron import (
        run_daily_checkin_send_pass,
        run_dayx_survey_send_pass,
        run_med_adherence_send_pass,
    )
    assert run_daily_checkin_send_pass(patient_store=patient_store, team_store=isolated_team_store) == 0
    assert run_dayx_survey_send_pass(patient_store=patient_store, team_store=isolated_team_store) == 0
    assert run_med_adherence_send_pass(patient_store=patient_store, team_store=isolated_team_store) == 0
