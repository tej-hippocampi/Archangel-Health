"""
End-to-end tests for the Post-Op Scoring HTTP surface (PRD §14).

Uses FastAPI's `TestClient` against `main:app`. Each test uses a unique
patient_id so rows don't collide with the shared team_store.

Wound-photo endpoints are intentionally absent (PRD §14.4 out of scope v1).
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402


@pytest.fixture()
def client():
    """Anonymous TestClient. Pass-4: patient-submitted endpoints
    (`checkin`, `survey`, `med-adherence`, `video-event`, `self-flag`)
    accept anonymous; clinical endpoints reject 401 here — those tests
    use the `staff_client` / `rn_client` fixtures below."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def staff_client():
    """Surgeon-authed TestClient for clinical reads/writes (`get_postop`,
    `discharge`, `care-goal-changed`, `postop-retier/run`, `events`)."""
    with TestClient(app, headers=auth_headers("surgeon", source="landing")) as c:
        yield c


@pytest.fixture()
def rn_client():
    """RN-coordinator-authed TestClient for alert resolve (`self-flag/resolve`)."""
    with TestClient(app, headers=auth_headers("rn_coordinator", source="landing")) as c:
        yield c


def _seed_patient(*, floor: str = "TIER_1", days_post_discharge: int = 3) -> str:
    pid = f"postop_{uuid.uuid4().hex[:8]}"
    discharge_at = (datetime.utcnow() - timedelta(days=days_post_discharge)).replace(microsecond=0).isoformat()
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "post_op",
        "current_tier": floor,
        "post_intraop_tier": floor,
        "discharge_at": discharge_at,
        "anchor_procedure_family": "LEJR",
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
    }
    from triage.postop.patient_state import ensure_postop_patient_state
    ensure_postop_patient_state(app.state.patient_store[pid])
    return pid


def _checkin_payload(**overrides):
    base = dict(
        pain_nrs=2, pain_trajectory="BETTER", fever="NO",
        incision_change="BETTER", incision_flags=[], nausea="NONE",
        eating_drinking="YES", red_flag_symptoms=[], walking="YES",
        worry_level="NOT_AT_ALL",
    )
    base.update(overrides)
    return {"answers": base}


# ─── GET postop view ────────────────────────────────────────────────────────


def test_get_postop_view(staff_client):
    pid = _seed_patient()
    r = staff_client.get(f"/api/episodes/{pid}/postop")
    assert r.status_code == 200
    body = r.json()
    assert body["postIntraOpTier"] == "TIER_1"
    assert body["dischargeAt"] is not None


def test_get_postop_unknown_patient_returns_404(staff_client):
    r = staff_client.get(f"/api/episodes/does-not-exist-{uuid.uuid4().hex}/postop")
    assert r.status_code == 404


# ─── Discharge ──────────────────────────────────────────────────────────────


def test_post_discharge_records_timestamp(staff_client):
    pid = _seed_patient(days_post_discharge=0)
    ts = datetime.utcnow().replace(microsecond=0).isoformat()
    r = staff_client.post(f"/api/episodes/{pid}/postop/discharge", json={"discharge_at": ts})
    assert r.status_code == 200
    assert r.json()["dischargeAt"] == ts


# ─── Daily check-in submit ─────────────────────────────────────────────────


