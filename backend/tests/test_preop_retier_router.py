"""
Smoke tests for the Pre-Op Re-Tier HTTP surface (Pass 2 §1.3).

Algorithm correctness is covered by `test_preop_retier.py`; here we
verify the wire format and router-level behavior:

  * `POST /api/triage/preop-retier/compute`         — pure preview.
  * `POST /api/episodes/{id}/preop-retier/run`      — manual recompute.
  * `POST /api/episodes/{id}/pam`                   — submit + re-tier.
  * `POST /api/events/preop-video`                  — dedupe + re-tier.
  * `POST /api/events/battlecard`                   — dedupe + re-tier.
  * `GET  /api/triage/tuning/preop-retier/current`  — read-only tuning.
  * `POST /api/triage/tuning/preop-retier`          — admin no-op stub.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402


@pytest.fixture()
def client():
    """Anonymous TestClient for patient-only endpoints (`/pam`,
    `/api/events/preop-video`, `/api/events/battlecard`).
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def staff_client():
    """Surgeon-authed TestClient for clinical reads/writes
    (`compute`, `run`, tuning GET)."""
    with TestClient(app, headers=auth_headers("surgeon", source="landing")) as c:
        yield c


def _seed_preop(*, initial_tier: str = "TIER_1") -> str:
    pid = f"preop_retier_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "pre_op",
        "specialty": "General Surgery",
        "current_tier": initial_tier,
        "initial_tier": initial_tier,
        "initial_tier_was_hard_escalator": False,
        "structured_data": {
            "procedure_name": "Total Knee Arthroplasty",
            "procedure_date": "2099-12-15T07:00:00",
        },
        "anchor_procedure_family": "LEJR",
    }
    return pid


def _retier_state_payload(*, hours_until: int = 72) -> dict:
    return {
        "initial_tier": "TIER_1",
        "initial_tier_was_hard_escalator": False,
        "hours_until_surgery": hours_until,
        "pam": None,
        "intake": {"status": "STARTED"},
        "surveys": [
            {"window": "T_96", "status": "PENDING"},
            {"window": "T_48", "status": "PENDING"},
            {"window": "T_24", "status": "PENDING"},
        ],
        "video": {"sessions": []},
        "battle_card": {"views": []},
    }


# ─── Compute preview ────────────────────────────────────────────────────────


def test_compute_returns_retier_result(staff_client):
    r = staff_client.post("/api/triage/preop-retier/compute", json=_retier_state_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["initial_tier"] == "TIER_1"
    assert body["computed_tier"] in ("TIER_1", "TIER_2", "TIER_3")
    assert "delta" in body and "reasons" in body and "model_version" in body


# ─── Manual run ─────────────────────────────────────────────────────────────


def test_run_writes_snapshot_and_event(staff_client):
    pid = _seed_preop()
    r = staff_client.post(
        f"/api/episodes/{pid}/preop-retier/run",
        json={"triggered_by": "MANUAL:UNIT_TEST"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    snap = body["event"]
    assert snap["episode_id"] == pid
    assert snap["triggered_by"] == "MANUAL:UNIT_TEST"

    rows = app.state.team_store.list_preop_retier_events(pid)
    assert len(rows) == 1
    assert rows[0]["triggered_by"] == "MANUAL:UNIT_TEST"


def test_run_unknown_patient_returns_404(staff_client):
    r = staff_client.post(
        f"/api/episodes/missing-{uuid.uuid4().hex[:6]}/preop-retier/run",
        json={},
    )
    assert r.status_code == 404


# ─── PAM submit ─────────────────────────────────────────────────────────────


def test_pam_submit_persists_row_and_runs_retier(client):
    pid = _seed_preop()
    responses = [{"item_index": i, "value": 4} for i in range(1, 14)]
    r = client.post(f"/api/episodes/{pid}/pam", json={"responses": responses})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["level"] == "HIGH"
    assert body["is_complete"] is True
    assert body["activation_score"] > 67.0
    assert body["retier"]["triggered_by"] == "SIGNAL:INTAKE_PAM"

    pam = app.state.team_store.get_latest_pam_assessment(pid)
    assert pam is not None
    assert pam["level"] == "HIGH"


def test_pam_submit_rejects_invalid_value(client):
    pid = _seed_preop()
    responses = [{"item_index": 1, "value": 9}]
    r = client.post(f"/api/episodes/{pid}/pam", json={"responses": responses})
    assert r.status_code == 422


# ─── Pre-Op video event ─────────────────────────────────────────────────────


def test_preop_video_event_logged_and_dedupes(client):
    pid = _seed_preop()
    body = {
        "episode_id": pid, "session_id": "sess-1",
        "duration_sec": 30, "completed_session": False,
    }
    r1 = client.post("/api/events/preop-video", json=body)
    r2 = client.post("/api/events/preop-video", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["deduped"] is True

    logs = app.state.team_store.get_events(pid)
    played = [e for e in logs if e.get("event_type") == "PREOP_VIDEO_PLAYED"]
    assert len(played) == 1


# ─── Battlecard event ───────────────────────────────────────────────────────


def test_battlecard_event_logged_and_dedupes(client):
    pid = _seed_preop()
    body = {"episode_id": pid, "dwell_ms": 2500, "scroll_depth_pct": 80}
    r1 = client.post("/api/events/battlecard", json=body)
    r2 = client.post("/api/events/battlecard", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["deduped"] is True


# ─── Tuning ─────────────────────────────────────────────────────────────────


def test_tuning_current(staff_client):
    r = staff_client.get("/api/triage/tuning/preop-retier/current")
    assert r.status_code == 200
    body = r.json()
    assert "modelVersion" in body or "model_version" in body


def test_tuning_post_requires_admin_token(client):
    r = client.post("/api/triage/tuning/preop-retier", json={})
    assert r.status_code == 401


def test_tuning_post_with_admin_token_returns_config(client):
    r = client.post(
        "/api/triage/tuning/preop-retier",
        headers={"X-Admin-Token": os.environ["ADMIN_AUTH_TOKEN"]},
        json={"weights": {"foo": 1}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deployed"] is False
    assert "config" in body
