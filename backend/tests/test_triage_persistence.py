"""
Restart-survival regression test (Triage Suite Pass 3 §1.4).

The two algorithm-guard fields that govern post-intake / post-intra-op
behavior live both on the in-memory `_patient_store` blob (hot cache)
and in the SQLite `episode_snapshots` table (cold-start source of
truth):

  - `initial_tier_was_hard_escalator` — read by the pre-op re-tier
    sticky-hard guard.
  - `post_intraop_tier`               — read by the post-op re-tier as
    the immutable lower-bound floor.

This test simulates a process restart (hot cache gone) and confirms
both fields rehydrate from the snapshot table so subsequent algorithm
runs behave identically to a no-restart run.
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
from triage.preop_retier.apply import _gather_state as _preop_gather_state  # noqa: E402
from triage.postop.apply import _gather_state as _postop_gather_state  # noqa: E402
from triage.preop_retier.algo import re_tier_preop  # noqa: E402


@pytest.fixture()
def client():
    """Surgeon-authed client so `/api/episodes/{id}/initial-tier` passes
    the pass-4 `WRITE_CLINICAL` gate."""
    from tests._role_auth import auth_headers
    with TestClient(app, headers=auth_headers("surgeon", source="landing")) as c:
        yield c


# ─── helpers ────────────────────────────────────────────────────────────────


def _seed_preop(pid: str, *, surgery_iso: str = "2099-12-15T07:00:00") -> None:
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "pre_op",
        "specialty": "General Surgery",
        "current_tier": "TIER_3",
        "initial_tier": "TIER_3",
        "initial_tier_was_hard_escalator": True,
        "structured_data": {
            "procedure_name": "Emergency Cholecystectomy",
            "procedure_date": surgery_iso,
        },
        "anchor_procedure_family": "GEN",
    }


def _seed_postop(pid: str) -> None:
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "post_op",
        "specialty": "Orthopedics",
        "current_tier": "TIER_2",
        "post_intraop_tier": "TIER_2",
        "post_intraop_tier_at": datetime.utcnow().replace(microsecond=0).isoformat(),
        "discharge_at": (datetime.utcnow() - timedelta(days=2)).isoformat(),
        "anchor_procedure_family": "LEJR",
    }


# ─── 1. Hard-escalator survives restart ─────────────────────────────────────


def test_initial_tier_was_hard_escalator_survives_restart(client):
    """After persisting via `/api/episodes/{id}/initial-tier`, the flag
    in `episode_snapshots` must drive the sticky-hard guard even when
    the in-memory blob has been wiped (process restart simulation).
    """
    pid = f"persist_hard_{uuid.uuid4().hex[:8]}"
    _seed_preop(pid)

    # 1) Persist via the router so the snapshot is written through.
    #    `is_emergency=True` triggers a HARD reason → guard flag set.
    payload = {
        "input": {
            "procedure": {
                "cpt_code": "27447",
                "anchor_procedure_family": "LEJR",
                "scheduled_date": "2099-12-15",
                "is_emergency": True,
            },
            "active_problems": {
                "problems": [{"icd10": "I10", "description": "HTN", "status": "ACTIVE"}],
                "functional_status": "INDEPENDENT",
            },
            "medications": {"medications": []},
            "allergies": {"allergies": []},
            "social_history": {
                "age": 78,
                "smoking_status": "NEVER",
                "lives_alone": False,
                "has_reliable_caregiver": True,
            },
            "recent_labs": {"labs": [], "studies": []},
        }
    }
    r = client.post(f"/api/episodes/{pid}/initial-tier", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["initialTierWasHardEscalator"] is True

    ts = app.state.team_store
    snap = ts.get_episode_snapshot(pid)
    assert snap is not None
    assert snap["initial_tier_was_hard_escalator"] is True

    # 2) Simulate a process restart — wipe the blob and re-seed without
    #    the guard flag (so the only place the bit survives is the
    #    snapshot row).
    del app.state.patient_store[pid]
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "pre_op",
        "specialty": "General Surgery",
        "current_tier": "TIER_3",
        "initial_tier": "TIER_3",
        # Intentionally absent — must be hydrated from episode_snapshots.
        "structured_data": {
            "procedure_name": "Emergency Cholecystectomy",
            "procedure_date": "2099-12-15T07:00:00",
        },
        "anchor_procedure_family": "GEN",
    }

    # 3) Drive `_gather_state` and confirm the flag was hydrated.
    state = _preop_gather_state(
        patient_id=pid,
        patient=app.state.patient_store[pid],
        team_store=ts,
    )
    assert state.initial_tier_was_hard_escalator is True, (
        "Pass 3 read-through must rehydrate the hard-escalator bit on "
        "cold start when the blob is missing the field."
    )

    # 4) Algorithmic confirmation — same state as the working sticky-
    #    hard test but with the bit coming from the snapshot row.
    from triage.preop_retier.types import (
        BattleCardEngagement,
        IntakeState,
        PamResult,
        PreOpReTierInput,
        SurveyWindowState,
        VideoEngagement,
    )

    state5 = PreOpReTierInput(
        initial_tier="TIER_3",
        initial_tier_was_hard_escalator=state.initial_tier_was_hard_escalator,
        hours_until_surgery=72,
        pam=PamResult(
            raw_sum=39, items_scored=13, raw_average=3.0,
            activation_score=90.0, level="HIGH", is_complete=True,
        ),
        intake=IntakeState(status="NOT_REQUIRED"),
        surveys=[
            SurveyWindowState(window="T_96", status="PENDING"),
            SurveyWindowState(window="T_48", status="PENDING"),
            SurveyWindowState(window="T_24", status="PENDING"),
        ],
        video=VideoEngagement(sessions=[80]),
        battle_card=BattleCardEngagement(views=[60]),
    )
    out = re_tier_preop(state5)
    assert out.delta == -5
    assert out.computed_tier == "TIER_3", (
        "Sticky-hard guard must still clamp the downgrade after restart."
    )


# ─── 2. post_intraop_tier survives restart ──────────────────────────────────


def test_post_intraop_tier_survives_restart(client):
    """After an intra-op lock writes the floor, blowing away the blob
    must still leave the floor available to the post-op algorithm.
    """
    pid = f"persist_floor_{uuid.uuid4().hex[:8]}"
    _seed_postop(pid)
    ts = app.state.team_store

    # Write the floor through directly (mirrors what `triage.intraop.apply`
    # does after a lock).
    ts.upsert_episode_snapshot(pid, post_intraop_tier="TIER_2")

    # Simulate a restart — wipe `post_intraop_tier` from the blob.
    blob = app.state.patient_store[pid]
    blob["post_intraop_tier"] = None

    state = _postop_gather_state(
        patient_id=pid,
        patient=blob,
        team_store=ts,
    )
    assert state.post_intraop_tier == "TIER_2", (
        "Pass 3 read-through must rehydrate the post-intra-op floor on "
        "cold start when the blob has lost the field."
    )
    # Verify the blob was hot-cache-hydrated as a side effect of the
    # read-through (so subsequent reads in this process are O(1)).
    assert blob["post_intraop_tier"] == "TIER_2"