def test_daily_checkin_clean_returns_green(client):
    pid = _seed_patient()
    r = client.post(f"/api/episodes/{pid}/postop/checkin", json=_checkin_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "GREEN"
    assert body["newRedFlagSymptom"] is False
    assert body["retier"]["tier_after"] == "TIER_1"


def test_daily_checkin_red_flag_chip_forces_tier_3(client):
    pid = _seed_patient()
    r = client.post(
        f"/api/episodes/{pid}/postop/checkin",
        json=_checkin_payload(red_flag_symptoms=["CHEST_PAIN"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "RED"
    assert body["newRedFlagSymptom"] is True
    # Synchronous re-tier moves the patient to TIER_3.
    assert body["retier"]["tier_after"] == "TIER_3"


def test_daily_checkin_invalid_payload(client):
    pid = _seed_patient()
    r = client.post(f"/api/episodes/{pid}/postop/checkin", json={"answers": {"pain_nrs": 99}})
    assert r.status_code == 422


# ─── Day-X survey submit ───────────────────────────────────────────────────


def _survey_answers(*, with_red_flag: bool = False):
    section_a = {
        "pain_nrs": 2,
        "pain_interference": {"work": 1, "sleep": 1, "mood": 1, "enjoyment": 1},
    }
    if with_red_flag:
        section_a["chest_pain"] = True
    return {
        "answers": {
            "section_a": section_a,
            "section_b": {
                "stiffness": 90, "pain": 90, "function": 90, "stairs": 90, "rising": 90,
            },
            "section_c": {
                "remembered_to_take": True, "took_yesterday": True, "stopped_when_better": True,
                "missed_when_traveling": True, "took_today": True,
                "pt_adherence_pct": 90, "appointments_attended_pct": 100,
            },
            "section_d": {"readiness_0_10": 9},
        }
    }


def test_dayx_survey_d7_clean_green(client):
    pid = _seed_patient(days_post_discharge=7)
    # Engaged patient: log the chat session BEFORE the video event so
    # the first signal-triggered re-tier doesn't pick up the day-7 zero-
    # engagement contributor (Triage Suite Pass 3 §3.3) and shove the
    # patient to TIER_2 (current_tier never re-drops post-op).
    app.state.team_store.log_event(
        patient_id=pid, event_type="avatar_chat", payload={"source": "chat"},
    )
    client.post(f"/api/episodes/{pid}/postop/video-event", json={
        "video_kind": "RED_FLAG", "event_type": "PLAYED", "session_id": "rf1",
    })
    r = client.post(f"/api/episodes/{pid}/postop/survey/7", json=_survey_answers())
    assert r.status_code == 200
    body = r.json()
    assert body["scored"]["tier"] == "GREEN"
    assert body["retier"]["tier_after"] == "TIER_1"


def test_dayx_invalid_day(client):
    pid = _seed_patient()
    r = client.post(f"/api/episodes/{pid}/postop/survey/5", json=_survey_answers())
    assert r.status_code == 400


def test_dayx_red_flag_propagates_to_hard_escalator(client):
    pid = _seed_patient(days_post_discharge=7)
    r = client.post(
        f"/api/episodes/{pid}/postop/survey/7",
        json=_survey_answers(with_red_flag=True),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scored"]["tier"] in ("RED", "ORANGE")
    # Red-flag chip in survey Section A → NEW_RED_FLAG_SYMPTOM hard.
    codes = [r["code"] for r in body["retier"]["reasons"]]
    assert "NEW_RED_FLAG_SYMPTOM" in codes
    assert body["retier"]["tier_after"] == "TIER_3"


# ─── Med adherence response ────────────────────────────────────────────────


def test_med_adherence_yes_records(client):
    pid = _seed_patient()
    r = client.post(
        f"/api/episodes/{pid}/postop/med-adherence",
        json={"response": "YES"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ─── Video event ────────────────────────────────────────────────────────────


def test_video_played_event_recorded(client):
    pid = _seed_patient()
    r = client.post(
        f"/api/episodes/{pid}/postop/video-event",
        json={
            "video_kind": "RED_FLAG",
            "event_type": "PLAYED",
            "session_id": "s1",
        },
    )
    assert r.status_code == 200


# ─── Self-flag ──────────────────────────────────────────────────────────────


def test_self_flag_creates_and_resolves(client, rn_client):
    pid = _seed_patient()
    # Patient creates the self-flag (anonymous).
    r = client.post(
        f"/api/episodes/{pid}/postop/self-flag",
        json={"free_text": "Something feels off"},
    )
    assert r.status_code == 200
    flag_id = r.json()["flagId"]
    # Active self-flag → hard TIER_3.
    assert r.json()["retier"]["tier_after"] == "TIER_3"

    # RN coordinator resolves the flag (Pass-4: rn_coordinator-only).
    r2 = rn_client.post(
        f"/api/episodes/{pid}/postop/self-flag/resolve",
        json={"flag_id": flag_id, "resolved_by": "rn-jane"},
    )
    assert r2.status_code == 200
    # Tier remains TIER_3 (post-op never algorithmically downgrades).
    assert r2.json()["retier"]["tier_after"] == "TIER_3"


def test_self_flag_resolve_unknown_returns_404(rn_client):
    pid = _seed_patient()
    r = rn_client.post(
        f"/api/episodes/{pid}/postop/self-flag/resolve",
        json={"flag_id": 999999, "resolved_by": "rn-jane"},
    )
    assert r.status_code == 404


# ─── Care-goal pivot ───────────────────────────────────────────────────────


def test_care_goal_changed_persists(staff_client):
    pid = _seed_patient()
    r = staff_client.post(
        f"/api/episodes/{pid}/postop/care-goal-changed",
        json={"care_goal_changed": True},
    )
    assert r.status_code == 200
    g = staff_client.get(f"/api/episodes/{pid}/postop").json()
    assert g["careGoalChanged"] is True


# ─── Manual recompute ──────────────────────────────────────────────────────


def test_manual_retier_run_returns_event(staff_client):
    pid = _seed_patient()
    r = staff_client.post(
        f"/api/episodes/{pid}/postop-retier/run",
        json={"triggered_by": "MANUAL:test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["retier"]["triggered_by"] == "MANUAL:test"


# ─── Audit list ────────────────────────────────────────────────────────────


def test_retier_events_list_grows_after_signal(client, staff_client):
    pid = _seed_patient()
    # Audit list = clinical read; check-in = patient-only.
    initial = staff_client.get(f"/api/episodes/{pid}/postop-retier-events").json()["events"]
    client.post(f"/api/episodes/{pid}/postop/checkin", json=_checkin_payload())
    grown = staff_client.get(f"/api/episodes/{pid}/postop-retier-events").json()["events"]
    assert len(grown) == len(initial) + 1
