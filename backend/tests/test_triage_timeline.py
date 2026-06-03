from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402


def _seed_escalation_case() -> tuple[str, int]:
    pid = f"timeline_case_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow().replace(microsecond=0)
    app.state.patient_store[pid] = {
        "id": pid,
        "name": "Maria Gonzalez",
        "email": "maria@example.com",
        "phase": "post_op",
        "pipeline_type": "post_op",
        "initial_tier": "TIER_1",
        "initial_tier_assigned_at": (now - timedelta(days=12)).isoformat(),
        "initial_tier_reasons": [
            {
                "kind": "BASE",
                "code": "LEJR_BASE",
                "label": "Joint replacement baseline risk",
                "weight": 1,
                "detail": None,
            }
        ],
        "current_tier": "TIER_3",
        "tier_last_changed": (now - timedelta(days=3)).isoformat(),
        "structured_data": {"procedure_date": (now - timedelta(days=13)).date().isoformat()},
        "or_started_at": (now - timedelta(days=11, hours=4)).isoformat(),
        "or_ended_at": (now - timedelta(days=11, hours=1)).isoformat(),
        "discharge_at": (now - timedelta(days=10)).isoformat(),
    }

    ts = app.state.team_store
    ts.ensure_episode(patient_id=pid)
    esc_id = ts.create_escalation(
        patient_id=pid,
        tier=2,
        trigger_type="chat:semantic",
        message="Risk concern",
        conversation_snapshot=[{"role": "patient", "content": "I feel worse"}],
    )
    ts.save_preop_retier_event(
        event_id=f"pre_{uuid.uuid4().hex[:8]}",
        episode_id=pid,
        triggered_by="PREOP_PAM",
        inputs_snapshot={},
        initial_tier="TIER_1",
        initial_tier_was_hard=False,
        computed_delta=0,
        computed_tier="TIER_1",
        tier_before="TIER_1",
        tier_after="TIER_1",
        changed=False,
        reasons=[],
        model_version="test",
        tuning_version=1,
    )
    ts.save_intraop_reassessment(
        reassessment_id=f"intra_{uuid.uuid4().hex[:8]}",
        patient_id=pid,
        intraop_form_id=f"form_{uuid.uuid4().hex[:6]}",
        form_snapshot={},
        pre_or_current_tier="TIER_1",
        proposed_tier="TIER_2",
        final_tier="TIER_2",
        hard_upgrade_applied=False,
        upgrade_steps=1,
        reasons=[{"kind": "SOFT", "code": "INTRA_BP", "label": "BP instability", "weight": 6}],
        is_conservative_default=False,
        procedure_family="LEJR",
        model_version="test",
        tuning_version=1,
        triggered_by="SURGEON_LOCK",
    )
    ts.save_postop_retier_event(
        event_id=f"post_{uuid.uuid4().hex[:8]}",
        patient_id=pid,
        triggered_by="SURVEY_D7",
        inputs_snapshot={"days_since_discharge": 7},
        post_intraop_tier="TIER_2",
        computed_delta=1,
        computed_tier="TIER_3",
        tier_before="TIER_2",
        tier_after="TIER_3",
        changed=True,
        reasons=[
            {
                "kind": "HARD",
                "code": "DAY7_RED_SURVEY",
                "label": "Patient scored RED on Day 7 survey",
                "weight": 85,
                "detail": "day7_red_flag: True",
            }
        ],
        model_version="test",
        tuning_version=1,
    )
    return pid, esc_id


def test_triage_timeline_aggregates_sources_and_labels_phases():
    pid, escalation_id = _seed_escalation_case()
    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="timeline@test.local")) as client:
        r = client.get(f"/api/escalations/{escalation_id}/triage-timeline")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient_id"] == pid
    assert body["patient_name"] == "Maria Gonzalez"
    assert body["current_tier"] == 3
    assert body["current_tier_since"] == app.state.patient_store[pid]["tier_last_changed"]
    assert body["intervention_subject"].endswith("— URGENT CARE MESSAGE")

    timeline = body.get("timeline") or []
    assert len(timeline) >= 4
    sources = {row.get("source") for row in timeline}
    assert {"initial", "preop", "intraop", "postop"}.issubset(sources)

    postop = [row for row in timeline if row.get("source") == "postop"]
    assert postop
    assert postop[-1].get("phase") == "POST_OP"
    assert str(postop[-1].get("phase_label") or "").startswith("Post-Op")
    assert any(str(reason.get("kind")).upper() == "HARD" for reason in (postop[-1].get("reasons") or []))
