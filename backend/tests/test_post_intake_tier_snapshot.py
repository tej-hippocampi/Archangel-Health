"""
`post_intake_tier` snapshot semantics (Triage Suite Pass 3 §4).

`post_intake_tier` is stamped exactly once per episode — the first time
the intake-finalize handler triggers a successful pre-op re-tier. The
snapshot:

  - lives on `episode_snapshots.post_intake_tier` (cold-start truth)
    AND on the in-memory `_patient_store` blob (hot cache).
  - is distinct from `initial_tier` (immutable assignment from the EHR
    upload) and `current_tier` (the rolling live tier).
  - never overwrites itself on subsequent intake submissions.

Tests cover: snapshot stamping on first intake, immutability under
later signals, immutability on a second intake submission.
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
from patient_session import create_patient_session  # noqa: E402
from tests._role_auth import tenant_token  # noqa: E402
from triage.preop_retier.apply import apply_preop_retier  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed_preop_t1(client, pid: str, *, hours_until_surgery: int = 96) -> None:
    surgery_iso = (datetime.utcnow() + timedelta(hours=hours_until_surgery)).isoformat()
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "pre_op",
        "specialty": "General Surgery",
        "current_tier": "TIER_1",
        "initial_tier": "TIER_1",
        "initial_tier_was_hard_escalator": False,
        "structured_data": {
            "procedure_name": "Total Knee Arthroplasty",
            "procedure_date": surgery_iso,
        },
        "anchor_procedure_family": "LEJR",
    }
    client.cookies.set("pt_session", create_patient_session(pid, None))


def _section10_pam(value: str) -> dict:
    return {
        f"pam_{i}": {"value": value, "source": "interview"}
        for i in range(1, 14)
    }


# ─── 1. Snapshot stamps on first intake; chain reflects movement ────────────


def test_first_intake_stamps_post_intake_tier(client):
    """Initial T1 + intake submission → `post_intake_tier` stamped."""
    pid = f"pit_first_{uuid.uuid4().hex[:8]}"
    _seed_preop_t1(client, pid)

    r = client.post(
        "/api/pre-op/intake/submit",
        json={
            "patient_id": pid,
            "form_data": {
                "section10_dayOfSurgeryReadiness": _section10_pam("4"),
            },
        },
    )
    assert r.status_code == 200, r.text

    ts = app.state.team_store
    snap = ts.get_episode_snapshot(pid)
    assert snap is not None
    assert snap["post_intake_tier"] in ("TIER_1", "TIER_2", "TIER_3")

    blob = app.state.patient_store[pid]
    assert blob.get("post_intake_tier") == snap["post_intake_tier"]
    assert blob.get("initial_tier") == "TIER_1"
    # current_tier is the rolling live value — may move per re-tier.

    # `POST_INTAKE_TIER_SNAPSHOTTED` event was logged with the right tier.
    events = ts.get_events(pid)
    snap_events = [e for e in events if e["event_type"] == "POST_INTAKE_TIER_SNAPSHOTTED"]
    assert len(snap_events) == 1
    assert snap_events[0]["payload"]["tier"] == snap["post_intake_tier"]


# ─── 2. post_intake_tier is immutable when current_tier moves ───────────────


def test_post_intake_tier_immutable_when_current_changes(client):
    """A signal that pushes `current_tier` upward after intake must NOT
    move `post_intake_tier`. The snapshot is once-per-episode."""
    pid = f"pit_immut_{uuid.uuid4().hex[:8]}"
    _seed_preop_t1(client, pid)

    r = client.post(
        "/api/pre-op/intake/submit",
        json={
            "patient_id": pid,
            "form_data": {
                "section10_dayOfSurgeryReadiness": _section10_pam("4"),
            },
        },
    )
    assert r.status_code == 200

    ts = app.state.team_store
    pit_after_intake = (ts.get_episode_snapshot(pid) or {}).get("post_intake_tier")
    assert pit_after_intake is not None

    # Submit a critical pre-op survey RED with a red flag → triggers a
    # re-tier that hard-escalates to TIER_3.
    blob = app.state.patient_store[pid]
    blob["initial_tier_was_hard_escalator"] = False
    ts.upsert_episode_snapshot(pid, initial_tier_was_hard_escalator=False)

    # Force the upgrade by directly invoking apply_preop_retier with a
    # critical survey side-loaded into the team store.
    ts.save_preop_survey(
        patient_id=pid,
        window="T_24",
        status="SUBMITTED_RED",
        has_critical_red_flag=True,
        responses={},
    ) if hasattr(ts, "save_preop_survey") else None

    # Force a re-tier directly
    apply_preop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CURRENT_MOVES",
    )

    # `post_intake_tier` snapshot must still equal what was stamped at intake.
    post_snap = ts.get_episode_snapshot(pid) or {}
    assert post_snap.get("post_intake_tier") == pit_after_intake
    assert app.state.patient_store[pid].get("initial_tier") == "TIER_1"


# ─── 3. Second intake submission does NOT overwrite the snapshot ───────────


def test_second_intake_does_not_overwrite_snapshot(client):
    pid = f"pit_second_{uuid.uuid4().hex[:8]}"
    _seed_preop_t1(client, pid)

    # First intake — establishes the snapshot.
    client.post(
        "/api/pre-op/intake/submit",
        json={
            "patient_id": pid,
            "form_data": {"section10_dayOfSurgeryReadiness": _section10_pam("4")},
        },
    )
    ts = app.state.team_store
    pit_first = (ts.get_episode_snapshot(pid) or {}).get("post_intake_tier")
    assert pit_first is not None

    # Second intake with very different PAM — the snapshot must not move.
    r2 = client.post(
        "/api/pre-op/intake/submit",
        json={
            "patient_id": pid,
            "form_data": {"section10_dayOfSurgeryReadiness": _section10_pam("1")},
        },
    )
    assert r2.status_code == 200

    pit_second = (ts.get_episode_snapshot(pid) or {}).get("post_intake_tier")
    assert pit_second == pit_first, (
        "post_intake_tier is stamped exactly once per episode (Pass 3 §4.2)"
    )

    # Only one POST_INTAKE_TIER_SNAPSHOTTED event was logged.
    events = ts.get_events(pid)
    snap_events = [e for e in events if e["event_type"] == "POST_INTAKE_TIER_SNAPSHOTTED"]
    assert len(snap_events) == 1, (
        f"Expected exactly one snapshot event, got {len(snap_events)}"
    )


# ─── 4. /api/patients exposes the chain for the doctor surface ─────────────


def test_patients_endpoint_serializes_tier_chain(client):
    pid = f"pit_api_{uuid.uuid4().hex[:8]}"
    _seed_preop_t1(client, pid)
    app.state.patient_store[pid].update({
        "name": "Chain Patient",
        "phone": "555-0100",
        "email": "chain@example.com",
        "pipeline_type": "pre_op",
    })

    client.post(
        "/api/pre-op/intake/submit",
        json={
            "patient_id": pid,
            "form_data": {"section10_dayOfSurgeryReadiness": _section10_pam("4")},
        },
    )

    auth = {"Authorization": f"Bearer {tenant_token('surgeon')}"}
    r = client.get("/api/patients", headers=auth)
    assert r.status_code == 200
    rows = {p["id"]: p for p in r.json()["patients"]}
    assert pid in rows
    row = rows[pid]
    # Three-tier chain on the wire (Pass 3 §4.3).
    assert "initialTier" in row
    assert "postIntakeTier" in row
    assert "currentTier" in row
    assert row["initialTier"] == "TIER_1"
    assert row["postIntakeTier"] is not None
